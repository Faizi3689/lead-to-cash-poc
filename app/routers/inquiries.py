import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.llm.base import LLMClient, LLMUnavailable
from app.llm.factory import get_llm_client
from app.schemas import (AmountsOut, DecisionOut, ErrorOut, ExtractionOut, InquiryCreate, InquiryOut,
                         ProductOut)
from app.security import require_api_key
from app.rules import engine as rules_engine
from app.services import approval_service, extraction_service, inquiry_service

router = APIRouter(prefix="/v1/inquiries", tags=["inquiries"], dependencies=[Depends(require_api_key)])


def _out(inquiry, duplicate: bool) -> InquiryOut:
    return InquiryOut(inquiry_id=inquiry.id, status=inquiry.status, duplicate=duplicate,
                      customer_id=inquiry.customer_id, received_at=inquiry.received_at)


@router.post(
    "",
    response_model=InquiryOut,
    status_code=status.HTTP_201_CREATED,
    responses={200: {"model": InquiryOut, "description": "Duplicate delivery - existing inquiry returned"},
               409: {"model": ErrorOut, "description": "Idempotency-Key reused with a different body"}},
)
def create_inquiry(
    payload: InquiryCreate,
    response: Response,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=200),
    db: Session = Depends(get_db),
) -> InquiryOut:
    try:
        inquiry, duplicate = inquiry_service.create_inquiry(db, payload, idempotency_key)
    except inquiry_service.IdempotencyConflict as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Idempotency-Key already used for inquiry {exc} with a different request body",
        ) from exc
    if duplicate:
        response.status_code = status.HTTP_200_OK
    return _out(inquiry, duplicate)


@router.get("/{inquiry_id}", response_model=InquiryOut, responses={404: {"model": ErrorOut}})
def read_inquiry(inquiry_id: uuid.UUID, db: Session = Depends(get_db)) -> InquiryOut:
    inquiry = inquiry_service.get_inquiry(db, inquiry_id)
    if inquiry is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Inquiry not found")
    return _out(inquiry, False)


def _llm_or_none() -> LLMClient | None:
    """Build the LLM client; a configuration problem (e.g. missing key) becomes a handled outage."""
    try:
        return get_llm_client()
    except LLMUnavailable:
        return None


class _UnavailableLLM:
    provider = "unconfigured"
    model = None

    def complete_json(self, messages):
        raise LLMUnavailable("LLM provider is not configured", retryable=False)


@router.post(
    "/{inquiry_id}/extract",
    response_model=ExtractionOut,
    responses={404: {"model": ErrorOut}, 409: {"model": ErrorOut, "description": "In progress or needs a human"}},
)
def extract(inquiry_id: uuid.UUID, db: Session = Depends(get_db),
            llm: LLMClient | None = Depends(_llm_or_none)) -> ExtractionOut:
    """Run AI extraction. Safe to call repeatedly: a finished extraction is returned, not redone."""
    try:
        result = extraction_service.extract_inquiry(db, inquiry_id, llm or _UnavailableLLM())
    except extraction_service.InquiryNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Inquiry not found")
    except extraction_service.ExtractionNotAllowed as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))

    p = result.product
    return ExtractionOut(
        inquiry_id=result.inquiry.id,
        status=result.inquiry.status,
        outcome=result.outcome,
        replay=result.replay,
        attempts=result.attempts,
        confidence=result.confidence,
        product=ProductOut(product_id=p.id, sku=p.sku, name=p.name, unit_price=str(p.unit_price),
                           currency=p.currency) if p else None,
        extracted=result.inquiry.extracted,
        review_reasons=result.reasons,
        exception_ids=result.exception_ids,
    )


@router.post(
    "/{inquiry_id}/decide",
    response_model=DecisionOut,
    responses={404: {"model": ErrorOut},
               409: {"model": ErrorOut, "description": "Extraction has not completed successfully"}},
)
def decide(inquiry_id: uuid.UUID, db: Session = Depends(get_db)) -> DecisionOut:
    """Run the deterministic discount rules and create the approval record. Safe to call twice."""
    from app.routers.approvals import approval_out

    try:
        result = approval_service.run_rules(db, inquiry_id)
    except approval_service.InquiryNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Inquiry not found")
    except approval_service.NotDecidable as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))

    return DecisionOut(
        inquiry_id=result.inquiry.id,
        status=result.inquiry.status,
        outcome=result.outcome,
        rule_version=rules_engine.RULE_VERSION,
        reasons=result.reasons,
        replay=result.replay,
        approval=approval_out(result.approval) if result.approval else None,
        approval_token=result.approval_token,
        amounts=AmountsOut(**result.amounts.as_dict()) if result.amounts else None,
        exception_ids=result.exception_ids or [],
    )
