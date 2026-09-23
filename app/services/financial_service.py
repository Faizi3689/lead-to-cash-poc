"""Invoice & expense exception handling: submit -> validate -> (block -> correct / accept / void)
-> revalidate -> approve.

Rules:
  * Validation is deterministic (app/rules/financial_checks.py). AI never clears an exception.
  * Anything flagged is BLOCKED; the database itself refuses to approve a document with an open
    exception (trigger block_if_open_exceptions).
  * Humans resolve by correcting data (then we revalidate), accepting an overridable finding
    (duplicate / over-threshold), or voiding the document.
"""
import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app import audit
from app.config import get_settings
from app.models import ExceptionRecord, Expense, Invoice, Order
from app.rules import financial_checks as checks
from app.services import exception_service, pricing
from app.services.document_service import invoice_fingerprint

log = logging.getLogger("app.financial")

INVOICE_FIELDS = ("invoice_number", "counterparty_name", "order_reference", "invoice_date", "due_date",
                  "currency", "subtotal", "discount_pct", "discount_amount", "tax_amount", "total", "line_items")
EXPENSE_FIELDS = ("expense_ref", "employee_name", "category", "amount", "currency", "expense_date",
                  "description", "receipt_ref")
EDITABLE_INVOICE_STATUSES = ("draft", "received", "validated", "blocked")
EDITABLE_EXPENSE_STATUSES = ("submitted", "validated", "blocked")


class NotFound(Exception):
    pass


class NotAllowed(Exception):
    pass


# --------------------------------------------------------------------------- helpers

def _values(doc, fields) -> dict:
    return {f: getattr(doc, f) for f in fields}


def data_hash(doc, fields) -> str:
    """Fingerprint of the document's content. An acceptance only holds while the data is unchanged."""
    return hashlib.sha256(json.dumps(_values(doc, fields), default=str, sort_keys=True).encode()).hexdigest()


def _merge(findings: list[checks.Finding]) -> dict[str, checks.Finding]:
    """One exception per type: several missing-field findings become one with all fields listed."""
    merged: dict[str, checks.Finding] = {}
    for f in findings:
        if f.type not in merged:
            merged[f.type] = checks.Finding(f.type, f.severity, f.message, dict(f.details))
            continue
        m = merged[f.type]
        m.message = f"{m.message}; {f.message}"
        if "fields" in f.details:
            m.details["fields"] = sorted(set(m.details.get("fields", [])) | set(f.details["fields"]))
        m.details.setdefault("also", []).append(f.details)
        if f.severity == "critical":
            m.severity = "critical"
    return merged


def _accepted_types(db: Session, entity_type: str, entity_id: uuid.UUID, current_hash: str) -> set[str]:
    rows = db.execute(select(ExceptionRecord).where(
        ExceptionRecord.entity_type == entity_type, ExceptionRecord.entity_id == entity_id,
        ExceptionRecord.status == "resolved")).scalars()
    return {r.exception_type for r in rows
            if (r.resolution or {}).get("action") == "accept"
            and (r.resolution or {}).get("data_hash") == current_hash}


def _apply_findings(db: Session, entity_type: str, doc, findings: list[checks.Finding], fields,
                    actor: str) -> list[ExceptionRecord]:
    """Open new exceptions, clear the ones no longer detected, return what is still open."""
    merged = _merge(findings)
    current_hash = data_hash(doc, fields)
    accepted = _accepted_types(db, entity_type, doc.id, current_hash)
    for etype in accepted:
        merged.pop(etype, None)

    for f in merged.values():
        exc_id = exception_service.open_exception(
            db, entity_type=entity_type, entity_id=doc.id, exception_type=f.type,
            details={"message": f.message, **f.details}, severity=f.severity,
            assigned_role="finance")
        record = db.get(ExceptionRecord, exc_id)
        record.details = audit._jsonable({"message": f.message, **f.details})   # keep details current

    for record in exception_service.open_exceptions_for(db, entity_type, doc.id):
        if record.exception_type in checks.VALIDATOR_TYPES and record.exception_type not in merged:
            exception_service.resolve(db, record, resolved_by=actor,
                                      resolution={"action": "cleared", "reason": "no longer detected on revalidation",
                                                  "data_hash": current_hash})
    db.flush()
    return exception_service.open_exceptions_for(db, entity_type, doc.id)


# --------------------------------------------------------------------------- invoices

