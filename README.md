# AI Lead-to-Cash & Exception Management PoC

A working proof of concept of an AI-assisted **lead-to-cash** pipeline with **financial exception handling**:

> customer message -> AI extraction -> deterministic rules -> human approval (approve / modify / reject)
> -> appointment -> quote -> order -> invoice -> invoice/expense validation -> block / correct / reprocess
> -> full audit trail

**Core principle: AI proposes, rules and humans decide, the database enforces.**
The LLM never authorises money. Discounts, approvals and invoice acceptance are decided by tested,
deterministic code and by people, and Postgres itself refuses any document that differs from what was approved.

| | |
|---|---|
| Orchestration | n8n (Cloud; workflows are JSON in `n8n/workflows/` and import into self-hosted n8n too) |
| Service | Python 3.12, FastAPI, SQLAlchemy 2, Pydantic 2 |
| Database | Supabase Postgres (plain Postgres connection; triggers, constraints, RLS) |
| AI | OpenAI (`gpt-4o-mini`), switchable to a deterministic mock for tests and failure simulation |
| Hosting | Render (API), n8n Cloud (workflows) |
| Tests | 128 automated tests (`pytest`) |

Architecture, diagrams and control matrix: **[docs/architecture.md](docs/architecture.md)**.
Live-review script and failure injection: **[docs/demo_checklist.md](docs/demo_checklist.md)**.
n8n setup: **[n8n/README.md](n8n/README.md)**.

---

## 1. Quick start (Windows / PowerShell)
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env            # fill in DATABASE_URL, API_KEY (and OPENAI_API_KEY for LLM_PROVIDER=openai)
python -m scripts.apply_migrations     # creates the schema (safe to re-run)
uvicorn app.main:app --reload --port 8000
```
Open http://127.0.0.1:8000/docs -> **Authorize** with your `API_KEY`.

End-to-end demo against the running API:
```powershell
.\scripts\demo_flow.ps1          # 20% requested -> manager modifies to 12% -> quote/order/invoice/appointment -> trail
.\scripts\demo_exceptions.ps1    # tampered invoice, duplicate vs retry, expense over threshold
```
Verify the database-level controls (Supabase SQL editor or psql): run `db/verify_controls.sql` - every row reads `PASS`.

### Tests
```powershell
$env:TEST_DATABASE_URL = "<a Postgres URL with the migrations applied>"
pytest -q
```
Every database test runs inside a transaction that is rolled back, and uses unique names, so tests never leave
data behind and are not affected by existing data. Without `TEST_DATABASE_URL` the database tests are skipped
and the pure unit tests (rules, validation, pricing, financial checks) still run.
Recommendation: point `TEST_DATABASE_URL` at a separate database, not the demo one.

## 2. Configuration
| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | - | Postgres URL (Supabase *Session pooler*; `postgresql://` is accepted and converted) |
| `API_KEY` | - | Shared key required in the `X-API-Key` header on every `/v1` endpoint |
| `LLM_PROVIDER` | `mock` | `openai` or `mock` |
| `OPENAI_API_KEY`, `OPENAI_MODEL` | -, `gpt-4o-mini` | OpenAI access |
| `LLM_TIMEOUT_SECONDS`, `LLM_MAX_RETRIES`, `LLM_RETRY_BACKOFF_SECONDS` | 20, 2, 1 | Per-call timeout; 1 + 2 retries with linear backoff |
| `LLM_MIN_CONFIDENCE` | 0.75 | Below this, extraction goes to human review |
| `BUSINESS_TIMEZONE` | `UTC` | Appointment slots (e.g. `Asia/Karachi`) |
| `QUOTE_VALID_DAYS`, `INVOICE_DUE_DAYS` | 14, 30 | Document dates |
| `INVOICE_APPROVAL_THRESHOLD`, `EXPENSE_APPROVAL_THRESHOLD`, `RECEIPT_REQUIRED_ABOVE` | 100000, 1000, 25 | Financial controls |
| `MOCK_CALENDAR_FAIL` | `false` | Simulate the calendar service being down |

## 3. Business rules (`app/rules/engine.py`, version `discount-rules-v1`)
| Requested discount | Outcome |
|---|---|
| <= 5% | automatic approval (recorded as `system:discount-rules-v1`) |
| > 5% and <= 15% | salesperson approval |
| > 15% and <= 40% | manager approval |
| > 40% | automatic rejection (outside policy) |

