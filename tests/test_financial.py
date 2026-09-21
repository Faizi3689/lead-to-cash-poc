import uuid

from sqlalchemy import select, text

from app.models import ExceptionRecord, Invoice
from tests.conftest import API_HEADERS

MSG_20 = "We need 200 units of Product X next month, can you give us 20% discount? I'd like to talk tomorrow."


def _post(client, path, key=None, **kw):
    headers = {**API_HEADERS, **({"Idempotency-Key": key} if key else {})}
    return client.post(path, headers=headers, **kw)


def _invoice(**over):
    base = {"invoice_number": f"SUP-{uuid.uuid4().hex[:6]}", "counterparty_name": "Northwind Supplies",
            "invoice_date": "2026-09-20", "currency": "USD", "subtotal": "1000.00", "discount_pct": "5",
            "discount_amount": "50.00", "tax_amount": "0.00", "total": "950.00"}
    base.update(over)
    return base


def _types(body):
    return sorted(e["exception_type"] for e in body["open_exceptions"])


def _order_for_20_modified_to_12(client):
    r = client.post("/v1/inquiries", json={"message": MSG_20},
                    headers={**API_HEADERS, "Idempotency-Key": f"f-{uuid.uuid4()}"})
    iid = r.json()["inquiry_id"]
    _post(client, f"/v1/inquiries/{iid}/extract")
    d = _post(client, f"/v1/inquiries/{iid}/decide").json()
    _post(client, f"/v1/approvals/{d['approval']['approval_id']}/decision",
          json={"action": "modify", "token": d["approval_token"], "decided_by": "maria.manager",
                "approved_discount_pct": "12"})
    q = _post(client, f"/v1/inquiries/{iid}/quote").json()
    o = _post(client, f"/v1/quotes/{q['quote_id']}/order").json()
    inv = _post(client, f"/v1/orders/{o['order_id']}/invoice").json()
    return o, inv


# ----------------------------------------------------------------------------- generated invoices

def test_generated_invoice_validates_and_can_be_approved(client):
    _, inv = _order_for_20_modified_to_12(client)
    v = _post(client, f"/v1/invoices/{inv['invoice_id']}/validate").json()
    assert v["status"] == "validated" and v["open_exceptions"] == []
    a = _post(client, f"/v1/invoices/{inv['invoice_id']}/approve", json={"by": "fiona.finance"}).json()
    assert a["status"] == "approved"


def test_tampered_invoice_is_blocked_then_corrected(client, db_session):
    """Someone edits the invoice to the 20% the customer wanted. It must be stopped."""
    _, inv = _order_for_20_modified_to_12(client)
    iid = inv["invoice_id"]
    r = _post(client, f"/v1/invoices/{iid}/correct",
              json={"changes": {"discount_pct": "20", "discount_amount": "2000.00", "total": "8000.00"},
                    "corrected_by": "eve.editor", "reason": "customer asked for 20%"}).json()
    assert r["status"] == "blocked"
    assert _types(r) == ["amount_mismatch", "unauthorized_discount"]
    ua = next(e for e in r["open_exceptions"] if e["exception_type"] == "unauthorized_discount")
    assert ua["severity"] == "critical" and not ua["overridable"]
    assert "exceeds the approved 12.00%" in ua["message"]

    # Cannot be approved while blocked - neither through the API nor directly in the database.
    assert _post(client, f"/v1/invoices/{iid}/approve", json={"by": "fiona.finance"}).status_code == 409

    # Cannot be waved through: an unauthorised discount is not overridable.
    acc = _post(client, f"/v1/exceptions/{ua['exception_id']}/accept",
                json={"by": "fiona.finance", "reason": "looks fine"})
    assert acc.status_code == 409 and "cannot be accepted" in acc.json()["detail"]

    # Correct back to the approved terms -> reprocessed -> clean.
    fixed = _post(client, f"/v1/invoices/{iid}/correct",
                  json={"changes": {"discount_pct": "12", "discount_amount": "1200.00", "total": "8800.00"},
                        "corrected_by": "fiona.finance", "reason": "restore approved 12%"}).json()
    assert fixed["status"] == "validated" and fixed["open_exceptions"] == []
    resolved = db_session.execute(select(ExceptionRecord).where(
        ExceptionRecord.entity_id == uuid.UUID(iid), ExceptionRecord.status == "resolved")).scalars().all()
    assert {e.resolved_by for e in resolved} == {"user:fiona.finance"}
    assert _post(client, f"/v1/invoices/{iid}/approve", json={"by": "fiona.finance"}).json()["status"] == "approved"