def _order_for(db: Session, inv: Invoice) -> Order | None:
    if inv.order_id:
        return db.get(Order, inv.order_id)
    if inv.order_reference:
        return db.execute(select(Order).where(func.upper(Order.order_number) ==
                                              inv.order_reference.strip().upper())).scalar_one_or_none()
    return None


def invoice_findings(db: Session, inv: Invoice) -> list[checks.Finding]:
    s = get_settings()
    values = _values(inv, INVOICE_FIELDS)
    order = _order_for(db, inv)
    if order and not inv.order_id:
        inv.order_id = order.id          # inbound invoice matched to the order it references

    findings = checks.missing_fields(values, checks.INVOICE_REQUIRED)
    findings += checks.invoice_arithmetic(values)
    findings += checks.against_order(
        values, {"order_number": order.order_number, "discount_pct": order.discount_pct, "total": order.total}
        if order else None, inv.order_reference if not order else None)

    others = select(Invoice).where(Invoice.id != inv.id, Invoice.status != "void",
                                   Invoice.direction == inv.direction)
    exact = []
    if inv.counterparty_name and inv.invoice_number:
        exact = db.execute(others.where(
            func.lower(Invoice.counterparty_name) == inv.counterparty_name.strip().lower(),
            func.upper(Invoice.invoice_number) == inv.invoice_number.strip().upper())).scalars().all()
    suspected = []
    if not exact and inv.counterparty_name and inv.total is not None and inv.invoice_date:
        candidates = others.where(
            func.lower(Invoice.counterparty_name) == inv.counterparty_name.strip().lower(),
            Invoice.total == inv.total, Invoice.invoice_date == inv.invoice_date)
        if inv.order_id:
            # Invoices for two different orders can legitimately have the same amount on the same day.
            candidates = candidates.where((Invoice.order_id.is_(None)) | (Invoice.order_id == inv.order_id))
        suspected = db.execute(candidates).scalars().all()
    findings += checks.duplicates(
        [{"id": str(m.id), "ref": m.invoice_number or str(m.id)} for m in exact],
        [{"id": str(m.id), "ref": m.invoice_number or str(m.id)} for m in suspected])

    findings += checks.over_threshold(inv.total, s.invoice_approval_threshold, "invoice total", "finance manager")
    return findings


def validate_invoice(db: Session, invoice_id: uuid.UUID, actor: str = "system") -> tuple[Invoice, list]:
    inv = db.execute(select(Invoice).where(Invoice.id == invoice_id).with_for_update()).scalar_one_or_none()
    if inv is None:
        raise NotFound("invoice not found")
    if inv.status in ("approved", "posted", "void"):
        return inv, exception_service.open_exceptions_for(db, "invoice", inv.id)

    open_items = _apply_findings(db, "invoice", inv, invoice_findings(db, inv), INVOICE_FIELDS, actor)
    before = inv.status
    inv.status = "blocked" if open_items else "validated"
    audit.record(db, action=f"invoice.{inv.status}", entity_type="invoice", entity_id=inv.id, actor=actor,
                 before={"status": before},
                 after={"status": inv.status, "open_exceptions": [e.exception_type for e in open_items]})
    db.commit()
    log.info("Invoice %s %s", inv.invoice_number, inv.status)
    return inv, open_items


def submit_invoice(db: Session, data: dict, idempotency_key: str | None, source: str = "manual",
                   raw_text: str | None = None) -> tuple[Invoice, bool]:
    """Store an inbound invoice. A retried submission (same key) returns the original record."""
    if idempotency_key:
        existing = db.execute(select(Invoice).where(Invoice.idempotency_key == idempotency_key)).scalar_one_or_none()
        if existing:
            return existing, True
    inv = Invoice(direction="inbound", source=source, raw_text=raw_text, idempotency_key=idempotency_key,
                  status="received", **{k: v for k, v in data.items() if k in INVOICE_FIELDS})
    inv.line_items = audit._jsonable(data.get("line_items") or [])
    inv.fingerprint = invoice_fingerprint(inv.counterparty_name, inv.invoice_number, inv.total, inv.invoice_date)
    db.add(inv)
    db.flush()
    audit.record(db, action="invoice.received", entity_type="invoice", entity_id=inv.id,
                 after={"source": source, **{k: data.get(k) for k in INVOICE_FIELDS if k != "line_items"}})
    db.commit()
    return inv, False


