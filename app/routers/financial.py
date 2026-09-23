"""Invoice / expense validation and the human exception queue."""
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.db import get_db
from app.extraction.invoice import capture_invoice
from app.llm.base import LLMClient
from app.models import ExceptionRecord, Expense, Invoice
from app.routers.inquiries import _llm_or_none, _UnavailableLLM
from app.rules.financial_checks import OVERRIDABLE
from app.schemas import (ActorIn, CorrectionIn, ErrorOut, ExceptionOut, ExpenseIn, ExplanationIn, InvoiceIn,
                         InvoiceTextIn, PaymentIn, ValidationOut)
from app.security import require_api_key
from app.services import exception_service
from app.services import financial_service as fin

router = APIRouter(tags=["financial exceptions"], dependencies=[Depends(require_api_key)])
ERR = {404: {"model": ErrorOut}, 409: {"model": ErrorOut}, 422: {"model": ErrorOut}}


def exception_out(e: ExceptionRecord) -> ExceptionOut:
    d = e.details or {}
    return ExceptionOut(exception_id=e.id, entity_type=e.entity_type, entity_id=e.entity_id,
                        exception_type=e.exception_type, severity=e.severity, status=e.status,
                        message=d.get("message"), details=d, assigned_role=e.assigned_role,
                        overridable=e.exception_type in OVERRIDABLE, resolved_by=e.resolved_by,
                        resolution=e.resolution, created_at=e.created_at)


def _doc(obj, fields) -> dict:
    out = {"id": str(obj.id), "status": obj.status}
    for f in fields:
        v = getattr(obj, f)
        out[f] = v if isinstance(v, (list, dict, type(None))) else str(v)
    if isinstance(obj, Invoice):
        out.update(direction=obj.direction, source=obj.source,
                   order_id=str(obj.order_id) if obj.order_id else None,
                   payment_status=obj.payment_status, amount_paid=str(obj.amount_paid))
    return out


def _out(entity_type, obj, open_items, replay=False, ai=None) -> ValidationOut:
    fields = fin.INVOICE_FIELDS if entity_type == "invoice" else fin.EXPENSE_FIELDS
    return ValidationOut(entity_type=entity_type, entity_id=obj.id, status=obj.status, replay=replay,
                         open_exceptions=[exception_out(e) for e in open_items], document=_doc(obj, fields), ai=ai)


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except fin.NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except fin.NotAllowed as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


def _changes(model, raw: dict) -> dict:
    """Validate a partial update with the same rules as a full submission."""
    try:
        parsed = model.model_validate(raw)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=[
            {"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"]} for e in exc.errors()])
    changes = parsed.model_dump(exclude_unset=True, mode="python")
    if not changes:
        raise HTTPException(status_code=422, detail="no changes supplied")
    return changes


# ------------------------------------------------------------------ invoices

@router.post("/v1/invoices", response_model=ValidationOut, responses=ERR)
def submit_invoice(payload: InvoiceIn, db: Session = Depends(get_db),
                   idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=200)):
    """Submit an invoice and validate it immediately. Flagged invoices come back 'blocked'."""
    data = payload.model_dump(mode="python")
    invoice, replay = fin.submit_invoice(db, data, idempotency_key)
    invoice, open_items = _call(fin.validate_invoice, db, invoice.id)
    return _out("invoice", invoice, open_items, replay)


@router.post("/v1/invoices/from-text", response_model=ValidationOut, responses=ERR)
def invoice_from_text(payload: InvoiceTextIn, db: Session = Depends(get_db),
                      llm: LLMClient | None = Depends(_llm_or_none),
                      idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=200)):
    """AI reads the invoice text; the result is then validated exactly like a manual submission."""
    invoice, replay, ai = capture_invoice(db, payload.raw_text, idempotency_key, llm or _UnavailableLLM())
    invoice, open_items = _call(fin.validate_invoice, db, invoice.id)
    return _out("invoice", invoice, open_items, replay, ai)


@router.get("/v1/invoices/{invoice_id}", response_model=ValidationOut, responses=ERR)
def read_invoice(invoice_id: uuid.UUID, db: Session = Depends(get_db)):
    invoice = db.get(Invoice, invoice_id)
    if invoice is None:
        raise HTTPException(status_code=404, detail="invoice not found")
    return _out("invoice", invoice, exception_service.open_exceptions_for(db, "invoice", invoice.id))


@router.post("/v1/invoices/{invoice_id}/validate", response_model=ValidationOut, responses=ERR)
def validate_invoice(invoice_id: uuid.UUID, db: Session = Depends(get_db)):
    invoice, open_items = _call(fin.validate_invoice, db, invoice_id)
    return _out("invoice", invoice, open_items)