def test_database_refuses_to_approve_a_blocked_invoice(client, db_session):
    body = _post(client, "/v1/invoices", json=_invoice(total="999.00")).json()
    assert body["status"] == "blocked"
    import pytest
    from sqlalchemy.exc import DBAPIError
    with pytest.raises(DBAPIError) as exc:
        with db_session.begin_nested():
            db_session.execute(text("update invoices set status = 'approved' where id = :id"),
                               {"id": body["entity_id"]})
    assert "unresolved exceptions" in str(exc.value)


# ----------------------------------------------------------------------------- submitted invoices

def test_clean_supplier_invoice(client):
    body = _post(client, "/v1/invoices", json=_invoice()).json()
    assert body["status"] == "validated" and body["open_exceptions"] == []


def test_inbound_invoice_with_more_discount_than_approved(client):
    order, _ = _order_for_20_modified_to_12(client)
    body = _post(client, "/v1/invoices", json=_invoice(
        order_reference=order["order_number"], subtotal="10000.00", discount_pct="20",
        discount_amount="2000.00", total="8000.00")).json()
    assert body["status"] == "blocked"
    assert "unauthorized_discount" in _types(body)
    assert body["document"]["order_id"] == order["order_id"]


def test_duplicate_invoice_detected_but_retry_is_not(client):
    inv = _invoice(invoice_number="SUP-DUP-1")
    first = _post(client, "/v1/invoices", key="k-1", json=inv).json()
    retry = _post(client, "/v1/invoices", key="k-1", json=inv).json()        # network retry
    assert retry["replay"] is True and retry["entity_id"] == first["entity_id"]
    assert first["status"] == "validated"

    second = _post(client, "/v1/invoices", key="k-2", json=inv).json()       # sent again by the supplier
    assert second["entity_id"] != first["entity_id"]
    assert second["status"] == "blocked" and _types(second) == ["duplicate_invoice"]
    assert second["open_exceptions"][0]["details"]["kind"] == "exact"


def test_suspected_duplicate_with_new_number(client):
    _post(client, "/v1/invoices", json=_invoice(counterparty_name="Contoso", total="950.00"))
    body = _post(client, "/v1/invoices", json=_invoice(counterparty_name="Contoso", total="950.00")).json()
    assert _types(body) == ["duplicate_invoice"]
    assert body["open_exceptions"][0]["severity"] == "medium"


def test_confirmed_duplicate_is_voided(client, db_session):
    inv = _invoice(invoice_number="SUP-DUP-2")
    _post(client, "/v1/invoices", json=inv)
    dup = _post(client, "/v1/invoices", json=inv).json()
    v = _post(client, f"/v1/invoices/{dup['entity_id']}/void",
              json={"by": "fiona.finance", "reason": "confirmed duplicate"}).json()
    assert v["status"] == "void"
    assert db_session.execute(select(ExceptionRecord).where(
        ExceptionRecord.entity_id == uuid.UUID(dup["entity_id"]),
        ExceptionRecord.status == "open")).first() is None


def test_missing_fields_then_corrected(client):
    body = _post(client, "/v1/invoices", json=_invoice(invoice_number=None, invoice_date=None)).json()
    assert body["status"] == "blocked" and _types(body) == ["missing_field"]
    assert body["open_exceptions"][0]["details"]["fields"] == ["invoice_date", "invoice_number"]
    fixed = _post(client, f"/v1/invoices/{body['entity_id']}/correct",
                  json={"changes": {"invoice_number": "SUP-FIX-1", "invoice_date": "2026-09-21"},
                        "corrected_by": "fiona.finance", "reason": "added from the PDF"}).json()
    assert fixed["status"] == "validated"