def correct_invoice(db: Session, invoice_id: uuid.UUID, changes: dict, corrected_by: str,
                    reason: str) -> tuple[Invoice, list]:
    inv = db.execute(select(Invoice).where(Invoice.id == invoice_id).with_for_update()).scalar_one_or_none()
    if inv is None:
        raise NotFound("invoice not found")
    if inv.status not in EDITABLE_INVOICE_STATUSES:
        db.rollback()
        raise NotAllowed(f"invoice is '{inv.status}' and can no longer be corrected")
    before = {k: getattr(inv, k) for k in changes}
    for k, v in changes.items():
        setattr(inv, k, audit._jsonable(v) if k == "line_items" else v)
    if "order_reference" in changes and inv.direction == "inbound":
        inv.order_id = None              # re-match against the corrected reference
    inv.fingerprint = invoice_fingerprint(inv.counterparty_name, inv.invoice_number, inv.total, inv.invoice_date)
    for record in exception_service.open_exceptions_for(db, "invoice", inv.id):
        if record.exception_type in checks.AI_CAPTURE_TYPES:
            exception_service.resolve(db, record, resolved_by=f"user:{corrected_by}",
                                      resolution={"action": "corrected", "reason": "data reviewed and corrected by a human",
                                                  "changes": audit._jsonable(changes)})
    audit.record(db, action="invoice.corrected", entity_type="invoice", entity_id=inv.id,
                 actor=f"user:{corrected_by}", before=before, after=changes, meta={"reason": reason})
    db.flush()
    return validate_invoice(db, inv.id, actor=f"user:{corrected_by}")


def approve_invoice(db: Session, invoice_id: uuid.UUID, approved_by: str) -> Invoice:
    inv = db.execute(select(Invoice).where(Invoice.id == invoice_id).with_for_update()).scalar_one_or_none()
    if inv is None:
        raise NotFound("invoice not found")
    if inv.status == "approved":
        return inv
    if inv.status != "validated":
        db.rollback()
        raise NotAllowed(f"invoice is '{inv.status}'; only a validated invoice can be approved")
    try:
        inv.status = "approved"
        db.flush()          # the DB trigger re-checks for open exceptions here
    except DBAPIError as exc:
        db.rollback()
        raise NotAllowed(str(getattr(exc, "orig", exc)).split("\n")[0]) from exc
    audit.record(db, action="invoice.approved", entity_type="invoice", entity_id=inv.id,
                 actor=f"user:{approved_by}", before={"status": "validated"}, after={"status": "approved"})
    db.commit()
    return inv


def void_invoice(db: Session, invoice_id: uuid.UUID, voided_by: str, reason: str) -> Invoice:
    inv = db.execute(select(Invoice).where(Invoice.id == invoice_id).with_for_update()).scalar_one_or_none()
    if inv is None:
        raise NotFound("invoice not found")
    if inv.status in ("approved", "posted"):
        db.rollback()
        raise NotAllowed(f"invoice is '{inv.status}'; issue a credit note instead of voiding")
    for record in exception_service.open_exceptions_for(db, "invoice", inv.id):
        exception_service.resolve(db, record, resolved_by=f"user:{voided_by}",
                                  resolution={"action": "voided", "reason": reason})
    before = inv.status
    inv.status = "void"
    audit.record(db, action="invoice.voided", entity_type="invoice", entity_id=inv.id,
                 actor=f"user:{voided_by}", before={"status": before}, after={"status": "void"},
                 meta={"reason": reason})
    db.commit()
    return inv


