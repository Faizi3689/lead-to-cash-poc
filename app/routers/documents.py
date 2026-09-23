"""Quote / order / invoice / appointment endpoints (each one idempotent)."""
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Appointment, Invoice, Order, Quote
from app.schemas import AppointmentOut, ErrorOut, InvoiceOut, OrderOut, QuoteOut
from app.security import require_api_key
from app.services import appointment_service, document_service, trail_service

router = APIRouter(tags=["documents"], dependencies=[Depends(require_api_key)])

ERRORS = {404: {"model": ErrorOut}, 409: {"model": ErrorOut}}


def _s(v):
    return None if v is None else str(v)


def quote_out(q: Quote, replay: bool = False) -> QuoteOut:
    return QuoteOut(quote_id=q.id, quote_number=q.quote_number, inquiry_id=q.inquiry_id,
                    approval_id=q.approval_id, status=q.status, quantity=q.quantity,
                    unit_price=str(q.unit_price), discount_pct=str(q.discount_pct), subtotal=str(q.subtotal),
                    discount_amount=str(q.discount_amount), total=str(q.total), currency=q.currency,
                    terms_hash=q.terms_hash, valid_until=_s(q.valid_until), replay=replay)


def order_out(o: Order, replay: bool = False) -> OrderOut:
    return OrderOut(order_id=o.id, order_number=o.order_number, quote_id=o.quote_id, inquiry_id=o.inquiry_id,
                    status=o.status, quantity=o.quantity, discount_pct=str(o.discount_pct), total=str(o.total),
                    currency=o.currency, terms_hash=o.terms_hash, replay=replay)


def invoice_out(i: Invoice, replay: bool = False) -> InvoiceOut:
    return InvoiceOut(invoice_id=i.id, invoice_number=i.invoice_number, order_id=i.order_id,
                      direction=i.direction, status=i.status, counterparty_name=i.counterparty_name,
                      invoice_date=_s(i.invoice_date), due_date=_s(i.due_date), subtotal=_s(i.subtotal),
                      discount_pct=_s(i.discount_pct), discount_amount=_s(i.discount_amount),
                      tax_amount=_s(i.tax_amount), total=_s(i.total), currency=i.currency,
                      line_items=i.line_items or [], terms_hash=i.terms_hash,
                      payment_status=i.payment_status, amount_paid=_s(i.amount_paid), replay=replay)


def appointment_out(a: Appointment, replay: bool = False) -> AppointmentOut:
    return AppointmentOut(appointment_id=a.id, inquiry_id=a.inquiry_id, status=a.status,
                          requested_text=a.requested_text, scheduled_start=a.scheduled_start,
                          scheduled_end=a.scheduled_end, timezone=a.timezone, external_ref=a.external_ref,
                          replay=replay)


def _run(fn, *args):
    try:
        return fn(*args)
    except (document_service.NotFound, appointment_service.NotFound) as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except (document_service.NotAllowed, document_service.TermsMismatch,
            appointment_service.NotRequested) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except appointment_service.CalendarUnavailable as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))


@router.post("/v1/inquiries/{inquiry_id}/quote", response_model=QuoteOut, responses=ERRORS)
def create_quote(inquiry_id: uuid.UUID, db: Session = Depends(get_db)) -> QuoteOut:
    """Issue the quote from the granted approval (approved or modified terms, never the request)."""
    r = _run(document_service.create_quote, db, inquiry_id)
    return quote_out(r.document, r.replay)


@router.post("/v1/quotes/{quote_id}/order", response_model=OrderOut, responses=ERRORS)
def create_order(quote_id: uuid.UUID, db: Session = Depends(get_db)) -> OrderOut:
    """Customer accepted the quote: create the sales order with identical terms."""
    r = _run(document_service.create_order, db, quote_id)
    return order_out(r.document, r.replay)


@router.post("/v1/orders/{order_id}/invoice", response_model=InvoiceOut, responses=ERRORS)
def create_invoice(order_id: uuid.UUID, db: Session = Depends(get_db)) -> InvoiceOut:
    """Generate the invoice as a DRAFT. It must pass validation (Step 7) before it can be approved."""
    r = _run(document_service.create_invoice, db, order_id)
    return invoice_out(r.document, r.replay)


@router.post("/v1/inquiries/{inquiry_id}/appointment", response_model=AppointmentOut,
             responses={**ERRORS, 503: {"model": ErrorOut, "description": "Calendar unavailable, retry later"}})
def create_appointment(inquiry_id: uuid.UUID, db: Session = Depends(get_db)) -> AppointmentOut:
    appointment, replay = _run(appointment_service.schedule, db, inquiry_id)
    return appointment_out(appointment, replay)


@router.get("/v1/inquiries/{inquiry_id}/documents", responses={404: {"model": ErrorOut}})
def documents(inquiry_id: uuid.UUID, db: Session = Depends(get_db)) -> dict:
    """Evidence view: approval, quote, order and invoice side by side, with a consistency verdict."""
    return _run(document_service.commercial_summary, db, inquiry_id)


@router.get("/v1/inquiries/{inquiry_id}/trail", responses={404: {"model": ErrorOut}})
def trail(inquiry_id: uuid.UUID, db: Session = Depends(get_db)) -> dict:
    """Full evidence trail: request, every AI attempt, rule decision, approval, executed documents,
    exceptions with their resolution, and the ordered audit timeline."""
    try:
        return trail_service.build_trail(db, inquiry_id)
    except trail_service.NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))

