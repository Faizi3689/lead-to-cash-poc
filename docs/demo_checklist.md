# Live review checklist (15-20 min)

## Before the call (10 min before)
- [ ] Open `https://lead-to-cash-api.onrender.com/health` (wakes Render's free tier) and `/health/db`
- [ ] Render env: `LLM_PROVIDER=openai`, valid `OPENAI_API_KEY`
- [ ] n8n: workflows 01, 02, 03 active; 99 set as error workflow
- [ ] Terminal in the project folder, venv active; `.env` present
- [ ] Browser tabs: `/docs`, n8n Executions, Supabase table editor (`audit_log`, `exceptions`), GitHub repo

## Suggested flow
| Min | Show | Command / action | Point to make |
|---|---|---|---|
| 0-2 | Architecture | `docs/architecture.md` | AI proposes, rules + humans decide, DB enforces |
| 2-6 | Happy path with modify | `.\scripts\n8n_demo.ps1 -N8nBase <n8n>` -> open form -> modify 12 | Webhook answers fast; token never exposed |
| 6-8 | Evidence | n8n execution -> Consistency Check; `GET /v1/inquiries/{id}/trail` | request -> AI -> rule -> approval -> executed |
| 8-10 | DB guarantee | Supabase SQL editor: run `db/verify_controls.sql` | 10 PASS rows: approved = executed at DB level |
| 10-14 | Exceptions | `.\scripts\demo_exceptions.ps1 -Base <render> -ApiKey <key>` | tampered invoice blocked, not overridable; duplicate vs retry |
| 14-16 | Tests | `pytest -q` (or show last CI/terminal run) | 128 tests incl. boundaries 5 / 15 / 40 % |
| 16-20 | Questions / failure injection | see below | |

## Failure injection they may ask for
| Ask | How | Expected |
|---|---|---|
| "Send the same webhook twice" | run the same `n8n_demo.ps1` body twice (same `message_id`) | same `inquiry_id`, `duplicate: true` |
| "What if the LLM is down?" | Render: set a wrong `OPENAI_API_KEY` (or mock + `[[mock:timeout]]`) | 3 attempts in `ai_runs`, inquiry -> `needs_review`, `llm_unavailable` exception; fix key, re-run `/extract` -> clears |
| "What if the AI hallucinates?" | mock: `[[mock:hallucinate]]` | grounding check -> `needs_review` |
| "Salesperson tries to give 20%" | form: modify to a value above the role limit | form comes back with the refusal reason |
| "Nobody approves" | set an approval's `expires_at` in the past, run workflow 02 | `expired`, `approval_timeout` exception |
| "Kill the API mid-flow" | suspend the Render service, run the demo | n8n retries 3x -> error branch -> after resume, re-run: idempotent, no duplicates |
| "Calendar down" | `MOCK_CALENDAR_FAIL=true` | 503 + exception; quote/order not blocked; retry books slot |
| "Change the invoice to 20%" | `POST /v1/invoices/{id}/correct` | blocked: `unauthorized_discount` (critical) + `amount_mismatch` |
| "Approve it anyway" | `POST /v1/invoices/{id}/approve`, or SQL `update invoices set status='approved'` | 409 / database exception |

## Where to look while debugging live
* n8n Executions -> red node -> input/output
* Render -> Logs (JSON lines with `request_id` = `n8n-<execution id>`)
* `GET /v1/exceptions` (review queue), `GET /v1/inquiries/{id}/trail`
* Supabase: `ai_runs`, `audit_log`, `exceptions`