def record_payment(db: Session, invoice_id: uuid.UUID, *, paid_by: str, amount: Decimal | None,
                   reference: str | None) -> Invoice:
    """Mark an approved invoice as (partially) paid. Money never moves on a blocked invoice."""
    inv = db.execute(select(Invoice).where(Invoice.id == invoice_id).with_for_update()).scalar_one_or_none()
    if inv is None:
        raise NotFound("invoice not found")
    if inv.status not in ("approved", "posted"):
        db.rollback()
        raise NotAllowed(f"invoice is '{inv.status}'; only an approved invoice can be paid")
    if inv.total is None:
        db.rollback()
        raise NotAllowed("invoice has no total")
    outstanding = pricing.money(inv.total - (inv.amount_paid or Decimal("0")))
    if outstanding <= 0:
        return inv
    value = pricing.money(amount) if amount is not None else outstanding
    if value <= 0 or value > outstanding:
        db.rollback()
        raise NotAllowed(f"payment {value} must be between 0 and the outstanding {outstanding}")

    before = {"payment_status": inv.payment_status, "amount_paid": str(inv.amount_paid)}
    inv.amount_paid = pricing.money((inv.amount_paid or Decimal("0")) + value)
    fully_paid = inv.amount_paid >= inv.total
    inv.payment_status = "paid" if fully_paid else "partially_paid"
    inv.payment_reference = reference or inv.payment_reference
    if fully_paid:
        inv.paid_at = datetime.now(timezone.utc)
        inv.status = "posted"
    try:
        db.flush()          # the DB trigger re-checks approval state and the amount
    except DBAPIError as exc:
        db.rollback()
        raise NotAllowed(str(getattr(exc, "orig", exc)).split("\n")[0]) from exc
    audit.record(db, action="invoice.payment_recorded", entity_type="invoice", entity_id=inv.id,
                 actor=f"user:{paid_by}", before=before,
                 after={"payment_status": inv.payment_status, "amount_paid": str(inv.amount_paid),
                        "payment": str(value), "reference": reference})
    db.commit()
    return inv


def attach_explanation(db: Session, exception_id: uuid.UUID, text_: str, source: str):
    """Store an AI-written, human-readable explanation of a finding. Advisory only: it never
    changes the status of the exception or of the document."""
    record = db.get(ExceptionRecord, exception_id)
    if record is None:
        raise NotFound("exception not found")
    record.ai_explanation = text_[:4000]
    audit.record(db, action="exception.explained", entity_type="exception", entity_id=record.id,
                 actor=source, after={"explanation": record.ai_explanation},
                 meta={"advisory_only": True})
    db.commit()
    return record


# --------------------------------------------------------------------------- expenses

def expense_fingerprint(e: Expense) -> str:
    parts = [(e.employee_name or "").strip().lower(), (e.category or "").strip().lower(),
             str(pricing.money(e.amount)) if e.amount is not None else "",
             e.expense_date.isoformat() if e.expense_date else ""]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def expense_findings(db: Session, e: Expense) -> list[checks.Finding]:
    s = get_settings()
    values = _values(e, EXPENSE_FIELDS)
    findings = checks.missing_fields(values, checks.EXPENSE_REQUIRED)
    findings += checks.expense_receipt(values, s.receipt_required_above)
    findings += checks.over_threshold(e.amount, s.expense_approval_threshold, "expense", "manager")
    if e.employee_name and e.amount is not None and e.expense_date:
        dupes = db.execute(select(Expense).where(Expense.id != e.id, Expense.fingerprint == e.fingerprint,
                                                 Expense.status.notin_(("rejected",)))).scalars().all()
        if dupes:
            findings.append(checks.Finding(
                "duplicate_invoice", "high",
                "same employee, category, amount and date as an existing expense claim",
                {"kind": "exact", "matches": [str(d.id) for d in dupes]}))
    return findings


def submit_expense(db: Session, data: dict, idempotency_key: str | None) -> tuple[Expense, bool]:
    if idempotency_key:
        existing = db.execute(select(Expense).where(Expense.idempotency_key == idempotency_key)).scalar_one_or_none()
        if existing:
            return existing, True
    e = Expense(idempotency_key=idempotency_key, **{k: v for k, v in data.items() if k in EXPENSE_FIELDS})
    e.fingerprint = expense_fingerprint(e)
    db.add(e)
    db.flush()
    audit.record(db, action="expense.submitted", entity_type="expense", entity_id=e.id,
                 after={k: data.get(k) for k in EXPENSE_FIELDS})
    db.commit()
    return e, False


def validate_expense(db: Session, expense_id: uuid.UUID, actor: str = "system") -> tuple[Expense, list]:
    e = db.execute(select(Expense).where(Expense.id == expense_id).with_for_update()).scalar_one_or_none()
    if e is None:
        raise NotFound("expense not found")
    if e.status in ("approved", "rejected", "paid"):
        return e, exception_service.open_exceptions_for(db, "expense", e.id)
    open_items = _apply_findings(db, "expense", e, expense_findings(db, e), EXPENSE_FIELDS, actor)
    before = e.status
    e.status = "blocked" if open_items else "validated"
    audit.record(db, action=f"expense.{e.status}", entity_type="expense", entity_id=e.id, actor=actor,
                 before={"status": before},
                 after={"status": e.status, "open_exceptions": [x.exception_type for x in open_items]})
    db.commit()
    return e, open_items


