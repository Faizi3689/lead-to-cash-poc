"""Generates the n8n workflow JSON files (kept in the repo so the workflows are reviewable as code).
Run: python n8n/build/build_workflows.py"""
import json
import uuid
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "workflows"
API = "https://lead-to-cash-api.onrender.com"
_ns = uuid.UUID("7b0c0e2a-5f3c-4b8e-9a51-0d1f2e3c4b5a")


def nid(name):
    return str(uuid.uuid5(_ns, name))


def node(name, type_, version, pos, params, **extra):
    n = {"parameters": params, "id": nid(name), "name": name, "type": type_, "typeVersion": version,
         "position": list(pos)}
    n.update(extra)
    return n


def http(name, pos, method, url, body=None, headers=None, timeout=60000, retries=3, on_error="continueErrorOutput"):
    p = {"method": method, "url": url, "authentication": "genericCredentialType",
         "genericAuthType": "httpHeaderAuth", "options": {"timeout": timeout}}
    hdrs = [{"name": "X-Request-ID", "value": "={{ $('Config').item.json.request_id }}"}] + (headers or [])
    p["sendHeaders"] = True
    p["headerParameters"] = {"parameters": hdrs}
    if body is not None:
        p.update(sendBody=True, specifyBody="json", jsonBody=body)
    extra = {"onError": on_error}
    if retries > 1:
        extra.update(retryOnFail=True, maxTries=retries, waitBetweenTries=3000)
    return node(name, "n8n-nodes-base.httpRequest", 4.2, pos, p, **extra)


def setnode(name, pos, fields, include_other=False):
    return node(name, "n8n-nodes-base.set", 3.4, pos, {
        "assignments": {"assignments": [
            {"id": nid(name + k), "name": k, "value": v, "type": t} for k, v, t in fields]},
        "includeOtherFields": include_other, "options": {}})


def respond(name, pos, body, code=200):
    return node(name, "n8n-nodes-base.respondToWebhook", 1.1, pos,
                {"respondWith": "json", "responseBody": body, "options": {"responseCode": code}})


def cond(left, op, right=None, type_="string"):
    c = {"id": nid(left + op + str(right)), "leftValue": left, "operator": {"type": type_, "operation": op}}
    if right is not None:
        c["rightValue"] = right
    if op in ("notEmpty", "empty", "true", "false", "exists", "notExists"):
        c["operator"]["singleValue"] = True
    return c


def if_node(name, pos, conditions, combinator="and"):
    return node(name, "n8n-nodes-base.if", 2, pos, {
        "conditions": {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose"},
                       "conditions": conditions, "combinator": combinator},
        "looseTypeValidation": True, "options": {}})


def noop(name, pos):
    return node(name, "n8n-nodes-base.noOp", 1, pos, {})


def config(pos, extra=()):
    return setnode("Config", pos, [("api_base_url", API, "string"),
                                   ("request_id", "=n8n-{{ $execution.id }}", "string"), *extra],
                   include_other=True)


def connect(pairs):
    """pairs: (from, to) or (from, to, output_index)."""
    conns = {}
    for p in pairs:
        src, dst, idx = (p + (0,))[:3] if len(p) == 2 else p
        outs = conns.setdefault(src, {"main": []})["main"]
        while len(outs) <= idx:
            outs.append([])
        outs[idx].append({"node": dst, "type": "main", "index": 0})
    return conns


def workflow(name, nodes, pairs, error_workflow_note=True):
    names = {n["name"] for n in nodes}
    for p in pairs:
        assert p[0] in names and p[1] in names, f"bad connection {p}"
    return {"name": name, "nodes": nodes, "connections": connect(pairs),
            "settings": {"executionOrder": "v1", "saveManualExecutions": True,
                         "saveDataErrorExecution": "all", "saveDataSuccessExecution": "all"},
            "pinData": {}}


INQ = "$('Create Inquiry').item.json.inquiry_id"

