# tasks-cockpit

Eigenständiger Task-Service für **`tasks.ooopppmmm.com`** — getrennt von KDP/Merch
(eigenes Postgres, eigener Service, eigene Subdomain). Der Daily-Brief-Executor
schreibt hier Maßnahmen rein; du siehst & steuerst sie im Dashboard.

## Was drin ist
- **Postgres** (gebündelt, eigene DB `tasks`)
- **REST-API** (Header `X-API-Key`) — für den Executor
- **Dashboard** (HTTP Basic Auth) auf derselben Subdomain: Kanban mit
  🟢 erledige ich · 🟡 Freigabe nötig · 🔴 nur du, plus Freigeben/Erledigt/Verwerfen

Getestet: 11/11 End-to-End-Checks grün (API-Auth, Create, idempotenter Upsert,
Patch, Validierung, Dashboard-Render, UI-Aktionen).

## 1. Secrets erzeugen
```bash
openssl rand -hex 32   # -> POSTGRES_PASSWORD
openssl rand -hex 32   # -> API_KEY
openssl rand -base64 18 # -> DASH_PASS
```

## 2. Auf GitHub pushen
```bash
cd "C:\Users\alexa\Claude\Projects\tasks-cockpit"
git init && git add . && git commit -m "tasks-cockpit init"
git branch -M main
git remote add origin https://github.com/MasterRico/tasks-cockpit.git
git push -u origin main
```
(Repo vorher auf github.com/MasterRico anlegen. `.env` ist via `.gitignore`
ausgeschlossen — es landen nur Code, keine Secrets im Repo.)

## 3. DNS
A-Record **`tasks.ooopppmmm.com` → 46.224.24.51** setzen (falls du keinen
Wildcard `*.ooopppmmm.com` hast — die anderen Subdomains laufen ja schon).

## 4. Coolify
1. **+ New** → **Docker Compose** → Git-Repo `MasterRico/tasks-cockpit`, Branch `main`.
2. **Environment Variables** setzen (aus Schritt 1):
   `POSTGRES_PASSWORD`, `API_KEY`, `DASH_PASS`, optional `DASH_USER` (Default `alex`).
3. **Domains**: dem Service **`app`** die Domain `https://tasks.ooopppmmm.com`
   zuweisen, interner Port **8000**.
4. **Deploy**.

Health-Check danach: `https://tasks.ooopppmmm.com/health` → `{"status":"ok"}`.
Dashboard: `https://tasks.ooopppmmm.com/` (Login `DASH_USER` / `DASH_PASS`).

## API (für den Executor)
Auth: Header `X-API-Key: <API_KEY>`.

| Methode | Pfad | Zweck |
|---|---|---|
| GET | `/api/tasks?status=&project=` | Tasks lesen |
| POST | `/api/tasks` | Task anlegen/aktualisieren (Upsert über `dedupe_key`) |
| PATCH | `/api/tasks/{id}` | Status/Ergebnis aktualisieren |
| GET | `/health` | Healthcheck (ohne Auth) |

**Felder:** `title` (pflicht), `project`, `source`, `detail`, `measure`,
`micro_action`, `automation_level` (`green`/`yellow`/`red`), `priority`
(`high`/`medium`/`low`), `due_date` (`YYYY-MM-DD`), `dedupe_key`.

`automation_level` steuert den Startstatus: `yellow` → `awaiting_approval`
(wartet auf deine Freigabe), `green`/`red` → `open`.

Beispiel:
```bash
curl -s -X POST https://tasks.ooopppmmm.com/api/tasks \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"title":"dad_jokes Slots befüllen","project":"Merch by Amazon",
       "source":"daily-brief 2026-06-12","measure":"10 dad_jokes Designs in den Slot-Plan",
       "micro_action":"10 Designs ziehen","automation_level":"yellow",
       "priority":"high","due_date":"2026-06-14","dedupe_key":"2026-06-12:merch:slots"}'
```

`dedupe_key` verhindert Duplikate: läuft der Brief mehrmals, wird derselbe
Task aktualisiert statt neu angelegt. Status & Ergebnis bleiben dabei erhalten.

## Status-Modell
`open` → `in_progress` → `done` · `awaiting_approval` (🟡 wartet auf dich) ·
`blocked` · `cancelled`. Dashboard-Buttons: **Freigeben** (🟡 → in Arbeit),
**Erledigt**, **Verwerfen**, **Wieder öffnen**.

## Lokal testen
```bash
pip install -r requirements.txt
# Postgres bereitstellen, dann:
DATABASE_URL=postgresql://tasks:pw@localhost:5432/tasks \
API_KEY=dev DASH_USER=alex DASH_PASS=dev \
uvicorn app.main:app --reload
```
