# n8n workflows

| File | Trigger | What it does |
|---|---|---|
| `00_connectivity_check.json` | Webhook `POST /l2c-ping` | n8n -> API connectivity test (auth, retries, failure path) |
| `01_lead_to_cash.json` | Webhook `POST /inquiry` | Intake -> AI extraction -> rules -> **responds immediately** -> approval form (Wait, 48 h timeout) -> quote -> order -> invoice -> invoice validation -> appointment -> consistency check |
| `02_approval_timeout_sweep.json` | Every 15 min | Expires overdue approvals into the review queue |
| `03_invoice_intake.json` | Webhook `POST /invoice` | Invoice submission (structured JSON or raw text read by AI) -> validation -> finance notification when blocked |
| `99_error_handler.json` | Error Trigger | Any unhandled workflow failure is recorded in the API (audit log / exception queue) |

## Design rules
* **n8n orchestrates, the API decides.** No business rule lives in n8n: thresholds, validation, pricing
  and approval authority are all in the Python service, where they are tested.
* **Every HTTP call is idempotent**, so every node can retry (3 tries, 3 s apart) without creating duplicates.
  Inquiries use an `Idempotency-Key` built from the message hash (or `message_id` when the sender supplies one).
* **The webhook answers fast.** Workflow 01 replies right after the rules decision; approval and document
  generation continue in the same execution. The reply contains `approval_form_url` when a human must decide.
* **The approval token never leaves n8n + API.** It is kept in the execution data and sent only with the
  decision; the customer-facing reply and the form do not contain it.
* **Failures are never silent.** Error outputs of the document steps call `/v1/ops/workflow-failures`
  (opens a `downstream_failure` exception on the inquiry); anything else is caught by workflow 99.
* **Nothing waits forever.** The approval form times out after 48 h and workflow 02 sweeps expired approvals.

## Setup (n8n Cloud or self-hosted)
1. **Credential:** Header Auth named `L2C API Key` - header `X-API-Key`, value = the API's `API_KEY`.
2. **Import** each JSON (Workflows -> Import from File).
3. In every **HTTP Request** node select the `L2C API Key` credential (credentials are never exported).
4. In every **Config** node check `api_base_url` (your Render URL, no trailing `/`).
5. Import `99_error_handler.json`, then in workflows 01, 02 and 03 open **Settings -> Error workflow**
   and choose `99 - Error Handler`.
6. **Activate** 01, 02, 03 and 99 (production webhook URLs only work while active).

`n8n/build/build_workflows.py` regenerates these files; after editing in the n8n UI, export and overwrite instead.

## Try it
```powershell
.\scripts\n8n_demo.ps1 -N8nBase "https://<you>.app.n8n.cloud"            # production URLs (workflow active)
.\scripts\n8n_demo.ps1 -N8nBase "https://<you>.app.n8n.cloud" -Test      # test URLs (click "Execute workflow" first)
```
The reply includes `approval_form_url`: open it, choose **modify**, enter **12**, submit. Then open the
execution in n8n to watch quote -> order -> invoice -> validation -> appointment -> consistency check run.