# ============================================================================ 01 Lead-to-Cash
wf1_nodes = [
    node("Webhook", "n8n-nodes-base.webhook", 2, (0, 300),
         {"httpMethod": "POST", "path": "inquiry", "responseMode": "responseNode",
          "options": {"allowedOrigins": "*"}},
         webhookId="l2c-inquiry"),
    config((200, 300), [("idempotency_key",
                         "={{ $json.body.message_id ? 'msg-' + $json.body.message_id : "
                         "'sha-' + JSON.stringify($json.body).hash('sha256') }}", "string")]),
    http("Create Inquiry", (400, 300), "POST", "={{ $('Config').item.json.api_base_url }}/v1/inquiries",
         body="={{ JSON.stringify({ message: $('Webhook').item.json.body.message, "
              "customer_name: $('Webhook').item.json.body.customer_name || null, "
              "customer_email: $('Webhook').item.json.body.customer_email || null, source: 'webhook' }) }}",
         headers=[{"name": "Idempotency-Key", "value": "={{ $('Config').item.json.idempotency_key }}"}]),
    respond("Respond Intake Error", (600, 520),
            "={{ { request_id: $('Config').item.json.request_id, stage: 'intake', "
            "error: $json.error?.message || $json.error || 'unknown' } }}", 502),
    http("AI Extract", (600, 300), "POST",
         f"={{{{ $('Config').item.json.api_base_url }}}}/v1/inquiries/{{{{ {INQ} }}}}/extract",
         timeout=120000, retries=2),
    respond("Respond Extraction Error", (800, 520),
            f"={{{{ {{ request_id: $('Config').item.json.request_id, stage: 'extraction', inquiry_id: {INQ}, "
            "error: $json.error?.message || $json.error || 'unknown', "
            "note: 'Inquiry is stored; extraction can be retried safely.' } }}", 502),
    if_node("Extracted?", (800, 300), [cond("={{ $json.outcome }}", "equals", "extracted")]),
    respond("Respond Needs Review", (1000, 520),
            "={{ { request_id: $('Config').item.json.request_id, inquiry_id: $json.inquiry_id, "
            "status: $json.status, outcome: 'needs_review', reasons: $json.review_reasons, "
            "next: 'A person will review this inquiry (exception queue).' } }}", 200),
    http("Decide", (1000, 300), "POST",
         f"={{{{ $('Config').item.json.api_base_url }}}}/v1/inquiries/{{{{ {INQ} }}}}/decide"),
    respond("Respond Decision Error", (1200, 520),
            f"={{{{ {{ request_id: $('Config').item.json.request_id, stage: 'decision', inquiry_id: {INQ}, "
            "error: $json.error?.message || $json.error || 'unknown' } }}", 502),
    respond("Respond Accepted", (1200, 300),
            "={{ { request_id: $('Config').item.json.request_id, inquiry_id: $json.inquiry_id, "
            "status: $json.status, outcome: $json.outcome, reasons: $json.reasons, "
            "attempts: $('AI Extract').item.json.attempts, confidence: $('AI Extract').item.json.confidence, "
            "product: $('AI Extract').item.json.product, rule_version: $json.rule_version, "
            "extracted: $('AI Extract').item.json.extracted, requested_terms: $json.amounts, "
            "approval: $json.approval ? { approval_id: $json.approval.approval_id, "
            "required_role: $json.approval.required_role, status: $json.approval.status, "
            "expires_at: $json.approval.expires_at } : null, "
            "approval_form_url: ($json.approval && $json.approval.status === 'pending') ? $execution.resumeFormUrl : null "
            "} }}", 200),
    node("Route by Outcome", "n8n-nodes-base.switch", 3, (1400, 300), {
        "rules": {"values": [
            {"conditions": {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose"},
                            "conditions": [cond("={{ $('Decide').item.json.outcome }}", "equals", "auto_approve")],
                            "combinator": "and"}, "renameOutput": True, "outputKey": "auto approved"},
            {"conditions": {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose"},
                            "conditions": [cond("={{ $('Decide').item.json.approval?.status }}", "equals", "pending")],
                            "combinator": "and"}, "renameOutput": True, "outputKey": "needs approval"},
        ]},
        "options": {"fallbackOutput": "extra", "renameFallbackOutput": "closed"}}),
    noop("Closed (rejected or review)", (1600, 560)),
    setnode("Notify Approver (demo)", (1600, 160), [
        ("to_role", "={{ $('Decide').item.json.approval.required_role }}", "string"),
        ("subject", f"=Approval needed: {{{{ $('Decide').item.json.amounts.discount_pct }}}}% discount on "
                    f"{{{{ $('Decide').item.json.amounts.quantity }}}} units", "string"),
        ("form_url", "={{ $execution.resumeFormUrl }}", "string"),
        ("note", "Replace this node with Gmail / Slack / Teams to deliver the link to the approver.", "string")]),
    node("Approval Form", "n8n-nodes-base.wait", 1.1, (1800, 160), {
        "resume": "form",
        "formTitle": "Discount approval",
        "formDescription": "={{ 'Customer asked for ' + $('Decide').item.json.amounts.discount_pct + '% on ' + "
                           "$('Decide').item.json.amounts.quantity + ' x ' + $('AI Extract').item.json.product.name + "
                           "' (list ' + $('Decide').item.json.amounts.subtotal + ' ' + $('Decide').item.json.amounts.currency + "
                           "', total at requested discount ' + $('Decide').item.json.amounts.total + '). ' + "
                           "'Required approver: ' + $('Decide').item.json.approval.required_role + '. ' + "
                           "($json.policy_message ? 'PREVIOUS ATTEMPT REFUSED: ' + $json.policy_message : '') }}",
        "formFields": {"values": [
            {"fieldLabel": "Decision", "fieldType": "dropdown", "requiredField": True,
             "fieldOptions": {"values": [{"option": "approve"}, {"option": "modify"}, {"option": "reject"}]}},
            {"fieldLabel": "Approved discount %", "fieldType": "number"},
            {"fieldLabel": "Your name", "requiredField": True},
            {"fieldLabel": "Comment", "fieldType": "textarea"}]},
        "limitWaitTime": True, "limitType": "afterTimeInterval", "resumeAmount": 48, "resumeUnit": "hours",
        "options": {}}, webhookId=nid("approval-form")),
    if_node("Decision Submitted?", (2000, 160), [cond("={{ $json['Decision'] }}", "notEmpty")]),
    http("Expire Overdue Approvals", (2200, 360), "POST",
         "={{ $('Config').item.json.api_base_url }}/v1/approvals/expire-due", retries=3),
    http("Submit Decision", (2200, 60), "POST",
         "={{ $('Config').item.json.api_base_url }}/v1/approvals/{{ $('Decide').item.json.approval.approval_id }}/decision",
         body="={{ JSON.stringify({ action: $json['Decision'], token: $('Decide').item.json.approval_token, "
              "decided_by: $json['Your name'], comment: $json['Comment'] || null, "
              "approved_discount_pct: $json['Decision'] === 'modify' ? String($json['Approved discount %']) : null }) }}"),
    setnode("Refused by Policy", (2400, -120), [
        ("policy_message", "={{ $json.error?.message || 'decision refused' }}", "string")]),
    if_node("Granted?", (2400, 60), [cond("={{ ['approved','modified'].includes($json.approval.status) }}",
                                         "true", type_="boolean")]),
    noop("Rejected by Approver", (2600, 200)),
    http("Create Quote", (2800, 300), "POST",
         f"={{{{ $('Config').item.json.api_base_url }}}}/v1/inquiries/{{{{ {INQ} }}}}/quote"),
    http("Create Order", (3000, 300), "POST",
         "={{ $('Config').item.json.api_base_url }}/v1/quotes/{{ $json.quote_id }}/order"),
    http("Create Invoice", (3200, 300), "POST",
         "={{ $('Config').item.json.api_base_url }}/v1/orders/{{ $json.order_id }}/invoice"),
    http("Validate Invoice", (3400, 300), "POST",
         "={{ $('Config').item.json.api_base_url }}/v1/invoices/{{ $json.invoice_id }}/validate"),
    if_node("Call Requested?", (3600, 300), [cond("={{ $('AI Extract').item.json.extracted.appointment_requested }}",
                                                 "true", type_="boolean")]),
    http("Book Appointment", (3800, 200), "POST",
         f"={{{{ $('Config').item.json.api_base_url }}}}/v1/inquiries/{{{{ {INQ} }}}}/appointment",
         retries=2, on_error="continueRegularOutput"),
    http("Consistency Check", (4000, 300), "GET",
         f"={{{{ $('Config').item.json.api_base_url }}}}/v1/inquiries/{{{{ {INQ} }}}}/documents"),
    http("Report Failure", (3400, 560), "POST",
         "={{ $('Config').item.json.api_base_url }}/v1/ops/workflow-failures",
         body=f"={{{{ JSON.stringify({{ workflow: $workflow.name, stage: $prevNode.name, "
              f"error: $json.error?.message || JSON.stringify($json.error || {{}}), execution_id: $execution.id, "
              f"inquiry_id: {INQ} }}) }}}}", retries=3, on_error="continueRegularOutput"),
]
wf1 = workflow("01 - Lead-to-Cash (Inquiry -> Approval -> Documents)", wf1_nodes, [
    ("Webhook", "Config"), ("Config", "Create Inquiry"),
    ("Create Inquiry", "AI Extract", 0), ("Create Inquiry", "Respond Intake Error", 1),
    ("AI Extract", "Extracted?", 0), ("AI Extract", "Respond Extraction Error", 1),
    ("Extracted?", "Decide", 0), ("Extracted?", "Respond Needs Review", 1),
    ("Decide", "Respond Accepted", 0), ("Decide", "Respond Decision Error", 1),
    ("Respond Accepted", "Route by Outcome"),
    ("Route by Outcome", "Create Quote", 0), ("Route by Outcome", "Notify Approver (demo)", 1),
    ("Route by Outcome", "Closed (rejected or review)", 2),
    ("Notify Approver (demo)", "Approval Form"), ("Approval Form", "Decision Submitted?"),
    ("Decision Submitted?", "Submit Decision", 0), ("Decision Submitted?", "Expire Overdue Approvals", 1),
    ("Submit Decision", "Granted?", 0), ("Submit Decision", "Refused by Policy", 1),
    ("Refused by Policy", "Approval Form"),
    ("Granted?", "Create Quote", 0), ("Granted?", "Rejected by Approver", 1),
    ("Create Quote", "Create Order", 0), ("Create Quote", "Report Failure", 1),
    ("Create Order", "Create Invoice", 0), ("Create Order", "Report Failure", 1),
    ("Create Invoice", "Validate Invoice", 0), ("Create Invoice", "Report Failure", 1),
    ("Validate Invoice", "Call Requested?", 0), ("Validate Invoice", "Report Failure", 1),
    ("Call Requested?", "Book Appointment", 0), ("Call Requested?", "Consistency Check", 1),
    ("Book Appointment", "Consistency Check"),
])

