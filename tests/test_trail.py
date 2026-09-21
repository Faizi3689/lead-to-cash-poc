import uuid

from tests.conftest import API_HEADERS

MSG = "We need 200 units of Product X next month, can you give us 20% discount? I'd like to talk tomorrow."


def _post(client, path, **kw):
    return client.post(path, headers=API_HEADERS, **kw)


def test_trail_tells_the_whole_story(client):
    uid = uuid.uuid4().hex[:8]
    iid = client.post("/v1/inquiries", json={"message": MSG, "customer_email": f"t-{uid}@x.example",
                                             "customer_name": f"Trail {uid}"},
                      headers={**API_HEADERS, "Idempotency-Key": f"tr-{uid}"}).json()["inquiry_id"]
    _post(client, f"/v1/inquiries/{iid}/extract")
    d = _post(client, f"/v1/inquiries/{iid}/decide").json()
    _post(client, f"/v1/approvals/{d['approval']['approval_id']}/decision",
          json={"action": "modify", "token": d["approval_token"], "decided_by": "maria.manager",
                "approved_discount_pct": "12", "comment": "volume too low for 20%"})
    q = _post(client, f"/v1/inquiries/{iid}/quote").json()
    o = _post(client, f"/v1/quotes/{q['quote_id']}/order").json()
    inv = _post(client, f"/v1/orders/{o['order_id']}/invoice").json()
    # Someone tampers with the invoice, finance fixes it.
    _post(client, f"/v1/invoices/{inv['invoice_id']}/correct",
          json={"changes": {"discount_pct": "20", "discount_amount": "2000.00", "total": "8000.00"},
                "corrected_by": "eve.editor", "reason": "customer asked"})
    _post(client, f"/v1/invoices/{inv['invoice_id']}/correct",
          json={"changes": {"discount_pct": "12", "discount_amount": "1200.00", "total": "8800.00"},
                "corrected_by": "fiona.finance", "reason": "restore approved terms"})

    t = client.get(f"/v1/inquiries/{iid}/trail", headers=API_HEADERS).json()
    s = t["story"]
    assert s["1_request"]["message"] == MSG
    assert s["2_ai_output"]["attempts"][0]["outcome"] == "valid"
    assert s["2_ai_output"]["extracted"]["requested_discount_pct"] == "20.0"
    assert s["3_rule_decision"]["outcome"] == "manager_approval"
    assert s["4_approval"][0]["decided_by"] == "maria.manager"
    assert s["4_approval"][0]["approved_discount_pct"] == "12.00"
    assert s["5_executed"]["documents"]["invoice"]["discount_pct"] == "12.00"
    types = {e["type"]: e for e in s["6_exceptions"]}
    assert types["unauthorized_discount"]["status"] == "resolved"
    assert types["unauthorized_discount"]["resolved_by"] == "user:fiona.finance"
    assert t["consistent"] is True

    actions = [e["action"] for e in t["timeline"]]
    order = ["inquiry.received", "ai.extraction_extracted", "rule.decided", "approval.modified",
             "quote.issued", "order.confirmed", "invoice.generated", "invoice.corrected", "invoice.blocked",
             "exception.resolved", "invoice.validated"]
    positions = [actions.index(a) for a in order]
    assert positions == sorted(positions), actions


def test_trail_404(client):
    assert client.get(f"/v1/inquiries/{uuid.uuid4()}/trail", headers=API_HEADERS).status_code == 404
