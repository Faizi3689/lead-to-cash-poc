"""Rules decision + approval lifecycle.

The approval row is the single source of truth for commercial terms. Quotes, orders and invoices
are built from it (and the database enforces that), so "what the manager approved" is always
"what the customer is charged".
"""
import hashlib
import hmac
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit
from app.models import Approval, Inquiry, Product, RuleDecision
from app.rules import engine
from app.services import exception_service, pricing

log = logging.getLogger("app.approvals")

APPROVAL_TTL_HOURS = 48
DECIDABLE_STATUSES = ("extracted",)
FINAL_INQUIRY_STATUSES = ("awaiting_approval", "approved", "rejected", "quoted", "ordered", "invoiced")


class InquiryNotFound(Exception):
    pass


class ApprovalNotFound(Exception):
    pass


class NotDecidable(Exception):
    """Inquiry is not in a state where the rules engine can run."""


class InvalidToken(Exception):
    pass


class ApprovalExpired(Exception):
    pass


class NotAuthorised(Exception):
    """The approver tried to grant more than their role allows, or more than was requested."""


@dataclass
class DecisionResult:
    inquiry: Inquiry
    outcome: str
    reasons: list[str]
    approval: Approval | None
    amounts: pricing.Amounts | None
    approval_token: str | None = None   # returned once, never stored in plain text
    replay: bool = False
    exception_ids: list[uuid.UUID] = None


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _amounts_for(product: Product, quantity: int, discount_pct: Decimal) -> pricing.Amounts:
    return pricing.compute_amounts(quantity, product.unit_price, discount_pct, product.currency)


# --------------------------------------------------------------------------- run the rules

def run_rules(db: Session, inquiry_id: uuid.UUID) -> DecisionResult:
    inquiry = db.execute(select(Inquiry).where(Inquiry.id == inquiry_id).with_for_update()).scalar_one_or_none()
    if inquiry is None:
        raise InquiryNotFound(str(inquiry_id))

    existing = db.execute(select(Approval).where(Approval.inquiry_id == inquiry.id)
                          .order_by(Approval.created_at.desc())).scalars().first()
    if existing is not None or inquiry.status in FINAL_INQUIRY_STATUSES:
        db.rollback()
        return _replay(db, inquiry, existing)

    if inquiry.status not in DECIDABLE_STATUSES:
        db.rollback()
        raise NotDecidable(f"inquiry status is '{inquiry.status}'; extraction must succeed first")

    extracted = inquiry.extracted or {}
    product = db.get(Product, uuid.UUID(extracted["product_id"])) if extracted.get("product_id") else None
    quantity = extracted.get("quantity")
    discount = Decimal(str(extracted.get("requested_discount_pct") or "0"))
    amounts = _amounts_for(product, quantity, discount) if (product and quantity) else None

    decision = engine.decide(product_found=product is not None, quantity=quantity,
                             discount_pct=discount, order_total=amounts.total if amounts else None)

    record = RuleDecision(
        inquiry_id=inquiry.id,
        ai_run_id=inquiry.accepted_ai_run_id,
        rule_version=engine.RULE_VERSION,
        inputs=audit._jsonable({"product_id": extracted.get("product_id"), "sku": extracted.get("sku"),
                                "quantity": quantity, "requested_discount_pct": str(discount),
                                "amounts": amounts.as_dict() if amounts else None}),
        outcome=decision.outcome,
        reasons=decision.reasons,
    )
    db.add(record)
    db.flush()
    audit.record(db, action="rule.decided", entity_type="inquiry", entity_id=inquiry.id,
                 inquiry_id=inquiry.id, actor=f"system:{engine.RULE_VERSION}",
                 after={"outcome": decision.outcome, "reasons": decision.reasons})

    exception_ids: list[uuid.UUID] = []
    approval = None
    token = None

    if decision.outcome == engine.HUMAN_REVIEW:
        inquiry.status = "needs_review"
        exception_ids.append(exception_service.open_exception(
            db, entity_type="inquiry", entity_id=inquiry.id, exception_type="missing_field",
            details={"rule_version": engine.RULE_VERSION, "reasons": decision.reasons},
            inquiry_id=inquiry.id))
    else:
        approval = Approval(
            inquiry_id=inquiry.id, rule_decision_id=record.id,
            required_role=decision.required_role or "manager",
            product_id=product.id if product else None,
            quantity=quantity, unit_price=product.unit_price, requested_discount_pct=discount,
        )
        if decision.outcome == engine.REJECT:
            approval.required_role = "manager"
            approval.status = "rejected"
            approval.decided_by = f"system:{engine.RULE_VERSION}"
            approval.decision_comment = "; ".join(decision.reasons)
            approval.decided_at = datetime.now(timezone.utc)
            inquiry.status = "rejected"
        elif decision.outcome == engine.AUTO_APPROVE:
            approval.status = "approved"
            approval.approved_discount_pct = discount
            approval.decided_by = f"system:{engine.RULE_VERSION}"
            approval.decision_comment = "; ".join(decision.reasons)
            approval.decided_at = datetime.now(timezone.utc)
            approval.terms_hash = pricing.terms_hash(
                inquiry_id=inquiry.id, product_id=product.id, quantity=quantity,
                unit_price=product.unit_price, discount_pct=discount, currency=product.currency)
            inquiry.status = "approved"
        else:
            token = secrets.token_urlsafe(32)
            approval.token_hash = _hash_token(token)
            approval.expires_at = datetime.now(timezone.utc) + timedelta(hours=APPROVAL_TTL_HOURS)
            inquiry.status = "awaiting_approval"
        db.add(approval)
        db.flush()
        audit.record(db, action=f"approval.{approval.status}", entity_type="approval",
                     entity_id=approval.id, inquiry_id=inquiry.id,
                     actor=approval.decided_by or f"system:{engine.RULE_VERSION}",
                     after={"required_role": approval.required_role, "status": approval.status,
                            "requested_discount_pct": str(discount),
                            "approved_discount_pct": str(approval.approved_discount_pct)
                            if approval.approved_discount_pct is not None else None,
                            "expires_at": approval.expires_at})

    inquiry.updated_at = datetime.now(timezone.utc)
    db.commit()
    log.info("Rules decided: %s", decision.outcome, extra={"inquiry_id": inquiry.id})
    return DecisionResult(inquiry=inquiry, outcome=decision.outcome, reasons=decision.reasons,
                          approval=approval, amounts=amounts, approval_token=token,
                          exception_ids=exception_ids)


