import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models import Approval, ExceptionRecord, Inquiry, RuleDecision
from tests.conftest import API_HEADERS

MSG_20 = "We need 200 units of Product X next month, can you give us 20% discount? I'd like to talk tomorrow."
MSG_3 = "Please send 100 units of Product X with 3% discount."
MSG_10 = "We need 100 units of Product X with 10% discount."
MSG_90 = "We need 100 units of Product X with 90% discount."


def _pipeline(client, message):
    """intake -> extract -> decide (the same order n8n calls them in)."""
    r = client.post("/v1/inquiries", json={"message": message},
                    headers={**API_HEADERS, "Idempotency-Key": f"a-{uuid.uuid4()}"})
    iid = r.json()["inquiry_id"]
    assert client.post(f"/v1/inquiries/{iid}/extract", headers=API_HEADERS).status_code == 200
    return iid, client.post(f"/v1/inquiries/{iid}/decide", headers=API_HEADERS).json()


def _decide(client, approval_id, **payload):
    return client.post(f"/v1/approvals/{approval_id}/decision", headers=API_HEADERS, json=payload)


def test_small_discount_is_auto_approved(client, db_session):
    iid, body = _pipeline(client, MSG_3)
    assert body["outcome"] == "auto_approve" and body["status"] == "approved"
    assert body["approval"]["status"] == "approved"
    assert body["approval"]["approved_discount_pct"] == "3.00"
    assert body["approval"]["terms_hash"] and body["approval_token"] is None
    assert body["amounts"]["total"] == "4850.00"          # 100 x 50.00 - 3%
    assert db_session.execute(select(RuleDecision).where(
        RuleDecision.inquiry_id == uuid.UUID(iid))).scalar_one().rule_version == "discount-rules-v1"


def test_mid_discount_requires_salesperson(client):
    _, body = _pipeline(client, MSG_10)
    assert body["outcome"] == "salesperson_approval" and body["status"] == "awaiting_approval"
    assert body["approval"]["required_role"] == "salesperson"
    assert body["approval"]["status"] == "pending" and body["approval_token"]
    assert body["approval"]["expires_at"] is not None


def test_large_discount_requires_manager_and_modify_wins(client, db_session):
    iid, body = _pipeline(client, MSG_20)
    assert body["outcome"] == "manager_approval"
    approval_id, token = body["approval"]["approval_id"], body["approval_token"]

    # The manager cuts 20% down to 12%.
    r = _decide(client, approval_id, action="modify", token=token, decided_by="maria.manager",
                approved_discount_pct="12", comment="Volume does not justify 20%")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["approval"]["status"] == "modified"
    assert out["approval"]["approved_discount_pct"] == "12.00"
    assert out["inquiry_status"] == "approved"
    assert out["amounts"]["total"] == "8800.00"           # 200 x 50.00 - 12%
    assert out["approval"]["terms_hash"]

    # The decided approval is frozen in the database (trigger from 001_schema.sql).
    approval = db_session.get(Approval, uuid.UUID(approval_id))
    assert approval.status == "modified" and approval.decided_by == "maria.manager"


def test_approve_as_requested(client):
    _, body = _pipeline(client, MSG_10)
    r = _decide(client, body["approval"]["approval_id"], action="approve",
                token=body["approval_token"], decided_by="sam.sales")
    assert r.json()["approval"]["status"] == "approved"
    assert r.json()["approval"]["approved_discount_pct"] == "10.00"


def test_reject_sets_inquiry_rejected(client):
    _, body = _pipeline(client, MSG_20)
    r = _decide(client, body["approval"]["approval_id"], action="reject",
                token=body["approval_token"], decided_by="maria.manager", comment="Margin too low")
    assert r.json()["approval"]["status"] == "rejected" and r.json()["inquiry_status"] == "rejected"


def test_discount_above_policy_ceiling_is_auto_rejected(client):
    _, body = _pipeline(client, MSG_90)
    assert body["outcome"] == "reject" and body["status"] == "rejected"
    assert body["approval"]["status"] == "rejected" and body["approval_token"] is None


def test_approver_cannot_grant_more_than_requested(client):
    _, body = _pipeline(client, MSG_10)
    r = _decide(client, body["approval"]["approval_id"], action="modify", token=body["approval_token"],
                decided_by="sam.sales", approved_discount_pct="25")
    assert r.status_code == 422 and "more than the" in r.json()["detail"]


