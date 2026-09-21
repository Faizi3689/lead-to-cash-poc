import uuid

from sqlalchemy import select

from app.models import AuditLog, ExceptionRecord
from tests.conftest import API_HEADERS


def test_failure_with_inquiry_opens_exception(client, db_session):
    iid = client.post("/v1/inquiries", json={"message": "We need 10 units of Product X"},
                      headers={**API_HEADERS, "Idempotency-Key": f"o-{uuid.uuid4()}"}).json()["inquiry_id"]
    r = client.post("/v1/ops/workflow-failures", headers=API_HEADERS, json={
        "workflow": "01 - Lead-to-Cash", "stage": "Create Quote", "error": "502 Bad Gateway",
        "execution_id": "123", "inquiry_id": iid}).json()
    assert r["recorded"] and r["exception_id"]
    exc = db_session.get(ExceptionRecord, uuid.UUID(r["exception_id"]))
    assert exc.exception_type == "downstream_failure" and exc.details["stage"] == "Create Quote"
    # Reporting the same failure twice does not create a second open exception.
    again = client.post("/v1/ops/workflow-failures", headers=API_HEADERS,
                        json={"workflow": "01", "stage": "Create Quote", "inquiry_id": iid}).json()
    assert again["exception_id"] == r["exception_id"]


def test_failure_without_inquiry_is_audited(client, db_session):
    r = client.post("/v1/ops/workflow-failures", headers=API_HEADERS,
                    json={"workflow": "99 - Error Handler", "error": "boom", "execution_id": "x9"}).json()
    assert r["recorded"] and r["exception_id"] is None
    assert db_session.execute(select(AuditLog).where(AuditLog.action == "n8n.workflow_failed")).first()
