# AI Lead-to-Cash & Exception Management PoC

Technical evaluation PoC: customer inquiry → AI extraction → deterministic rules → human approval →
appointment → quote/order → invoice → invoice/expense exception handling → audit trail.

**Principle:** n8n orchestrates; the Python service validates, decides (deterministic rules) and records.
The LLM only *proposes* — it never authorises commercial or financial actions.

## Stack
- n8n (n8n Cloud for development; workflows are plain JSON exports and import into self-hosted n8n too)
- Python 3.11+ / FastAPI — validation, rules engine, persistence, exception detection
- Supabase (Postgres) — CRM / transaction store / audit log
- OpenAI (switchable, with `mock` mode for deterministic tests & demos)

## Setup (Windows / Git Bash)
```bash
python -m venv .venv
source .venv/Scripts/activate
pip install -r requirements.txt
cp .env.example .env        # fill in DATABASE_URL, API_KEY, OPENAI_API_KEY
uvicorn app.main:app --reload --port 8000
pytest -q
```
Check: http://localhost:8000/health and http://localhost:8000/health/db

## Exposing the API to n8n Cloud (development)
n8n Cloud cannot reach `localhost`, so expose the API with a Cloudflare quick tunnel:
```bash
cloudflared tunnel --url http://localhost:8000
```
Put the printed `https://....trycloudflare.com` URL into the **Config** node of each workflow.
All non-health endpoints require the `X-API-Key` header (n8n Header Auth credential).

## Repository layout
- `app/` — FastAPI service
- `db/` — schema & seed SQL
- `n8n/workflows/` — exported n8n workflows
- `samples/` — sample payloads & invoice scenarios
- `tests/` — automated tests
- `docs/` — architecture diagram, design notes

## Sections to complete
- Architecture diagram
- Business rules & reasoning
- Failure handling
- Security (production approach)
- Assumptions / Known limitations
- AI-assisted development disclosure
- ## Database
Apply migrations (safe to re-run):
    python -m scripts.apply_migrations
Verify controls (run in Supabase SQL Editor or psql) — all rows should be PASS:
    db/verify_controls.sql