Additional guard rails: order total > 50,000 -> manager even for small discounts; quantity > 10,000 -> human review;
unknown product or missing quantity -> human review. Boundaries are inclusive (5% is automatic, 15% is salesperson)
and covered by tests. Every decision stores its inputs, reasons and rule version.

Approval rules: approvers may only **reduce** the requested discount and only within their role's authority
(salesperson <= 15%, manager <= 40%); decisions use a one-time token (only its SHA-256 hash is stored) and
expire after 48 h into the review queue. A decided approval is frozen by a database trigger.

**Why these choices:** the thresholds come from the brief; the 40% ceiling and value/quantity escalations
show how policy limits sit outside anyone's individual authority. Rules are plain Python constants so a
reviewer can read and change them in one place, and they are versioned in every decision record.

## 4. How "approved = executed" is guaranteed
1. The approval stores the exact terms and a `terms_hash` (product, quantity, unit price, discount, currency).
2. Quote, order and invoice are **built from the approval**, never from the customer's request.
3. Postgres triggers re-check every quote against its approval and every order against its quote.
   A document with different terms cannot be inserted or updated - `db/verify_controls.sql` proves it.
4. Invoices are validated against the order: a higher discount is an `unauthorized_discount`
   (critical, not overridable) and the invoice cannot be approved.
5. `GET /v1/inquiries/{id}/documents` shows all documents side by side with a `consistent` verdict.

## 5. AI usage (and its limits)
| Where | What the AI does | Guardrails |
|---|---|---|
| Inquiry extraction | message -> product, quantity, discount, timing, appointment wish, confidence | strict JSON schema; up to 3 attempts with error feedback; every number must appear in the message (hallucination check); product matched deterministically; confidence threshold; message treated as untrusted data (prompt-injection safe) |
| Invoice capture | invoice text -> invoice fields | same schema + grounding approach; result goes through the same deterministic checks as a manual invoice |

The AI never approves, prices, discounts or clears an exception. When it is unavailable or unsure, the item is
stored and routed to a person with an exception; a later re-run can clear only system failures
(outage / invalid output), never a low-confidence result. Every attempt is stored in `ai_runs`
(input, raw output, parsed output, validation errors, model, prompt version, latency).

## 6. Financial exception handling
| Exception | Detected when | Resolution |
|---|---|---|
| `unauthorized_discount` | invoice discount > approved, or > 5% with no approval | correct (not overridable) |
| `amount_mismatch` | totals/discount/line items do not add up, or total differs from the order | correct (not overridable) |
| `missing_field` | required fields missing, receipt missing, unknown order reference | correct |
| `duplicate_invoice` | same supplier + number (exact) or same supplier + amount + date (suspected) | accept with reason, or void |
| `over_threshold` | invoice > 100,000 / expense > 1,000 | accept (sign-off) with reason |
| `low_confidence`, `llm_unavailable`, `invalid_ai_output` | AI capture problems | human corrects or confirms |

Flow: submit -> validate -> **blocked** -> correct (`/correct`, revalidated immediately) or accept or void
-> **validated** -> approve. An acceptance is bound to a hash of the document data: if the data changes later, the
finding returns. Retried submissions (same `Idempotency-Key`) return the same record; a genuinely new submission
of the same invoice is stored and flagged.

## 7. Failure handling
| Failure | Behaviour |
|---|---|
| Duplicate webhook / n8n retry | `Idempotency-Key` + `INSERT ... ON CONFLICT`; same key + different body -> 409 |
| LLM timeout / outage / bad key | retried (non-retryable errors stop at once) -> `needs_review` + `llm_unavailable`; recoverable by re-running `/extract` |
| Malformed or invalid AI output | retried with feedback -> `invalid_ai_output` -> human review |
| API down / slow (Render cold start) | n8n retries 3x with generous timeouts; every endpoint is idempotent, so retries never duplicate |
| Failure mid-chain (e.g. after the quote) | each step is its own transaction; re-running continues where it stopped; failures after retries open a `downstream_failure` exception via `/v1/ops/workflow-failures` |
| Approval never answered | 48 h expiry, sweep workflow every 15 min -> `approval_timeout` exception |
| Calendar down | 503 + exception; quote/order continue; retry books the slot and clears it |
| Concurrent requests | row locks (`SELECT ... FOR UPDATE`), advisory lock for calendar slots |
| Unhandled workflow error | n8n error workflow 99 records it in the audit log |

