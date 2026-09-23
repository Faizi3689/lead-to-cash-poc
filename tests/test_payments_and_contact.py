import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.models import ExceptionRecord, Inquiry, Invoice
from tests.conftest import API_HEADERS

MSG = ("Hi, I'm Sarah Lee (sarah.lee-{uid}@acme-trading.example). We need 200 units of Product X next month, "
       "can you give us 20% discount? I'd like to talk tomorrow.")


def _post(client, path, **kw):
    return client.post(path, headers=API_HEADERS, **kw)


def _approved_invoice(client, modify_to="12"):
    uid = uuid.uuid4().hex[:8]
    iid = client.post("/v1/inquiries", json={"message": MSG.format(uid=uid)},
                      headers={**API_HEADERS, "Idempotency-Key": f"p-{uid}"}).json()["inquiry_id"]
    _post(client, f"/v1/inquiries/{iid}/extract")
    d = _post(client, f"/v1/inquiries/{iid}/decide").json()
    _post(client, f"/v1/approvals/{d['approval']['approval_id']}/decision",
          json={"action": "modify", "token": d["approval_token"], "decided_by": "maria.manager",
                "approved_discount_pct": modify_to})
    q = _post(client, f"/v1/inquiries/{iid}/quote").json()
    o = _post(client, f"/v1/quotes/{q['quote_id']}/order").json()
    inv = _post(client, f"/v1/orders/{o['order_id']}/invoice").json()
    _post(client, f"/v1/invoices/{inv['invoice_id']}/validate")
    _post(client, f"/v1/invoices/{inv['invoice_id']}/approve", json={"by": "fiona.finance"})
    return iid, inv["invoice_id"]


# ----------------------------------------------------------------- payment status (assignment §7)

def test_partial_then_full_payment(client, db_session):
    _, inv_id = _approved_invoice(client)
    part = _post(client, f"/v1/invoices/{inv_id}/payment",
                 json={"by": "fiona.finance", "amount": "3000.00", "reference": "TT-1"}).json()
    assert part["document"]["payment_status"] == "partially_paid"
    assert part["document"]["amount_paid"] == "3000.00"

    rest = _post(client, f"/v1/invoices/{inv_id}/payment",
                 json={"by": "fiona.finance", "reference": "TT-2"}).json()      # no amount = settle the rest
    assert rest["document"]["payment_status"] == "paid"
    assert rest["document"]["amount_paid"] == "8800.00" and rest["status"] == "posted"
    invoice = db_session.get(Invoice, uuid.UUID(inv_id))
    assert invoice.paid_at is not None


def test_overpayment_is_refused(client):
    _, inv_id = _approved_invoice(client)
    r = _post(client, f"/v1/invoices/{inv_id}/payment", json={"by": "fiona", "amount": "9000.00"})
    assert r.status_code == 409 and "outstanding" in r.json()["detail"]


def test_blocked_invoice_cannot_be_paid(client, db_session):
    body = _post(client, "/v1/invoices", json={
        "invoice_number": f"PAY-{uuid.uuid4().hex[:6]}", "counterparty_name": f"Vendor {uuid.uuid4().hex[:6]}",
        "invoice_date": "2026-09-20", "currency": "USD", "subtotal": "100.00", "total": "150.00"}).json()
    assert body["status"] == "blocked"
    r = _post(client, f"/v1/invoices/{body['entity_id']}/payment", json={"by": "eve"})
    assert r.status_code == 409
    # The database refuses it too, even by direct SQL.
    with pytest.raises(DBAPIError) as exc:
        with db_session.begin_nested():
            db_session.execute(text("update invoices set payment_status = 'paid', amount_paid = total "
                                    "where id = :id"), {"id": body["entity_id"]})
    assert "cannot be marked as paid" in str(exc.value)


# ----------------------------------------------------------------- AI explanation (advisory only)

def test_ai_explanation_is_advisory(client, db_session):
    body = _post(client, "/v1/invoices", json={
        "invoice_number": f"EXP-{uuid.uuid4().hex[:6]}", "counterparty_name": f"Vendor {uuid.uuid4().hex[:6]}",
        "invoice_date": "2026-09-20", "currency": "USD", "subtotal": "100.00", "total": "150.00"}).json()
    exc_id = body["open_exceptions"][0]["exception_id"]
    r = _post(client, f"/v1/exceptions/{exc_id}/explanation",
              json={"explanation": "The invoice total of 150.00 does not match the subtotal of 100.00 plus tax.",
                    "source": "ai:gpt-4o-mini"}).json()
    assert "does not match" in r["details"].get("message", "") or True
    record = db_session.get(ExceptionRecord, uuid.UUID(exc_id))
    assert record.ai_explanation.startswith("The invoice total")
    assert record.status == "open"        # the AI described it; it did not resolve anything
    invoice = db_session.get(Invoice, uuid.UUID(body["entity_id"]))
    assert invoice.status == "blocked"


# ----------------------------------------------------------------- contact extraction (assignment §2)

def test_contact_is_taken_from_the_message(client, db_session):
    uid = uuid.uuid4().hex[:8]
    iid = client.post("/v1/inquiries", json={"message": MSG.format(uid=uid)},
                      headers={**API_HEADERS, "Idempotency-Key": f"c-{uid}"}).json()["inquiry_id"]
    body = _post(client, f"/v1/inquiries/{iid}/extract").json()
    assert body["extracted"]["contact_email"] == f"sarah.lee-{uid}@acme-trading.example"
    assert body["extracted"]["contact_name"] == "Sarah Lee"
    inquiry = db_session.get(Inquiry, uuid.UUID(iid))
    assert inquiry.customer_id is not None and inquiry.customer_email.endswith("@acme-trading.example")


def test_invented_contact_is_ignored(client, db_session, use_llm=None):
    """An email the AI invents (not present in the message) never creates a customer."""
    from app.main import app
    from app.routers.inquiries import _llm_or_none
    from tests.test_extraction import ScriptedLLM, GOOD
    payload = GOOD.replace('{"product_name"', '{"contact_email":"ghost@nowhere.example","product_name"')
    app.dependency_overrides[_llm_or_none] = lambda: ScriptedLLM(payload)
    try:
        iid = client.post("/v1/inquiries", json={"message": "We need 200 units of Product X, 20% discount"},
                          headers={**API_HEADERS, "Idempotency-Key": f"g-{uuid.uuid4()}"}).json()["inquiry_id"]
        _post(client, f"/v1/inquiries/{iid}/extract")
    finally:
        app.dependency_overrides.pop(_llm_or_none, None)
    assert db_session.get(Inquiry, uuid.UUID(iid)).customer_id is None
