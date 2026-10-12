# MaintAI — Equipment Maintenance Triage Assistant

A runnable, evidence-first maintenance triage MVP for a hiring assessment. It combines deterministic sensor checks, a small synthetic knowledge base, structured AI-assisted triage (optional local Ollama), editable work-order drafts, human approval, maintenance history and audit events.

> **Safety and scope:** This is a demo, not a certified industrial safety system. Seeded manuals and thresholds are synthetic and illustrative. Validate all limits and procedures against approved site documentation before real use. The app has no equipment-control integration and does not automatically approve work.

## Features

- Equipment registry and persistent issue reports.
- Required asset and symptom fields; optional recent events and sensor readings.
- Deterministic threshold rules with units, boundary behavior, stale/missing/wrong-unit/conflicting data states.
- Keyword retrieval over approved synthetic manual sections, with source IDs and revisions.
- Hypotheses explicitly labeled unconfirmed, targeted questions and inspection suggestions.
- Visible local-model failure and disclosed deterministic fallback.
- Draft work orders with optimistic revision checks, submit-for-review, server-side role header checks, approval/rejection and required rejection reason.
- Separate technician-confirmed maintenance findings and equipment history.
- Audit events and API health endpoint.
- Responsive dashboard and automated API tests.

## Quick start (Python)

Requires Python 3.11+ (3.12 recommended).

```bash
python -m venv .venv
# macOS/Linux
source .venv/bin/activate
# Windows PowerShell
# .venv\Scripts\Activate.ps1

pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload
```

Open http://localhost:8000. API docs: http://localhost:8000/docs. Health: http://localhost:8000/api/health.

The database is created at `data/triage.db` by default. To reset the demo database, stop the server and delete that file; startup recreates the schema and seed data.

## Run tests

```bash
pytest -q
```

Tests use a temporary SQLite database configured before the application is imported.

## Docker

```bash
docker compose up --build
```

Open http://localhost:8000. The SQLite database persists in the `triage_data` volume.

## Render demo deployment

The repository includes a Render Blueprint for a free demo web service. Connect the GitHub repository in Render and deploy the Blueprint from `render.yaml`. The container uses Render's assigned `PORT` and checks `/api/health`.

The free service uses ephemeral storage, so SQLite data can be lost when the instance restarts or redeploys. The demo also has no real authentication; do not use it for sensitive data or operational decisions. Use an authenticated deployment and persistent database/storage before real users rely on it.

## Optional local model (no paid API)

Install Ollama and pull a model that fits your machine, for example:

```bash
ollama pull llama3.2
ollama serve
```

In another terminal set:

```bash
# macOS/Linux
export AI_MODE=ollama
export OLLAMA_URL=http://localhost:11434
export OLLAMA_MODEL=llama3.2
uvicorn app.main:app --reload
```

For Windows PowerShell use `$env:AI_MODE="ollama"` etc. If using Docker, set `AI_MODE=ollama` in `.env`; the compose file defaults the URL to `host.docker.internal`. Local model availability and memory requirements vary by model/hardware.

If Ollama is unavailable, the backend records a partial triage run and displays a clearly labeled deterministic fallback. It does not claim the model succeeded. Deterministic rule results, retrieval sources and priority are preserved. The local model adapter is a starter integration; its free-form fields still need stronger citation-grounding validation before any production deployment.

## Demo workflow

1. Open **Report an issue**.
2. Choose `MTR-001`, describe unusual vibration after startup, add a startup event, and optionally add `vibration = 8.2 mm/s`.
3. Inspect the triage: rule output, retrieved demo manual, possible causes, questions and inspection suggestions.
4. Open the work-order draft, edit it, save it, and submit for review.
5. Approve or reject it. Approval controls only the database work-order state, never equipment.
6. Add a technician finding to the issue and inspect equipment history/audit events.
7. Try missing values, wrong units, stale timestamps (API), conflicting duplicate readings, and a request with `X-Role: technician` to the approval endpoint.

## Demo sensor rules

These are illustrative software-test values only:
- `motor_temperature`: above 85 C
- `vibration`: above 7.1 mm/s
- `hydraulic_pressure`: below 2 bar
- `motor_current`: above 18 A

Threshold comparison is strict (`>` or `<`); a value exactly at the threshold is not exceeded. No conversions are silently performed. The freshness window is 24 hours for supplied ISO timestamps. Configure and validate site-specific limits before any real use.

## API overview

- `GET /api/health`
- `GET/POST /api/equipment`
- `GET /api/equipment/{id}/history`
- `GET/POST /api/issues`
- `GET /api/issues/{id}`
- `POST /api/issues/{id}/triage`
- `GET /api/issues/{id}/audit`
- `POST /api/issues/{id}/findings`
- `GET /api/documents`
- `GET /api/work-orders`
- `GET /api/work-orders/{id}`
- `PUT /api/work-orders/{id}`
- `POST /api/work-orders/{id}/submit`
- `POST /api/work-orders/{id}/decision/{approve|reject}`

OpenAPI provides complete request schemas and response details at `/docs`.

## Architecture

- `app/main.py`: API, SQLite schema, deterministic rules, retrieval, optional Ollama adapter, workflow transitions and audit.
- `static/`: responsive browser UI.
- `tests/`: rule and workflow tests.
- `data/`: generated local database (not committed).

## Honest limitations / production hardening backlog

This is a focused MVP, not a certified production deployment:
- Authentication is **not implemented**. The demo uses caller-supplied `X-Actor` / `X-Role` headers; these are not secure and can be spoofed. Production must replace them with verified identity tokens and server-derived roles before exposing the app to users.
- SQLite is used for zero-friction local setup; use PostgreSQL, migrations and a managed backup/recovery plan for multi-user production.
- Retrieval is keyword scoring, not embeddings/pgvector; document upload, OCR and ingestion review UI are not implemented.
- AI local model adapter has limited output validation and is not guaranteed to be grounded. Verify every suggestion and implement citation validation, prompt-injection defenses, evaluation datasets and model version controls before deployment.
- Audit rows are protected from ordinary UI edits but SQLite file/database administrators can modify them; production needs stronger tamper-evidence and access controls.
- Approval is a demo workflow, not a legal or site-specific authorization process.
- No live IoT, predictive maintenance, inventory, dispatch, equipment commands or remote actuation.
- No real manufacturer manuals are included.
- No automatic priority policy can substitute for a site's safety procedures.

## Hiring demo talking points

Explain why deterministic threshold logic is separate from AI; why hypotheses differ from findings; how missing/conflicting sensor data is handled; how a work-order revision prevents lost updates; how server-side authorization blocks unauthorized approval; and what would need to change before a real industrial deployment.