## 8. Security
**In this PoC:** API key on all business endpoints (constant-time comparison); secrets only in environment
variables (never in code, repo or workflow exports); Supabase RLS enabled on every table with no policies, so the
public Supabase REST API exposes nothing; approval tokens are random, single-use in effect, expiring, and stored
only as hashes; strict request validation (`extra="forbid"`, length and range limits); errors never leak internals
(full detail only in server logs, tied to a `request_id`); append-only audit log; the LLM receives only the message
text, treated as data.

**Production approach (not implemented here):** per-client credentials or OAuth2/OIDC with roles instead of one
shared key, and approver identity from SSO rather than a typed name; secrets in a vault with rotation; a
least-privilege database role for the service instead of the owner role; network restrictions (private
networking / IP allow-lists between n8n and the API); rate limiting and request size limits at the edge; PII
minimisation and retention rules for messages and AI logs, plus a data-processing agreement with the LLM
provider; signed webhooks from upstream systems; monitoring and alerting on the exception queue and error rates.

## 9. API overview
| Method | Path | Purpose |
|---|---|---|
| POST | `/v1/inquiries` | intake (requires `Idempotency-Key`) |
| POST | `/v1/inquiries/{id}/extract` | AI extraction with guardrails |
| POST | `/v1/inquiries/{id}/decide` | deterministic rules -> approval record |
| GET / POST | `/v1/approvals/{id}`, `/v1/approvals/{id}/decision` | view / approve, modify, reject (token) |
| POST | `/v1/approvals/expire-due` | timeout sweep |
| POST | `/v1/inquiries/{id}/quote`, `/v1/quotes/{id}/order`, `/v1/orders/{id}/invoice` | documents from the approval |
| POST | `/v1/inquiries/{id}/appointment` | mock calendar booking |
| GET | `/v1/inquiries/{id}/documents` | consistency verdict |
| GET | `/v1/inquiries/{id}/trail` | full evidence trail + audit timeline |
| POST | `/v1/invoices`, `/v1/invoices/from-text` | submit / AI-capture and validate |
| POST | `/v1/invoices/{id}/validate`, `/correct`, `/approve`, `/void` | exception loop |
| POST | `/v1/expenses`, `/v1/expenses/{id}/correct`, `/approve`, `/reject` | expense loop |
| GET / POST | `/v1/exceptions`, `/v1/exceptions/{id}/accept` | review queue / override |
| POST | `/v1/ops/workflow-failures` | failure reporting from n8n |

Full schemas at `/docs`.

## 10. Repository layout
```
app/            FastAPI service
  routers/      HTTP layer (thin)
  services/     workflows: inquiries, extraction, approvals, documents, appointments, financial, trail
  rules/        pure deterministic rules (discount policy, financial checks)
  extraction/   prompts, schemas, validation/grounding for AI output
  llm/          provider interface, OpenAI client, deterministic mock
db/             numbered SQL migrations (source of truth) + verify_controls.sql
n8n/            workflow exports, generator script, setup guide
scripts/        migration runner, PowerShell demo scripts
tests/          128 tests (unit + API/database)
docs/           architecture, demo checklist
samples/        sample inquiries and invoice texts
```

## 11. Assumptions
* One product per inquiry; single currency per document; tax is zero in generated invoices.
* "Salesperson" and "manager" are roles, identified by the name typed in the approval form.
* The customer accepts a quote when the order endpoint is called (no customer portal).
* Appointments: weekdays 10:00-16:00 in `BUSINESS_TIMEZONE`, 30-minute slots, one per inquiry.
* An invoice discount *lower* than approved is not a violation (customer pays more than the floor).
* The approval thresholds apply to the discount percentage requested in the message.

## 12. Known limitations
* Render free tier sleeps after 15 min: the first call can take up to a minute (warm up before demos;
  n8n timeouts are set generously).
* One shared API key; no user accounts or role-based access in the API.
* Calendar, accounting posting and notifications are mocked (n8n "demo" Set nodes mark where email/Slack go).
* The mock LLM is regex-based and only understands the demo phrasings; the real behaviour comes from OpenAI.
* No credit notes / partial invoices / multi-line orders; no currency conversion.
* Approval-form links live inside one n8n execution; if that execution is deleted, the approval must be
  decided through the API.
* Tests run against a real Postgres (no in-memory fake) - accurate, but slower over long distances.

## 13. AI-assisted development
This project was built with the help of an AI assistant (Claude) for planning, scaffolding, code, tests and
documentation. I directed the design decisions, ran every step locally and on Render/n8n, fixed issues that came
up during integration (connection pooling, Windows specifics, n8n configuration, test isolation), and reviewed the
code so I can explain and change any part of it during the review.
