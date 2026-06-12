"""tasks-cockpit — minimaler Task-Service.

REST-API (X-API-Key) für den Executor + ein Dashboard (HTTP Basic Auth)
auf derselben Subdomain. Eigenes Postgres, getrennt von KDP/Merch.
"""
import os
import secrets
from contextlib import asynccontextmanager
from datetime import date
from typing import Optional

from fastapi import (
    Depends, FastAPI, Form, Header, HTTPException, Request, status,
)
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pydantic import BaseModel

DATABASE_URL = os.environ["DATABASE_URL"]
API_KEY = os.environ.get("API_KEY", "")
DASH_USER = os.environ.get("DASH_USER", "alex")
DASH_PASS = os.environ.get("DASH_PASS", "")

LEVELS = ("green", "yellow", "red")
STATUSES = ("open", "in_progress", "awaiting_approval", "blocked", "done", "cancelled")
PRIORITIES = ("high", "medium", "low")

# Reihenfolge der Spalten im Dashboard
BOARD = ("awaiting_approval", "in_progress", "open", "blocked", "done")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    source       TEXT,
    project      TEXT,
    title        TEXT NOT NULL,
    detail       TEXT,
    measure      TEXT,
    micro_action TEXT,
    automation_level TEXT NOT NULL DEFAULT 'yellow'
        CHECK (automation_level IN ('green','yellow','red')),
    status       TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open','in_progress','awaiting_approval','blocked','done','cancelled')),
    priority     TEXT NOT NULL DEFAULT 'medium'
        CHECK (priority IN ('high','medium','low')),
    due_date     DATE,
    result       TEXT,
    dedupe_key   TEXT UNIQUE
);
"""

pool = ConnectionPool(
    DATABASE_URL, min_size=1, max_size=5, open=False,
    kwargs={"row_factory": dict_row, "autocommit": True},
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool.open()
    with pool.connection() as conn:
        conn.execute(SCHEMA)
    yield
    pool.close()


app = FastAPI(title="tasks-cockpit", lifespan=lifespan)
templates = Jinja2Templates(directory="app/templates")
basic = HTTPBasic()


# ---------- Auth ----------
def require_api_key(x_api_key: str = Header(default="")):
    if not API_KEY:
        raise HTTPException(500, "API_KEY not configured")
    if not secrets.compare_digest(x_api_key, API_KEY):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad api key")


def require_dash(creds: HTTPBasicCredentials = Depends(basic)):
    ok_user = secrets.compare_digest(creds.username, DASH_USER)
    ok_pass = secrets.compare_digest(creds.password, DASH_PASS or "")
    if not (ok_user and ok_pass):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        )


# ---------- Schemas ----------
class TaskIn(BaseModel):
    title: str
    project: Optional[str] = None
    source: Optional[str] = None
    detail: Optional[str] = None
    measure: Optional[str] = None
    micro_action: Optional[str] = None
    automation_level: str = "yellow"
    status: Optional[str] = None
    priority: str = "medium"
    due_date: Optional[date] = None
    dedupe_key: Optional[str] = None


class TaskPatch(BaseModel):
    status: Optional[str] = None
    result: Optional[str] = None
    automation_level: Optional[str] = None
    priority: Optional[str] = None
    measure: Optional[str] = None
    micro_action: Optional[str] = None
    due_date: Optional[date] = None


def _default_status(level: str) -> str:
    # gelb -> erst Freigabe, grün/rot -> offen
    return "awaiting_approval" if level == "yellow" else "open"


# ---------- API ----------
@app.get("/health")
def health():
    with pool.connection() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok"}


@app.get("/api/tasks", dependencies=[Depends(require_api_key)])
def list_tasks(status: Optional[str] = None, project: Optional[str] = None,
               limit: int = 200):
    sql = "SELECT * FROM tasks WHERE 1=1"
    args: list = []
    if status:
        sql += " AND status = %s"
        args.append(status)
    if project:
        sql += " AND project = %s"
        args.append(project)
    sql += " ORDER BY (priority='high') DESC, created_at DESC LIMIT %s"
    args.append(limit)
    with pool.connection() as conn:
        rows = conn.execute(sql, args).fetchall()
    return JSONResponse([_jsonable(r) for r in rows])


@app.post("/api/tasks", dependencies=[Depends(require_api_key)])
def upsert_task(t: TaskIn):
    if t.automation_level not in LEVELS:
        raise HTTPException(422, f"automation_level must be one of {LEVELS}")
    if t.priority not in PRIORITIES:
        raise HTTPException(422, f"priority must be one of {PRIORITIES}")
    st = t.status or _default_status(t.automation_level)
    if st not in STATUSES:
        raise HTTPException(422, f"status must be one of {STATUSES}")
    sql = """
        INSERT INTO tasks
            (title, project, source, detail, measure, micro_action,
             automation_level, status, priority, due_date, dedupe_key)
        VALUES
            (%(title)s, %(project)s, %(source)s, %(detail)s, %(measure)s,
             %(micro_action)s, %(automation_level)s, %(status)s, %(priority)s,
             %(due_date)s, %(dedupe_key)s)
        ON CONFLICT (dedupe_key) DO UPDATE SET
            detail = EXCLUDED.detail,
            measure = EXCLUDED.measure,
            micro_action = EXCLUDED.micro_action,
            automation_level = EXCLUDED.automation_level,
            priority = EXCLUDED.priority,
            due_date = EXCLUDED.due_date,
            updated_at = now()
        RETURNING *;
    """
    params = t.model_dump()
    params["status"] = st
    with pool.connection() as conn:
        row = conn.execute(sql, params).fetchone()
    return _jsonable(row)


@app.patch("/api/tasks/{task_id}", dependencies=[Depends(require_api_key)])
def patch_task(task_id: str, p: TaskPatch):
    fields = {k: v for k, v in p.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(422, "no fields to update")
    if "status" in fields and fields["status"] not in STATUSES:
        raise HTTPException(422, f"status must be one of {STATUSES}")
    sets = ", ".join(f"{k} = %({k})s" for k in fields)
    fields["id"] = task_id
    sql = f"UPDATE tasks SET {sets}, updated_at = now() WHERE id = %(id)s RETURNING *;"
    with pool.connection() as conn:
        row = conn.execute(sql, fields).fetchone()
    if not row:
        raise HTTPException(404, "task not found")
    return _jsonable(row)


# ---------- Dashboard ----------
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, _=Depends(require_dash)):
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT * FROM tasks ORDER BY (priority='high') DESC, created_at DESC"
        ).fetchall()
    columns = {s: [r for r in rows if r["status"] == s] for s in BOARD}
    counts = {s: len(columns[s]) for s in BOARD}
    return templates.TemplateResponse(
        request,
        "index.html",
        {"columns": columns, "board": BOARD, "counts": counts},
    )


@app.post("/ui/{task_id}/{action}")
def ui_action(task_id: str, action: str, _=Depends(require_dash)):
    mapping = {
        "approve": "in_progress",   # gelb freigegeben -> in Arbeit
        "done": "done",
        "cancel": "cancelled",
        "reopen": "open",
    }
    if action not in mapping:
        raise HTTPException(404, "unknown action")
    with pool.connection() as conn:
        conn.execute(
            "UPDATE tasks SET status=%s, updated_at=now() WHERE id=%s",
            (mapping[action], task_id),
        )
    return RedirectResponse("/", status_code=303)


# ---------- Helpers ----------
def _jsonable(row: dict) -> dict:
    out = dict(row)
    for k in ("id",):
        if out.get(k) is not None:
            out[k] = str(out[k])
    for k in ("created_at", "updated_at", "due_date"):
        if out.get(k) is not None:
            out[k] = out[k].isoformat()
    return out
