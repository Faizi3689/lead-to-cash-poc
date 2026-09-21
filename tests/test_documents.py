import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.config import get_settings
from app.models import AuditLog, ExceptionRecord, Inquiry, Quote
from app.services import appointment_service
from tests.conftest import API_HEADERS

MSG_20 = "We need 200 units of Product X next month, can you give us 20% discount? I'd like to talk tomorrow."
MSG_3 = "Please send 100 units of Product X with 3% discount."
MSG_Q = "We want 100 units of Product Q at 10% off."


def _post(client, path, **kw):
    return client.post(path, headers=API_HEADERS, **kw)


def _approved(client, message=MSG_20, modify_to="12"):
    """intake -> extract -> decide (-> manager modifies). Returns inquiry id."""
    r = client.post("/v1/inquiries", json={"message": message},
                    headers={**API_HEADERS, "Idempotency-Key": f"d-{uuid.uuid4()}"})
    iid = r.json()["inquiry_id"]
    _post(client, f"/v1/inquiries/{iid}/extract")
    decision = _post(client, f"/v1/inquiries/{iid}/decide").json()
    if decision.get("approval_token"):
        _post(client, f"/v1/approvals/{decision['approval']['approval_id']}/decision",
              json={"action": "modify", "token": decision["approval_token"],
                    "decided_by": "maria.manager", "approved_discount_pct": modify_to})
    return iid


def _full_chain(client, iid):
    quote = _post(client, f"/v1/inquiries/{iid}/quote").json()
    order = _post(client, f"/v1/quotes/{quote['quote_id']}/order").json()
    invoice = _post(client, f"/v1/orders/{order['order_id']}/invoice").json()
    return quote, order, invoice


# ----------------------------------------------------------------------------- the core promise

def test_modified_discount_flows_through_every_document(client):
    iid = _approved(client, MSG_20, modify_to="12")
    quote, order, invoice = _full_chain(client, iid)

    for doc in (quote, order, invoice):
        assert doc["discount_pct"] == "12.00", doc          # NOT the 20% the customer asked for
        assert doc["total"] == "8800.00"
    assert quote["terms_hash"] == order["terms_hash"] == invoice["terms_hash"]
    assert quote["quote_number"].startswith("Q-") and order["order_number"].startswith("SO-")
    assert invoice["invoice_number"].startswith("INV-") and invoice["status"] == "draft"

    summary = client.get(f"/v1/inquiries/{iid}/documents", headers=API_HEADERS).json()
    assert summary["consistent"] is True
    assert summary["requested_discount_pct"] == "20.0"
    assert summary["documents"]["approval"]["discount_pct"] == "12.00"
    assert summary["inquiry_status"] == "invoiced"


def test_auto_approved_order_chain(client):
    iid = _approved(client, MSG_3)
    quote, order, invoice = _full_chain(client, iid)
    assert quote["discount_pct"] == order["discount_pct"] == invoice["discount_pct"] == "3.00"
    assert invoice["total"] == "4850.00"
    assert invoice["line_items"][0]["sku"] == "PRD-X"


def test_database_rejects_a_quote_that_differs_from_the_approval(client, db_session):
    """Even if application code were buggy, the DB trigger refuses unapproved terms."""
    iid = _approved(client, MSG_20, modify_to="12")
    quote = _post(client, f"/v1/inquiries/{iid}/quote").json()
    with pytest.raises(DBAPIError) as exc:
        with db_session.begin_nested():
            db_session.execute(text("update quotes set discount_pct = 20, discount_amount = 2000, "
                                    "total = 8000 where id = :id"), {"id": quote["quote_id"]})
    assert "do not match approved terms" in str(exc.value)


# ----------------------------------------------------------------------------- state checks

def test_cannot_quote_without_approval(client):
    r = client.post("/v1/inquiries", json={"message": MSG_20},
                    headers={**API_HEADERS, "Idempotency-Key": f"nq-{uuid.uuid4()}"})
    iid = r.json()["inquiry_id"]
    _post(client, f"/v1/inquiries/{iid}/extract")
    _post(client, f"/v1/inquiries/{iid}/decide")            # pending manager approval, not decided
    out = _post(client, f"/v1/inquiries/{iid}/quote")
    assert out.status_code == 409 and "no granted approval" in out.json()["detail"]


def test_rejected_inquiry_cannot_be_quoted(client):
    iid = _approved(client, "We need 100 units of Product X with 90% discount.")
    assert _post(client, f"/v1/inquiries/{iid}/quote").status_code == 409


def test_expired_quote_cannot_become_an_order(client, db_session):
    iid = _approved(client, MSG_3)
    quote = _post(client, f"/v1/inquiries/{iid}/quote").json()
    db_session.execute(text("update quotes set valid_until = :d where id = :id"),
                       {"d": date.today() - timedelta(days=1), "id": quote["quote_id"]})
    r = _post(client, f"/v1/quotes/{quote['quote_id']}/order")
    assert r.status_code == 409 and "expired" in r.json()["detail"]


