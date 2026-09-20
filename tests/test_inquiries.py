import uuid

from sqlalchemy import func, select

from app.models import AuditLog, Customer, Inquiry
from tests.conftest import API_HEADERS

MESSAGE = "We need 200 units of Product X next month, can you give us 20% discount? I'd like to talk tomorrow."


def _post(client, body, key=None):
    headers = {**API_HEADERS, "Idempotency-Key": key or f"test-{uuid.uuid4()}"}
    return client.post("/v1/inquiries", json=body, headers=headers)


def test_create_inquiry_returns_201_and_writes_audit(client, db_session):
    r = _post(client, {"message": MESSAGE, "customer_email": "buyer@newco.example", "customer_name": "Buyer"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "received" and body["duplicate"] is False
    assert body["customer_id"] is not None

    inquiry_id = uuid.UUID(body["inquiry_id"])
    actions = db_session.execute(
        select(AuditLog.action).where(AuditLog.inquiry_id == inquiry_id)).scalars().all()
    assert "inquiry.received" in actions


def test_duplicate_delivery_returns_same_inquiry(client, db_session):
    key = f"dup-{uuid.uuid4()}"
    first = _post(client, {"message": MESSAGE}, key)
    second = _post(client, {"message": MESSAGE}, key)
    assert first.status_code == 201 and second.status_code == 200
    assert second.json()["duplicate"] is True
    assert first.json()["inquiry_id"] == second.json()["inquiry_id"]
    count = db_session.execute(
        select(func.count()).select_from(Inquiry).where(Inquiry.idempotency_key == key)).scalar_one()
    assert count == 1


def test_same_key_different_body_is_rejected(client):
    key = f"conflict-{uuid.uuid4()}"
    assert _post(client, {"message": MESSAGE}, key).status_code == 201
    r = _post(client, {"message": "Completely different request"}, key)
    assert r.status_code == 409
    assert r.json()["error"] == "http_error"


def test_existing_customer_matched_case_insensitively(client, db_session):
    email = f"mixed-{uuid.uuid4().hex[:8]}@example.com"
    a = _post(client, {"message": "first", "customer_email": email})
    b = _post(client, {"message": "second", "customer_email": email.upper()})
    assert a.json()["customer_id"] == b.json()["customer_id"]
    n = db_session.execute(
        select(func.count()).select_from(Customer).where(func.lower(Customer.email) == email)).scalar_one()
    assert n == 1


def test_missing_idempotency_key_is_422(client):
    r = client.post("/v1/inquiries", json={"message": MESSAGE}, headers=API_HEADERS)
    assert r.status_code == 422
    assert any("Idempotency-Key" in d["field"] for d in r.json()["detail"])


def test_invalid_email_is_422(client):
    assert _post(client, {"message": MESSAGE, "customer_email": "not-an-email"}).status_code == 422


def test_requires_api_key(client):
    r = client.post("/v1/inquiries", json={"message": MESSAGE}, headers={"Idempotency-Key": "abcdefgh1"})
    assert r.status_code == 401


def test_get_inquiry_and_404(client):
    created = _post(client, {"message": MESSAGE}).json()
    r = client.get(f"/v1/inquiries/{created['inquiry_id']}", headers=API_HEADERS)
    assert r.status_code == 200 and r.json()["inquiry_id"] == created["inquiry_id"]
    assert client.get(f"/v1/inquiries/{uuid.uuid4()}", headers=API_HEADERS).status_code == 404