def test_partial_correction_keeps_remaining_issue_open(client):
    body = _post(client, "/v1/invoices", json=_invoice(invoice_number=None, total="999.00")).json()
    assert _types(body) == ["amount_mismatch", "missing_field"]
    step1 = _post(client, f"/v1/invoices/{body['entity_id']}/correct",
                  json={"changes": {"invoice_number": "SUP-P-1"}, "corrected_by": "fiona", "reason": "fix"}).json()
    assert step1["status"] == "blocked" and _types(step1) == ["amount_mismatch"]


def test_over_threshold_needs_finance_sign_off(client):
    body = _post(client, "/v1/invoices", json=_invoice(subtotal="150000.00", discount_pct="0",
                                                       discount_amount="0.00", total="150000.00")).json()
    assert _types(body) == ["over_threshold"]
    exc = body["open_exceptions"][0]
    assert exc["overridable"] is True
    assert _post(client, f"/v1/exceptions/{exc['exception_id']}/accept",
                 json={"by": "fiona.finance"}).status_code == 422          # a reason is mandatory
    ok = _post(client, f"/v1/exceptions/{exc['exception_id']}/accept",
               json={"by": "fiona.finance", "reason": "annual contract, budget approved"}).json()
    assert ok["status"] == "validated"
    # Revalidating does not re-open an accepted finding while the data is unchanged ...
    again = _post(client, f"/v1/invoices/{body['entity_id']}/validate").json()
    assert again["status"] == "validated"
    # ... but changing the amount invalidates the acceptance.
    changed = _post(client, f"/v1/invoices/{body['entity_id']}/correct",
                    json={"changes": {"subtotal": "160000.00", "total": "160000.00"},
                          "corrected_by": "eve.editor", "reason": "price change"}).json()
    assert _types(changed) == ["over_threshold"]


def test_approved_invoice_cannot_be_edited_or_voided(client):
    body = _post(client, "/v1/invoices", json=_invoice()).json()
    _post(client, f"/v1/invoices/{body['entity_id']}/approve", json={"by": "fiona"})
    assert _post(client, f"/v1/invoices/{body['entity_id']}/correct",
                 json={"changes": {"total": "1.00"}, "corrected_by": "eve", "reason": "x" * 5}).status_code == 409
    assert _post(client, f"/v1/invoices/{body['entity_id']}/void", json={"by": "eve"}).status_code == 409


def test_invalid_correction_payload_is_rejected(client):
    body = _post(client, "/v1/invoices", json=_invoice()).json()
    r = _post(client, f"/v1/invoices/{body['entity_id']}/correct",
              json={"changes": {"discount_pct": "150"}, "corrected_by": "eve", "reason": "typo"})
    assert r.status_code == 422
    r = _post(client, f"/v1/invoices/{body['entity_id']}/correct",
              json={"changes": {"status": "approved"}, "corrected_by": "eve", "reason": "sneaky"})
    assert r.status_code == 422                                          # status is not an editable field


# ----------------------------------------------------------------------------- AI invoice capture

INVOICE_TEXT = """Invoice No: SUP-7788
From: Northwind Supplies
Date: 2026-09-20
Currency: USD
Subtotal: 1,000.00
Discount (5%): -50.00
Tax: 0.00
Total Due: 950.00"""


def test_ai_reads_invoice_text_and_it_is_validated(client, db_session):
    body = _post(client, "/v1/invoices/from-text", json={"raw_text": INVOICE_TEXT}).json()
    assert body["ai"]["outcome"] == "extracted"
    assert body["document"]["invoice_number"] == "SUP-7788" and body["document"]["total"] == "950.00"
    assert body["document"]["source"] == "ai_extracted" and body["status"] == "validated"