@router.post("/v1/invoices/{invoice_id}/correct", response_model=ValidationOut, responses=ERR)
def correct_invoice(invoice_id: uuid.UUID, payload: CorrectionIn, db: Session = Depends(get_db)):
    """Human fixes the data; the invoice is revalidated straight away (correct -> reprocess)."""
    changes = _changes(InvoiceIn, payload.changes)
    invoice, open_items = _call(fin.correct_invoice, db, invoice_id, changes, payload.corrected_by, payload.reason)
    return _out("invoice", invoice, open_items)


@router.post("/v1/invoices/{invoice_id}/approve", response_model=ValidationOut, responses=ERR)
def approve_invoice(invoice_id: uuid.UUID, payload: ActorIn, db: Session = Depends(get_db)):
    invoice = _call(fin.approve_invoice, db, invoice_id, payload.by)
    return _out("invoice", invoice, [])


@router.post("/v1/invoices/{invoice_id}/void", response_model=ValidationOut, responses=ERR)
def void_invoice(invoice_id: uuid.UUID, payload: ActorIn, db: Session = Depends(get_db)):
    invoice = _call(fin.void_invoice, db, invoice_id, payload.by, payload.reason or "voided")
    return _out("invoice", invoice, [])


@router.post("/v1/invoices/{invoice_id}/payment", response_model=ValidationOut, responses=ERR)
def record_payment(invoice_id: uuid.UUID, payload: PaymentIn, db: Session = Depends(get_db)):
    """Record a payment. Only an approved invoice can be paid (checked again by a database trigger)."""
    invoice = _call(fin.record_payment, db, invoice_id, paid_by=payload.by, amount=payload.amount,
                    reference=payload.reference)
    return _out("invoice", invoice, [])


@router.post("/v1/exceptions/{exception_id}/explanation", response_model=ExceptionOut, responses=ERR)
def attach_explanation(exception_id: uuid.UUID, payload: ExplanationIn, db: Session = Depends(get_db)):
    """Attach an AI-written explanation to a finding (used by the OpenAI node in n8n workflow 03).
    Advisory only: the AI can describe an exception but never resolve or downgrade it."""
    record = _call(fin.attach_explanation, db, exception_id, payload.explanation, payload.source)
    return exception_out(record)


# ------------------------------------------------------------------ expenses

@router.post("/v1/expenses", response_model=ValidationOut, responses=ERR)
def submit_expense(payload: ExpenseIn, db: Session = Depends(get_db),
                   idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=200)):
    expense, replay = fin.submit_expense(db, payload.model_dump(mode="python"), idempotency_key)
    expense, open_items = _call(fin.validate_expense, db, expense.id)
    return _out("expense", expense, open_items, replay)


@router.post("/v1/expenses/{expense_id}/correct", response_model=ValidationOut, responses=ERR)
def correct_expense(expense_id: uuid.UUID, payload: CorrectionIn, db: Session = Depends(get_db)):
    changes = _changes(ExpenseIn, payload.changes)
    expense, open_items = _call(fin.correct_expense, db, expense_id, changes, payload.corrected_by, payload.reason)
    return _out("expense", expense, open_items)


@router.post("/v1/expenses/{expense_id}/approve", response_model=ValidationOut, responses=ERR)
def approve_expense(expense_id: uuid.UUID, payload: ActorIn, db: Session = Depends(get_db)):
    expense = _call(fin.decide_expense, db, expense_id, "approve", payload.by, payload.reason)
    return _out("expense", expense, [])


@router.post("/v1/expenses/{expense_id}/reject", response_model=ValidationOut, responses=ERR)
def reject_expense(expense_id: uuid.UUID, payload: ActorIn, db: Session = Depends(get_db)):
    expense = _call(fin.decide_expense, db, expense_id, "reject", payload.by, payload.reason)
    return _out("expense", expense, [])


# ------------------------------------------------------------------ exception queue

@router.get("/v1/exceptions", response_model=list[ExceptionOut])
def list_exceptions(status_filter: str | None = Query(default="open", alias="status"),
                    entity_type: str | None = None, limit: int = Query(default=100, le=500),
                    db: Session = Depends(get_db)):
    """The human review queue (open items by default; ?status=resolved for history)."""
    return [exception_out(e) for e in fin.list_exceptions(db, status_filter, entity_type, limit)]


@router.post("/v1/exceptions/{exception_id}/accept", response_model=ValidationOut, responses=ERR)
def accept_exception(exception_id: uuid.UUID, payload: ActorIn, db: Session = Depends(get_db)):
    """Override a judgement-call finding (duplicate / over threshold). Others must be corrected."""
    if not payload.reason:
        raise HTTPException(status_code=422, detail="a reason is required to accept an exception")
    doc, open_items = _call(fin.accept_exception, db, exception_id, payload.by, payload.reason)
    return _out("invoice" if isinstance(doc, Invoice) else "expense", doc, open_items)