def _replay(db: Session, inquiry: Inquiry, approval: Approval | None) -> DecisionResult:
    record = db.execute(select(RuleDecision).where(RuleDecision.inquiry_id == inquiry.id)
                        .order_by(RuleDecision.created_at.desc())).scalars().first()
    amounts = None
    if approval and approval.product_id:
        product = db.get(Product, approval.product_id)
        pct = approval.approved_discount_pct if approval.approved_discount_pct is not None \
            else approval.requested_discount_pct
        amounts = _amounts_for(product, approval.quantity, pct)
    return DecisionResult(inquiry=inquiry, outcome=record.outcome if record else "human_review",
                          reasons=record.reasons if record else [], approval=approval,
                          amounts=amounts, replay=True,
                          exception_ids=[e.id for e in exception_service.open_exceptions_for(
                              db, "inquiry", inquiry.id)])


# --------------------------------------------------------------------------- human decision

def apply_decision(db: Session, approval_id: uuid.UUID, *, token: str, action: str,
                   decided_by: str, approved_discount_pct: Decimal | None = None,
                   comment: str | None = None) -> tuple[Approval, bool]:
    """action: 'approve' | 'reject' | 'modify'. Returns (approval, replay)."""
    approval = db.execute(select(Approval).where(Approval.id == approval_id)
                          .with_for_update()).scalar_one_or_none()
    if approval is None:
        raise ApprovalNotFound(str(approval_id))

    if not approval.token_hash or not hmac.compare_digest(approval.token_hash, _hash_token(token)):
        db.rollback()
        raise InvalidToken("invalid approval token")

    if approval.status != "pending":
        db.rollback()   # already decided: an n8n retry or a second click must not change anything
        return approval, True

    expires = _aware(approval.expires_at)
    if expires and datetime.now(timezone.utc) > expires:
        _expire(db, approval)
        db.commit()
        raise ApprovalExpired(f"approval expired at {expires.isoformat()}")

    requested = approval.requested_discount_pct
    if action == "approve":
        granted = requested
    elif action == "modify":
        if approved_discount_pct is None:
            raise NotAuthorised("approved_discount_pct is required when modifying")
        granted = approved_discount_pct
        if granted > requested:
            db.rollback()
            raise NotAuthorised(f"cannot grant {granted}%: more than the {requested}% requested")
    elif action == "reject":
        granted = None
    else:
        raise NotAuthorised(f"unknown action '{action}'")

    if granted is not None and not engine.may_grant(approval.required_role, granted):
        db.rollback()
        raise NotAuthorised(f"a {approval.required_role} cannot grant {granted}% "
                            f"(limit {engine.ROLE_MAX_PCT[approval.required_role]}%)")

    inquiry = db.execute(select(Inquiry).where(Inquiry.id == approval.inquiry_id)
                         .with_for_update()).scalar_one()
    product = db.get(Product, approval.product_id)
    before = {"status": approval.status}

    # All fields of the decision are written together: the row never exists in a half-decided state.
    if action == "reject":
        approval.status = "rejected"
        inquiry.status = "rejected"
    else:
        approval.status = "approved" if granted == requested else "modified"
        approval.approved_discount_pct = granted
        approval.terms_hash = pricing.terms_hash(
            inquiry_id=approval.inquiry_id, product_id=approval.product_id, quantity=approval.quantity,
            unit_price=approval.unit_price, discount_pct=granted, currency=product.currency)
        inquiry.status = "approved"

    approval.decided_by = decided_by
    approval.decision_comment = comment
    approval.decided_at = datetime.now(timezone.utc)
    # The token stays on the row so a retried call can be recognised and replayed; the status check
    # above is what makes the link single-use - a second click can never change the decision.
    inquiry.updated_at = datetime.now(timezone.utc)

    audit.record(db, action=f"approval.{approval.status}", entity_type="approval", entity_id=approval.id,
                 inquiry_id=inquiry.id, actor=f"user:{decided_by}", before=before,
                 after={"status": approval.status, "requested_discount_pct": str(requested),
                        "approved_discount_pct": str(granted) if granted is not None else None,
                        "terms_hash": approval.terms_hash, "comment": comment})
    db.commit()
    log.info("Approval %s by %s", approval.status, decided_by, extra={"inquiry_id": inquiry.id})
    return approval, False


