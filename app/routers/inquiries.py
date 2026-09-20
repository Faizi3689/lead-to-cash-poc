import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas import ErrorOut, InquiryCreate, InquiryOut
from app.security import require_api_key
from app.services import inquiry_service

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
