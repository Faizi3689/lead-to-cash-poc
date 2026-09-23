# Requirements coverage

Every numbered requirement of the assignment, and where it is implemented, plus how to verify it.
`§` refers to the section of the technical evaluation brief.

| § | Requirement | Implementation | Evidence / how to check |
|---|---|---|---|
| 1 | End-to-end flow: inquiry → AI → qualification → CRM → rules → approval → appointment → quote/order → invoice → exception → resolution → audit | n8n workflow 01 + the FastAPI service | `.\scripts\demo_flow.ps1`, or the n8n execution view |
| 2 | Webhook/API intake; LLM turns text into validated structured data (contact, intent, product, quantity, discount, delivery timing, appointment, confidence) | `app/extraction/prompt.py`, `ExtractedInquiry`, `app/services/extraction_service.py` | `POST /v1/inquiries/{id}/extract`; `tests/test_extraction.py`, `tests/test_validation.py` |
| 2 | Incomplete / malformed / ambiguous / low-confidence output handled | Retries with feedback, schema validation, grounding check, confidence threshold → `needs_review` + exception | `[[mock:invalid_json]]`, `[[mock:low_confidence]]`, `[[mock:hallucinate]]`; 13 extraction tests |
| 2 | AI has no authority to execute commercial transactions | AI output is a proposal only; rules and approvals decide; DB triggers enforce | `tests/test_extraction.py::test_prompt_injection_is_treated_as_data` |
| 3 | Deterministic rules separate from the LLM (≤5 / >5-15 / >15 / unknown product / low confidence) | `app/rules/engine.py` (`discount-rules-v1`), plus documented improvements: 40% policy ceiling, >50,000 escalation, >10,000 units review | `tests/test_rules_engine.py` (every boundary), README §3 |
| 4 | Approval workflow with approve / reject / **modify**, and the approved value reaching downstream | `app/services/approval_service.py`, n8n approval form; quote/order/invoice built from the approval + `terms_hash` | `tests/test_approvals.py`, `tests/test_documents.py`, `db/verify_controls.sql` |
| 5 | Persistence: customer, lead, inquiry, AI interpretation, qualification, approval, quote, order, appointment, invoice, exceptions, workflow status | 14 tables in `db/001_schema.sql` (+ 002-008) | Supabase table editor; `GET /v1/inquiries/{id}/trail` |
| 6 | Appointment: process request, mock appointment, linked to customer, confirmation | `app/services/appointment_service.py` | `POST /v1/inquiries/{id}/appointment`; `tests/test_documents.py` |
| 7 | Quote/order and invoice with customer, product, quantity, unit price, approved discount, tax, amount, date, **payment status** | `app/services/document_service.py`, `financial_service.record_payment` | `tests/test_documents.py`, `tests/test_payments_and_contact.py` |
| 8 | Exception scenarios: duplicate, amount mismatch, unauthorised discount, expense over threshold, missing information | `app/rules/financial_checks.py`, `app/services/financial_service.py` | `.\scripts\demo_exceptions.ps1`; `tests/test_financial*.py` |
| 9 | One AI use in the invoice/expense stage; AI does not authorise | (a) invoice text → structured data (`/v1/invoices/from-text`); (b) plain-English explanation of a finding, written by the OpenAI node in n8n and stored as advisory text | `tests/test_financial.py` (AI capture), `tests/test_payments_and_contact.py::test_ai_explanation_is_advisory` |
| 10 | Meaningful Python service: REST design, data models, validation, error handling, logging, configuration, dependencies, tests, no hard-coded secrets | `app/` (routers, services, rules, schemas, config, logging), `requirements.txt`, `.env.example` | `/docs`, `api/openapi.json`, 134 tests |
| 11 | n8n as the orchestration layer: webhooks, AI calls, Python API calls, branching, human approval, persistence, error handling, retries, downstream actions, evidence logging | `n8n/workflows/*.json` (5 workflows) | n8n executions; `n8n/README.md` |
| 12 | Failure handling: LLM unavailable, malformed AI response, Python service unavailable, database unavailable, duplicate webhook, approval timeout, downstream failure; no duplicate / unauthorised / partial transactions | Idempotency keys, retries, per-step transactions, timeouts + sweep, `/v1/ops/workflow-failures`, DB uniqueness and triggers | README §7 matrix; `docs/demo_checklist.md` failure injection table |
| 13 | Audit/evidence: request → AI → rule → approval → approved parameters → executed values → exception → resolution, with event id, actor, timestamp | `app/audit.py`, `audit_log` (append-only), `app/services/trail_service.py` | `GET /v1/inquiries/{id}/trail`; `tests/test_trail.py` |
| 14 | Security discussion for production | README §8 (10-point table) | - |
| 15 | Private repo with workflows, code, AI integration, API definitions/sample payloads, schema, tests, config, `.env.example`, architecture diagram, README, setup, assumptions, limitations; incremental commits | This repository | `api/openapi.json`, `samples/`, `docs/architecture.md`, `git log` |
| 16 | AI-assisted development disclosure | README §13 | - |
| 17 | End-to-end demonstration incl. the 20% → 12% and the tampered-invoice scenarios | `scripts/demo_flow.ps1`, `scripts/demo_exceptions.ps1`, `scripts/n8n_demo.ps1` | `docs/demo_checklist.md` |
| 18 | Live changes and debugging | Configurable thresholds, mock failure markers, suspend/resume friendly (idempotent) | `docs/demo_checklist.md` |
| 19 | Timebox; document what remains | README §12 "Known limitations" with effort estimates | - |

## Naming note (§5)
The brief distinguishes *lead* and *qualification*; this implementation keeps them on the inquiry rather than
duplicating entities: an **inquiry row is the lead** (customer, message, status, extracted data) and the
**qualification is the `rule_decisions` row** (outcome, reasons, rule version) plus the inquiry status
(`extracted`, `awaiting_approval`, `approved`, `rejected`, `needs_review`). One less table, same information,
and the decision history is preserved.