# ============================================================================ 02 Approval timeout sweep
wf2 = workflow("02 - Approval Timeout Sweep (every 15 min)", [
    node("Every 15 Minutes", "n8n-nodes-base.scheduleTrigger", 1.2, (0, 0),
         {"rule": {"interval": [{"field": "minutes", "minutesInterval": 15}]}}),
    config((200, 0)),
    http("Expire Overdue Approvals", (400, 0), "POST",
         "={{ $('Config').item.json.api_base_url }}/v1/approvals/expire-due", timeout=90000),
    if_node("Anything Expired?", (600, 0), [cond("={{ $json.expired.length }}", "gt", 0, type_="number")]),
    setnode("Notify Sales Ops (demo)", (800, -100), [
        ("message", "={{ $json.expired.length + ' approval(s) timed out and moved to the review queue' }}", "string"),
        ("approval_ids", "={{ $json.expired.join(', ') }}", "string")]),
    noop("Nothing to do", (800, 100)),
], [("Every 15 Minutes", "Config"), ("Config", "Expire Overdue Approvals"),
    ("Expire Overdue Approvals", "Anything Expired?", 0),
    ("Anything Expired?", "Notify Sales Ops (demo)", 0), ("Anything Expired?", "Nothing to do", 1)])

# ============================================================================ 03 Invoice intake
IKEY = ("idempotency_key", "={{ $json.body.submission_id ? 'sub-' + $json.body.submission_id : "
                           "'sha-' + JSON.stringify($json.body).hash('sha256') }}", "string")
