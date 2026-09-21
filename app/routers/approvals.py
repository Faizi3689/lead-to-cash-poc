"""Approval endpoints. Called by n8n when a human clicks approve / reject / modify."""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Approval, Product
from app.schemas import (AmountsOut, ApprovalDecisionIn, ApprovalDecisionOut, ApprovalOut, ErrorOut,
                         ExpireOut)
from app.security import require_api_key
from app.services import approval_service, pricing

router = APIRouter(prefix="/v1/approvals", tags=["approvals"], dependencies=[Depends(require_api_key)])


def approval_out(approval: Approval) -> ApprovalOut:
    return ApprovalOut(
        approval_id=approval.id, inquiry_id=approval.inquiry_id, status=approval.status,
        required_role=approval.required_role, quantity=approval.quantity,
        unit_price=str(pricing.money(approval.unit_price)),
        requested_discount_pct=str(pricing.money(approval.requested_discount_pct)),
        approved_discount_pct=str(pricing.money(approval.approved_discount_pct))
        if approval.approved_discount_pct is not None else None,
        terms_hash=approval.terms_hash, decided_by=approval.decided_by,
        decided_at=approval.decided_at, expires_at=approval.expires_at,
    )


def _amounts(db: Session, approval: Approval) -> AmountsOut | None:
    if not approval.product_id:
        return None
    product = db.get(Product, approval.product_id)
    pct = approval.approved_discount_pct if approval.approved_discount_pct is not None \
        else approval.requested_discount_pct
    return AmountsOut(**pricing.compute_amounts(approval.quantity, approval.unit_price, pct,
                                                product.currency).as_dict())


@router.get("/{approval_id}", response_model=ApprovalDecisionOut,
            responses={401: {"model": ErrorOut}, 404: {"model": ErrorOut}})
def read_approval(approval_id: uuid.UUID, token: str = Query(min_length=10),
                  db: Session = Depends(get_db)) -> ApprovalDecisionOut:
    """What the approver sees before deciding: the exact terms and what they would cost."""
    try:
        approval = approval_service.get_approval(db, approval_id, token)
    except approval_service.ApprovalNotFound:
        raise HTTPException(status_code=404, detail="Approval not found")
    except approval_service.InvalidToken:
        raise HTTPException(status_code=401, detail="Invalid approval token")
    from app.models import Inquiry
    inquiry = db.get(Inquiry, approval.inquiry_id)
    return ApprovalDecisionOut(approval=approval_out(approval), inquiry_status=inquiry.status,
                               amounts=_amounts(db, approval))


@router.post("/{approval_id}/decision", response_model=ApprovalDecisionOut,
             responses={401: {"model": ErrorOut}, 404: {"model": ErrorOut},
                        409: {"model": ErrorOut, "description": "Approval expired"},
                        422: {"model": ErrorOut, "description": "Beyond the approver's authority"}})
def decide_approval(approval_id: uuid.UUID, payload: ApprovalDecisionIn,
                    db: Session = Depends(get_db)) -> ApprovalDecisionOut:
    try:
        approval, replay = approval_service.apply_decision(
            db, approval_id, token=payload.token, action=payload.action,
            decided_by=payload.decided_by, approved_discount_pct=payload.approved_discount_pct,
            comment=payload.comment)
    except approval_service.ApprovalNotFound:
        raise HTTPException(status_code=404, detail="Approval not found")
    except approval_service.InvalidToken:
        raise HTTPException(status_code=401, detail="Invalid approval token")
    except approval_service.ApprovalExpired as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except approval_service.NotAuthorised as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    from app.models import Inquiry
    inquiry = db.get(Inquiry, approval.inquiry_id)
    return ApprovalDecisionOut(approval=approval_out(approval), inquiry_status=inquiry.status,
                               replay=replay, amounts=_amounts(db, approval))


@router.post("/expire-due", response_model=ExpireOut)
def expire_due(db: Session = Depends(get_db)) -> ExpireOut:
    """Scheduled sweep (n8n cron): approvals that timed out go to the human-review queue."""
    return ExpireOut(expired=approval_service.expire_due(db))