def _expire(db: Session, approval: Approval) -> None:
    inquiry = db.execute(select(Inquiry).where(Inquiry.id == approval.inquiry_id)
                         .with_for_update()).scalar_one()
    approval.status = "expired"
    approval.decided_by = "system:timeout"
    approval.decided_at = datetime.now(timezone.utc)
    inquiry.status = "needs_review"
    inquiry.updated_at = datetime.now(timezone.utc)
    exception_service.open_exception(
        db, entity_type="approval", entity_id=approval.id, exception_type="approval_timeout",
        details={"inquiry_id": str(approval.inquiry_id), "required_role": approval.required_role,
                 "requested_discount_pct": str(approval.requested_discount_pct)},
        inquiry_id=approval.inquiry_id, assigned_role="sales_ops")
    audit.record(db, action="approval.expired", entity_type="approval", entity_id=approval.id,
                 inquiry_id=approval.inquiry_id, actor="system:timeout",
                 before={"status": "pending"}, after={"status": "expired"})


def expire_due(db: Session) -> list[uuid.UUID]:
    """Called on a schedule by n8n: nothing stays pending forever."""
    due = db.execute(select(Approval).where(Approval.status == "pending",
                                            Approval.expires_at < datetime.now(timezone.utc))
                     .with_for_update(skip_locked=True)).scalars().all()
    for approval in due:
        _expire(db, approval)
    db.commit()
    return [a.id for a in due]


def get_approval(db: Session, approval_id: uuid.UUID, token: str) -> Approval:
    approval = db.get(Approval, approval_id)
    if approval is None:
        raise ApprovalNotFound(str(approval_id))
    if not approval.token_hash or not hmac.compare_digest(approval.token_hash, _hash_token(token)):
        raise InvalidToken("invalid approval token")
    return approval