def test_salesperson_cannot_exceed_their_authority(client, db_session):
    """Even a 'modify' must stay inside the role's limit."""
    _, body = _pipeline(client, MSG_10)
    approval = db_session.get(Approval, uuid.UUID(body["approval"]["approval_id"]))
    approval.requested_discount_pct = 30      # pretend a 30% request routed to a salesperson
    db_session.flush()
    r = _decide(client, str(approval.id), action="modify", token=body["approval_token"],
                decided_by="sam.sales", approved_discount_pct="20")
    assert r.status_code == 422 and "cannot grant" in r.json()["detail"]


def test_wrong_token_is_rejected(client):
    _, body = _pipeline(client, MSG_10)
    r = _decide(client, body["approval"]["approval_id"], action="approve",
                token="not-the-real-token", decided_by="attacker")
    assert r.status_code == 401


def test_second_click_does_not_change_the_decision(client):
    _, body = _pipeline(client, MSG_20)
    aid, token = body["approval"]["approval_id"], body["approval_token"]
    first = _decide(client, aid, action="modify", token=token, decided_by="maria.manager",
                    approved_discount_pct="12").json()
    second = _decide(client, aid, action="approve", token=token, decided_by="someone.else")
    assert second.status_code == 200 and second.json()["replay"] is True
    assert second.json()["approval"]["approved_discount_pct"] == first["approval"]["approved_discount_pct"]
    assert second.json()["approval"]["decided_by"] == "maria.manager"


def test_expired_approval_goes_to_human_review(client, db_session):
    _, body = _pipeline(client, MSG_20)
    aid = uuid.UUID(body["approval"]["approval_id"])
    db_session.get(Approval, aid).expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
    db_session.flush()

    r = _decide(client, str(aid), action="approve", token=body["approval_token"], decided_by="late.manager")
    assert r.status_code == 409 and "expired" in r.json()["detail"]
    approval = db_session.get(Approval, aid)
    assert approval.status == "expired"
    assert db_session.get(Inquiry, approval.inquiry_id).status == "needs_review"
    types = db_session.execute(select(ExceptionRecord.exception_type).where(
        ExceptionRecord.entity_id == aid, ExceptionRecord.status == "open")).scalars().all()
    assert types == ["approval_timeout"]


def test_scheduled_sweep_expires_overdue_approvals(client, db_session):
    _, body = _pipeline(client, MSG_20)
    aid = uuid.UUID(body["approval"]["approval_id"])
    db_session.get(Approval, aid).expires_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    db_session.flush()
    r = client.post("/v1/approvals/expire-due", headers=API_HEADERS)
    assert str(aid) in r.json()["expired"]
    assert db_session.get(Approval, aid).status == "expired"


def test_view_approval_with_token(client):
    _, body = _pipeline(client, MSG_20)
    aid, token = body["approval"]["approval_id"], body["approval_token"]
    r = client.get(f"/v1/approvals/{aid}", params={"token": token}, headers=API_HEADERS)
    assert r.status_code == 200 and r.json()["amounts"]["discount_pct"] == "20.00"
    assert client.get(f"/v1/approvals/{aid}", params={"token": "x" * 20},
                      headers=API_HEADERS).status_code == 401


def test_decide_is_idempotent(client, db_session):
    iid, first = _pipeline(client, MSG_20)
    second = client.post(f"/v1/inquiries/{iid}/decide", headers=API_HEADERS).json()
    assert second["replay"] is True
    assert second["approval"]["approval_id"] == first["approval"]["approval_id"]
    assert second["approval_token"] is None          # the token is only ever returned once
    n = db_session.execute(select(RuleDecision).where(RuleDecision.inquiry_id == uuid.UUID(iid))).all()
    assert len(n) == 1


def test_cannot_decide_before_extraction(client):
    r = client.post("/v1/inquiries", json={"message": MSG_20},
                    headers={**API_HEADERS, "Idempotency-Key": f"nd-{uuid.uuid4()}"})
    iid = r.json()["inquiry_id"]
    out = client.post(f"/v1/inquiries/{iid}/decide", headers=API_HEADERS)
    assert out.status_code == 409 and "extraction" in out.json()["detail"]


def test_needs_review_inquiry_cannot_be_decided(client):
    r = client.post("/v1/inquiries", json={"message": "We want 100 units of Product Q at 10% off."},
                    headers={**API_HEADERS, "Idempotency-Key": f"nr-{uuid.uuid4()}"})
    iid = r.json()["inquiry_id"]
    client.post(f"/v1/inquiries/{iid}/extract", headers=API_HEADERS)
    assert client.post(f"/v1/inquiries/{iid}/decide", headers=API_HEADERS).status_code == 409


def test_unknown_approval_404(client):
    r = _decide(client, str(uuid.uuid4()), action="approve", token="x" * 20, decided_by="someone")
    assert r.status_code == 404