# ----------------------------------------------------------------------------- idempotency

def test_every_step_is_idempotent(client, db_session):
    iid = _approved(client, MSG_20)
    q1, o1, i1 = _full_chain(client, iid)
    q2, o2, i2 = _full_chain(client, iid)                     # n8n retries the whole chain
    assert (q1["quote_id"], o1["order_id"], i1["invoice_id"]) == (q2["quote_id"], o2["order_id"], i2["invoice_id"])
    assert q2["replay"] and o2["replay"] and i2["replay"]
    n = db_session.execute(select(Quote).where(Quote.inquiry_id == uuid.UUID(iid))).all()
    assert len(n) == 1


def test_documents_are_audited(client, db_session):
    iid = _approved(client, MSG_20)
    _full_chain(client, iid)
    actions = db_session.execute(select(AuditLog.action).where(AuditLog.inquiry_id == uuid.UUID(iid))
                                 .order_by(AuditLog.id)).scalars().all()
    for expected in ("inquiry.received", "ai.extraction_extracted", "rule.decided", "approval.modified",
                     "quote.issued", "order.confirmed", "invoice.generated"):
        assert expected in actions, expected
    assert actions.index("approval.modified") < actions.index("quote.issued")


# ----------------------------------------------------------------------------- appointments

def test_target_day_interpretation():
    wed = datetime(2026, 9, 23, 9, 0, tzinfo=timezone.utc)      # a Wednesday
    fri = datetime(2026, 9, 25, 9, 0, tzinfo=timezone.utc)
    assert appointment_service.target_day("tomorrow", wed) == date(2026, 9, 24)
    assert appointment_service.target_day("tomorrow", fri) == date(2026, 9, 28)   # skips the weekend
    assert appointment_service.target_day("next week", wed) == date(2026, 9, 28)
    assert appointment_service.target_day("friday", wed) == date(2026, 9, 25)
    assert appointment_service.target_day(None, wed) == date(2026, 9, 24)


def test_appointment_booked_from_customer_words(client):
    iid = _approved(client, MSG_20)
    r = _post(client, f"/v1/inquiries/{iid}/appointment")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "confirmed" and body["requested_text"] == "tomorrow"
    assert body["external_ref"].startswith("MOCK-CAL-")
    start = datetime.fromisoformat(body["scheduled_start"])
    assert start > datetime.now(timezone.utc) and start.weekday() < 5
    again = _post(client, f"/v1/inquiries/{iid}/appointment").json()
    assert again["replay"] is True and again["appointment_id"] == body["appointment_id"]


def test_two_customers_never_get_the_same_slot(client):
    a = _post(client, f"/v1/inquiries/{_approved(client, MSG_20)}/appointment").json()
    b = _post(client, f"/v1/inquiries/{_approved(client, MSG_20)}/appointment").json()
    assert a["scheduled_start"] != b["scheduled_start"]


def test_no_appointment_when_not_requested(client):
    iid = _approved(client, MSG_3)                              # message does not ask for a call
    r = _post(client, f"/v1/inquiries/{iid}/appointment")
    assert r.status_code == 409 and "did not ask" in r.json()["detail"]


def test_calendar_outage_is_handled_and_recovers(client, db_session, monkeypatch):
    iid = _approved(client, MSG_20)
    monkeypatch.setattr(get_settings(), "mock_calendar_fail", True)
    r = _post(client, f"/v1/inquiries/{iid}/appointment")
    assert r.status_code == 503 and "retry" in r.json()["detail"]
    open_types = db_session.execute(select(ExceptionRecord.exception_type).where(
        ExceptionRecord.entity_id == uuid.UUID(iid), ExceptionRecord.status == "open")).scalars().all()
    assert "downstream_failure" in open_types

    # The quote/order flow is NOT blocked by the calendar being down.
    assert _post(client, f"/v1/inquiries/{iid}/quote").status_code == 200

    monkeypatch.setattr(get_settings(), "mock_calendar_fail", False)
    assert _post(client, f"/v1/inquiries/{iid}/appointment").json()["status"] == "confirmed"
    still_open = db_session.execute(select(ExceptionRecord.exception_type).where(
        ExceptionRecord.entity_id == uuid.UUID(iid), ExceptionRecord.status == "open")).scalars().all()
    assert "downstream_failure" not in still_open


def test_unknown_ids_404(client):
    assert _post(client, f"/v1/inquiries/{uuid.uuid4()}/quote").status_code == 404
    assert _post(client, f"/v1/quotes/{uuid.uuid4()}/order").status_code == 404
    assert _post(client, f"/v1/orders/{uuid.uuid4()}/invoice").status_code == 404
    assert client.get(f"/v1/inquiries/{uuid.uuid4()}/documents", headers=API_HEADERS).status_code == 404