def test_ai_extracted_invoice_still_gets_business_checks(client):
    text_20 = INVOICE_TEXT.replace("SUP-7788", "SUP-7789").replace("Discount (5%): -50.00", "Discount (20%): -200.00") \
        .replace("950.00", "800.00")
    body = _post(client, "/v1/invoices/from-text", json={"raw_text": text_20}).json()
    assert body["status"] == "blocked" and "unauthorized_discount" in _types(body)


def test_ai_outage_during_capture_goes_to_review(client):
    body = _post(client, "/v1/invoices/from-text", json={"raw_text": INVOICE_TEXT + "\n[[mock:timeout]]"}).json()
    assert body["ai"]["outcome"] == "needs_review" and body["status"] == "blocked"
    assert set(_types(body)) >= {"llm_unavailable", "missing_field"}
    # Revalidating does not hide the outage: only a human entering the data clears it.
    again = _post(client, f"/v1/invoices/{body['entity_id']}/validate").json()
    assert "llm_unavailable" in _types(again)
    fixed = _post(client, f"/v1/invoices/{body['entity_id']}/correct", json={
        "changes": {"invoice_number": "SUP-7790", "counterparty_name": "Northwind Supplies",
                    "invoice_date": "2026-09-20", "currency": "USD", "subtotal": "1000.00",
                    "discount_pct": "5", "discount_amount": "50.00", "tax_amount": "0.00", "total": "950.00"},
        "corrected_by": "fiona.finance", "reason": "typed in from the PDF"}).json()
    assert fixed["status"] == "validated", fixed["open_exceptions"]


# ----------------------------------------------------------------------------- expenses

def _expense(**over):
    base = {"employee_name": "Ali Raza", "category": "travel", "amount": "120.00", "currency": "USD",
            "expense_date": "2026-09-18", "description": "Taxi to client", "receipt_ref": "rcpt-001.jpg"}
    base.update(over)
    return base


def test_clean_expense_is_approved(client):
    body = _post(client, "/v1/expenses", json=_expense()).json()
    assert body["status"] == "validated"
    assert _post(client, f"/v1/expenses/{body['entity_id']}/approve",
                 json={"by": "mark.manager"}).json()["status"] == "approved"


def test_expense_over_threshold_and_missing_receipt(client):
    body = _post(client, "/v1/expenses", json=_expense(amount="1500.00", receipt_ref=None)).json()
    assert body["status"] == "blocked" and _types(body) == ["missing_field", "over_threshold"]
    fixed = _post(client, f"/v1/expenses/{body['entity_id']}/correct",
                  json={"changes": {"receipt_ref": "rcpt-002.pdf"}, "corrected_by": "ali.raza",
                        "reason": "uploaded receipt"}).json()
    assert _types(fixed) == ["over_threshold"]
    exc = fixed["open_exceptions"][0]
    ok = _post(client, f"/v1/exceptions/{exc['exception_id']}/accept",
               json={"by": "mark.manager", "reason": "client dinner pre-approved"}).json()
    assert ok["status"] == "validated"


def test_duplicate_expense_claim(client):
    _post(client, "/v1/expenses", json=_expense(amount="75.00"))
    dup = _post(client, "/v1/expenses", json=_expense(amount="75.00")).json()
    assert _types(dup) == ["duplicate_invoice"]
    rej = _post(client, f"/v1/expenses/{dup['entity_id']}/reject",
                json={"by": "mark.manager", "reason": "claimed twice"}).json()
    assert rej["status"] == "rejected"


def test_blocked_expense_cannot_be_approved(client):
    body = _post(client, "/v1/expenses", json=_expense(employee_name=None)).json()
    assert body["status"] == "blocked"
    assert _post(client, f"/v1/expenses/{body['entity_id']}/approve", json={"by": "mark"}).status_code == 409


# ----------------------------------------------------------------------------- queue

def test_exception_queue_lists_open_items(client):
    body = _post(client, "/v1/invoices", json=_invoice(total="1.00")).json()
    queue = client.get("/v1/exceptions", headers=API_HEADERS, params={"entity_type": "invoice"}).json()
    assert any(e["entity_id"] == body["entity_id"] for e in queue)
    assert all(e["status"] == "open" for e in queue)
