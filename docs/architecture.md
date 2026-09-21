# Architecture

## Principle
**AI proposes, rules and humans decide, the database enforces.**

* **n8n** orchestrates: webhooks, sequencing, retries, human approval form, timeouts, error workflow.
* **Python (FastAPI)** owns every decision: validation, deterministic business rules, pricing,
  approval authority, exception detection, audit.
* **Postgres (Supabase)** is the last line of defence: triggers and constraints make "approved = executed",
  duplicate prevention and append-only audit impossible to bypass, even by buggy application code.
* **The LLM** (OpenAI, or a deterministic mock) only turns unstructured text into a *proposal*, which is
  schema-validated and grounded against the source text before anything uses it.

## Components
```mermaid
flowchart LR
    C[Customer / email / form] -->|POST /webhook/inquiry| N1
    S[Supplier / finance] -->|POST /webhook/invoice| N3
    subgraph n8n [n8n Cloud]
        N1[01 Lead-to-Cash]
        N2[02 Approval timeout sweep<br/>every 15 min]
        N3[03 Invoice intake]
        N9[99 Error handler]
        F[[Approval form<br/>Wait node, 48 h]]
    end
    N1 --- F
    A((Approver)) -->|approve / modify / reject| F
    subgraph api [FastAPI service - Render]
        R[Routers + API-key auth]
        E[Extraction + validation]
        RU[Rules engine<br/>discount-rules-v1]
        AP[Approval service]
        D[Quote / order / invoice]
        X[Financial checks<br/>exception engine]
        AU[Audit + trail]
    end
    N1 & N2 & N3 & N9 -->|HTTPS + X-API-Key<br/>Idempotency-Key| R
    E -->|chat completions| L[(OpenAI / mock)]
    R --> E & RU & AP & D & X & AU
    E & RU & AP & D & X & AU --> DB[(Supabase Postgres<br/>triggers, constraints,<br/>RLS, append-only audit)]
```

## Lead-to-cash sequence (20% requested, manager approves 12%)
```mermaid
sequenceDiagram
    participant Cu as Customer
    participant N as n8n (01)
    participant API as FastAPI
    participant AI as LLM
    participant DB as Postgres
    participant M as Manager
    Cu->>N: POST /webhook/inquiry
    N->>API: POST /v1/inquiries (Idempotency-Key)
    API->>DB: insert ... on conflict do nothing + audit
    N->>API: POST /extract
    API->>AI: prompt (message as untrusted data)
    AI-->>API: JSON proposal
    API->>API: parse, schema-validate, ground numbers, resolve product
    N->>API: POST /decide
    API->>API: rules: 20% > 15% -> manager
    API->>DB: rule_decision + pending approval (token hash, expiry)
    N-->>Cu: 200 {outcome: manager_approval, approval_form_url}
    N->>M: approval form link
    M->>N: modify -> 12%
    N->>API: POST /approvals/{id}/decision (token)
    API->>DB: approval frozen with 12% + terms_hash
    N->>API: quote -> order -> invoice -> validate
    DB-->>DB: triggers verify each document equals the approval
    N->>API: GET /documents -> consistent: true
```

## State machines
```mermaid
stateDiagram-v2
    [*] --> received
    received --> extracting
    extracting --> extracted
    extracting --> needs_review: low confidence / unknown product /<br/>missing data / AI down
    extracted --> approved: <= 5% (auto)
    extracted --> awaiting_approval: > 5%
    extracted --> rejected: > 40%
    extracted --> needs_review: missing data / oversized order
    awaiting_approval --> approved: approve / modify
    awaiting_approval --> rejected: reject
    awaiting_approval --> needs_review: timeout (48 h)
    approved --> quoted --> ordered --> invoiced
```
```mermaid
stateDiagram-v2
    [*] --> received: inbound invoice
    [*] --> draft: generated invoice
    received --> validated
    draft --> validated
    received --> blocked
    draft --> blocked
    blocked --> blocked: correct (still failing)
    blocked --> validated: correct / accept overridable finding
    blocked --> void: confirmed duplicate
    validated --> approved: DB refuses if any exception is open
```

## Where each control lives
| Control | Enforced in |
|---|---|
| Quote/order must equal approved terms | Postgres triggers `quotes_enforce_approval`, `orders_enforce_quote` + `terms_hash` |
| Approver can only reduce, within role authority | Service check + DB constraint `approvals_approved_not_above_requested` |
| Decided approval cannot change | Trigger `approvals_freeze_decided` |
| No duplicate inquiry / quote / order / invoice per order | Unique indexes + `INSERT ... ON CONFLICT` |
| Blocked invoice/expense cannot be approved | Trigger `block_if_open_exceptions` |
| Audit cannot be edited | Trigger `audit_log_append_only` (update, delete, truncate) |
| Public Supabase REST API cannot read data | RLS enabled on every table, no policies |
| AI output cannot be trusted blindly | Pydantic schema, retries with feedback, grounding check, confidence threshold |