def correct_expense(db: Session, expense_id: uuid.UUID, changes: dict, corrected_by: str,
                    reason: str) -> tuple[Expense, list]:
    e = db.execute(select(Expense).where(Expense.id == expense_id).with_for_update()).scalar_one_or_none()
    if e is None:
        raise NotFound("expense not found")
    if e.status not in EDITABLE_EXPENSE_STATUSES:
        db.rollback()
        raise NotAllowed(f"expense is '{e.status}' and can no longer be corrected")
    before = {k: getattr(e, k) for k in changes}
    for k, v in changes.items():
        setattr(e, k, v)
    e.fingerprint = expense_fingerprint(e)
    audit.record(db, action="expense.corrected", entity_type="expense", entity_id=e.id,
                 actor=f"user:{corrected_by}", before=before, after=changes, meta={"reason": reason})
    db.flush()
    return validate_expense(db, e.id, actor=f"user:{corrected_by}")


def decide_expense(db: Session, expense_id: uuid.UUID, action: str, decided_by: str,
                   reason: str | None = None) -> Expense:
    e = db.execute(select(Expense).where(Expense.id == expense_id).with_for_update()).scalar_one_or_none()
    if e is None:
        raise NotFound("expense not found")
    if action == "approve":
        if e.status == "approved":
            return e
        if e.status != "validated":
            db.rollback()
            raise NotAllowed(f"expense is '{e.status}'; only a validated expense can be approved")
        try:
            e.status = "approved"
            db.flush()
        except DBAPIError as exc:
            db.rollback()
            raise NotAllowed(str(getattr(exc, "orig", exc)).split("\n")[0]) from exc
    else:
        if e.status in ("approved", "paid"):
            db.rollback()
            raise NotAllowed(f"expense is already '{e.status}'")
        for record in exception_service.open_exceptions_for(db, "expense", e.id):
            exception_service.resolve(db, record, resolved_by=f"user:{decided_by}",
                                      resolution={"action": "rejected", "reason": reason})
        e.status = "rejected"
    audit.record(db, action=f"expense.{e.status}", entity_type="expense", entity_id=e.id,
                 actor=f"user:{decided_by}", after={"status": e.status}, meta={"reason": reason})
    db.commit()
    return e


# --------------------------------------------------------------------------- exception queue

def accept_exception(db: Session, exception_id: uuid.UUID, accepted_by: str, note: str) -> tuple:
    """Human override for findings that are a judgement call (duplicate / over threshold)."""
    record = db.execute(select(ExceptionRecord).where(ExceptionRecord.id == exception_id)
                        .with_for_update()).scalar_one_or_none()
    if record is None:
        raise NotFound("exception not found")
    if record.status not in exception_service.OPEN:
        db.rollback()
        raise NotAllowed(f"exception is already '{record.status}'")
    if record.exception_type not in checks.OVERRIDABLE:
        db.rollback()
        raise NotAllowed(f"'{record.exception_type}' cannot be accepted: correct the document or void it")
    if record.entity_type not in ("invoice", "expense"):
        db.rollback()
        raise NotAllowed("only invoice and expense exceptions are handled here")

    model, fields = (Invoice, INVOICE_FIELDS) if record.entity_type == "invoice" else (Expense, EXPENSE_FIELDS)
    doc = db.get(model, record.entity_id)
    exception_service.resolve(db, record, resolved_by=f"user:{accepted_by}",
                              resolution={"action": "accept", "note": note, "data_hash": data_hash(doc, fields)})
    db.flush()
    if record.entity_type == "invoice":
        return validate_invoice(db, doc.id, actor=f"user:{accepted_by}")
    return validate_expense(db, doc.id, actor=f"user:{accepted_by}")


def list_exceptions(db: Session, status: str | None, entity_type: str | None, limit: int = 100):
    q = select(ExceptionRecord).order_by(ExceptionRecord.created_at.desc()).limit(limit)
    if status:
        q = q.where(ExceptionRecord.status == status)
    if entity_type:
        q = q.where(ExceptionRecord.entity_type == entity_type)
    return db.execute(q).scalars().all()