wf3 = workflow("03 - Invoice Intake & Validation", [
    node("Webhook", "n8n-nodes-base.webhook", 2, (0, 200),
         {"httpMethod": "POST", "path": "invoice", "responseMode": "responseNode",
          "options": {"allowedOrigins": "*"}},
         webhookId="l2c-invoice"),
    config((200, 200), [IKEY]),
    if_node("Raw Text?", (400, 200), [cond("={{ $('Webhook').item.json.body.raw_text }}", "notEmpty")]),
    http("AI Capture Invoice", (600, 100), "POST",
         "={{ $('Config').item.json.api_base_url }}/v1/invoices/from-text",
         body="={{ JSON.stringify({ raw_text: $('Webhook').item.json.body.raw_text }) }}",
         headers=[{"name": "Idempotency-Key", "value": "={{ $('Config').item.json.idempotency_key }}"}],
         timeout=120000, retries=2),
    http("Submit Invoice", (600, 300), "POST", "={{ $('Config').item.json.api_base_url }}/v1/invoices",
         body="={{ JSON.stringify(Object.fromEntries(Object.entries($('Webhook').item.json.body).filter(e => e[0] !== 'submission_id'))) }}",
         headers=[{"name": "Idempotency-Key", "value": "={{ $('Config').item.json.idempotency_key }}"}]),
    respond("Respond Error", (800, 480),
            "={{ { request_id: $('Config').item.json.request_id, error: $json.error?.message || $json.error || 'unknown' } }}", 502),
    respond("Respond Result", (900, 200),
            "={{ { request_id: $('Config').item.json.request_id, invoice_id: $json.entity_id, status: $json.status, "
            "replay: $json.replay, ai: $json.ai, exceptions: $json.open_exceptions.map(e => ({ id: e.exception_id, "
            "type: e.exception_type, severity: e.severity, message: e.message, overridable: e.overridable })) } }}", 200),
    if_node("Blocked?", (1100, 200), [cond("={{ $json.status }}", "equals", "blocked")]),
    node("AI Explain (OpenAI)", "n8n-nodes-base.httpRequest", 4.2, (1300, 60), {
        "method": "POST", "url": "https://api.openai.com/v1/chat/completions",
        "authentication": "predefinedCredentialType", "nodeCredentialType": "openAiApi",
        "sendBody": True, "specifyBody": "json",
        "jsonBody": "={{ JSON.stringify({ model: 'gpt-4o-mini', temperature: 0, messages: ["
                    "{ role: 'system', content: 'You explain finance exceptions to a finance officer in plain "
                    "English. Two sentences maximum. State what is wrong and what a person should check. You never "
                    "approve, clear or downgrade anything.' }, "
                    "{ role: 'user', content: 'Invoice ' + ($json.document.invoice_number || $json.entity_id) + "
                    "' was blocked. Findings: ' + JSON.stringify($json.open_exceptions.map(e => "
                    "({ type: e.exception_type, message: e.message, details: e.details }))) } ] }) }}",
        "options": {"timeout": 60000}},
        retryOnFail=True, maxTries=2, waitBetweenTries=3000, onError="continueRegularOutput"),
    http("Save Explanation", (1500, 60), "POST",
         "={{ $('Config').item.json.api_base_url }}/v1/exceptions/"
         "{{ $('Blocked?').item.json.open_exceptions[0].exception_id }}/explanation",
         body="={{ JSON.stringify({ explanation: $json.choices ? $json.choices[0].message.content : "
              "'AI explanation unavailable', source: 'ai:' + ($json.model || 'unknown') }) }}",
         retries=2, on_error="continueRegularOutput"),
    setnode("Notify Finance (demo)", (1700, 60), [
        ("message", "={{ 'Invoice ' + ($('Blocked?').item.json.document.invoice_number || "
                    "$('Blocked?').item.json.entity_id) + ' blocked: ' + "
                    "$('Blocked?').item.json.open_exceptions.map(e => e.exception_type).join(', ') }}", "string"),
        ("ai_explanation", "={{ $json.ai_explanation || '' }}", "string"),
        ("review_url", "={{ $('Config').item.json.api_base_url }}/docs", "string")]),
    noop("Validated", (1300, 300)),
], [("Webhook", "Config"), ("Config", "Raw Text?"),
    ("Raw Text?", "AI Capture Invoice", 0), ("Raw Text?", "Submit Invoice", 1),
    ("AI Capture Invoice", "Respond Result", 0), ("AI Capture Invoice", "Respond Error", 1),
    ("Submit Invoice", "Respond Result", 0), ("Submit Invoice", "Respond Error", 1),
    ("Respond Result", "Blocked?"),
    ("Blocked?", "AI Explain (OpenAI)", 0), ("AI Explain (OpenAI)", "Save Explanation"),
    ("Save Explanation", "Notify Finance (demo)"), ("Blocked?", "Validated", 1)])

# ============================================================================ 99 Error handler
wf99 = workflow("99 - Error Handler", [
    node("Error Trigger", "n8n-nodes-base.errorTrigger", 1, (0, 0), {}),
    config((200, 0)),
    http("Record Failure", (400, 0), "POST", "={{ $('Config').item.json.api_base_url }}/v1/ops/workflow-failures",
         body="={{ JSON.stringify({ workflow: $('Error Trigger').item.json.workflow.name, "
              "stage: $('Error Trigger').item.json.execution.lastNodeExecuted, "
              "error: $('Error Trigger').item.json.execution.error?.message, "
              "execution_id: String($('Error Trigger').item.json.execution.id || ''), "
              "execution_url: $('Error Trigger').item.json.execution.url }) }}",
         retries=3, on_error="continueRegularOutput"),
], [("Error Trigger", "Config"), ("Config", "Record Failure")])

for fname, wf in [("01_lead_to_cash.json", wf1), ("02_approval_timeout_sweep.json", wf2),
                  ("03_invoice_intake.json", wf3), ("99_error_handler.json", wf99)]:
    (OUT / fname).write_text(json.dumps(wf, indent=2), encoding="utf-8")
    print("wrote", fname, len(wf["nodes"]), "nodes")
