"""Quote -> Order -> Invoice. Every document is derived from the APPROVAL, never from the request.

Each step is its own small transaction and is idempotent, so if anything fails half-way n8n simply
calls the same step again: nothing is duplicated and nothing is left half-created.
The database triggers from 001_schema.sql re-check every value, so even a bug in this file could
not produce a quote or order that differs from the approved terms.
"""
import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from app import audit
from app.config import get_settings
from app.models import Approval, Appointment, Customer, Inquiry, Invoice, Order, Product, Quote
from app.services import pricing

log = logging.getLogger("app.documents")


class NotFound(Exception):
    pass


class NotAllowed(Exception):
    """The current state does not allow this step (e.g. quote before approval)."""


class TermsMismatch(Exception):
    """The database refused the document because it does not match the approved terms."""


@dataclass
class DocResult:
    document: object
    replay: bool


def _lock_inquiry(db: Session, inquiry_id: uuid.UUID) -> Inquiry:
    inquiry = db.execute(select(Inquiry).where(Inquiry.id == inquiry_id).with_for_update()).scalar_one_or_none()
    if inquiry is None:
        raise NotFound("inquiry not found")
    return inquiry


def granted_approval(db: Session, inquiry_id: uuid.UUID) -> Approval | None:
    return db.execute(select(Approval).where(Approval.inquiry_id == inquiry_id,
                                             Approval.status.in_(("approved", "modified")))).scalar_one_or_none()


def _db_refused(exc: Exception) -> TermsMismatch:
    msg = str(getattr(exc, "orig", exc)).split("\n")[0]
    return TermsMismatch(f"database refused the document: {msg}")


# --------------------------------------------------------------------------- quote

def create_quote(db: Session, inquiry_id: uuid.UUID) -> DocResult:
    inquiry = _lock_inquiry(db, inquiry_id)
    approval = granted_approval(db, inquiry.id)
    if approval is None:
        db.rollback()
        raise NotAllowed(f"no granted approval for this inquiry (status '{inquiry.status}')")

    existing = db.execute(select(Quote).where(Quote.approval_id == approval.id)).scalar_one_or_none()
    if existing:
        db.rollback()
        return DocResult(existing, True)

    product = db.get(Product, approval.product_id)
    amounts = pricing.compute_amounts(approval.quantity, approval.unit_price,
                                      approval.approved_discount_pct, product.currency)
    quote = Quote(
        inquiry_id=inquiry.id, approval_id=approval.id, product_id=approval.product_id,
        quantity=amounts.quantity, unit_price=amounts.unit_price, discount_pct=amounts.discount_pct,
        subtotal=amounts.subtotal, discount_amount=amounts.discount_amount, total=amounts.total,
        currency=amounts.currency, terms_hash=approval.terms_hash,
        valid_until=date.today() + timedelta(days=get_settings().quote_valid_days),
    )
    try:
        db.add(quote)
        db.flush()
    except (IntegrityError, DBAPIError) as exc:
        db.rollback()
        raise _db_refused(exc) from exc

    inquiry.status = "quoted"
    audit.record(db, action="quote.issued", entity_type="quote", entity_id=quote.id, inquiry_id=inquiry.id,
                 after={"quote_number": quote.quote_number, "approval_id": approval.id,
                        **amounts.as_dict(), "terms_hash": approval.terms_hash})
    db.commit()
    log.info("Quote %s issued", quote.quote_number, extra={"inquiry_id": inquiry.id})
    return DocResult(quote, False)


# --------------------------------------------------------------------------- order

def create_order(db: Session, quote_id: uuid.UUID) -> DocResult:
    quote = db.execute(select(Quote).where(Quote.id == quote_id).with_for_update()).scalar_one_or_none()
    if quote is None:
        raise NotFound("quote not found")
    existing = db.execute(select(Order).where(Order.quote_id == quote.id)).scalar_one_or_none()
    if existing:
        db.rollback()
        return DocResult(existing, True)
    if quote.status != "issued":
        db.rollback()
        raise NotAllowed(f"quote is '{quote.status}', only an issued quote can become an order")
    if quote.valid_until and quote.valid_until < date.today():
        quote.status = "expired"
        db.commit()
        raise NotAllowed(f"quote expired on {quote.valid_until.isoformat()}")

    inquiry = _lock_inquiry(db, quote.inquiry_id)
    order = Order(
        quote_id=quote.id, inquiry_id=quote.inquiry_id, product_id=quote.product_id, quantity=quote.quantity,
        unit_price=quote.unit_price, discount_pct=quote.discount_pct, subtotal=quote.subtotal,
        discount_amount=quote.discount_amount, total=quote.total, currency=quote.currency,
        terms_hash=quote.terms_hash,
    )
    try:
        db.add(order)
        db.flush()
    except (IntegrityError, DBAPIError) as exc:
        db.rollback()
        raise _db_refused(exc) from exc

    quote.status = "accepted"
    inquiry.status = "ordered"
    audit.record(db, action="order.confirmed", entity_type="order", entity_id=order.id, inquiry_id=inquiry.id,
                 after={"order_number": order.order_number, "quote_number": quote.quote_number,
                        "total": order.total, "discount_pct": order.discount_pct,
                        "terms_hash": order.terms_hash})
    db.commit()
    log.info("Order %s confirmed", order.order_number, extra={"inquiry_id": inquiry.id})
    return DocResult(order, False)


# --------------------------------------------------------------------------- invoice

def invoice_fingerprint(counterparty: str | None, invoice_number: str | None, total: Decimal | None,
                        invoice_date: date | None) -> str:
    """Normalised fingerprint used by the duplicate detector (Step 7)."""
    parts = [(counterparty or "").strip().lower(), (invoice_number or "").strip().upper(),
             str(pricing.money(total)) if total is not None else "", invoice_date.isoformat() if invoice_date else ""]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def create_invoice(db: Session, order_id: uuid.UUID) -> DocResult:
    order = db.execute(select(Order).where(Order.id == order_id).with_for_update()).scalar_one_or_none()
    if order is None:
        raise NotFound("order not found")
    existing = db.execute(select(Invoice).where(Invoice.order_id == order.id, Invoice.direction == "outbound",
                                                Invoice.status != "void")).scalar_one_or_none()
    if existing:
        db.rollback()
        return DocResult(existing, True)
    if order.status == "cancelled":
        db.rollback()
        raise NotAllowed("order is cancelled")

    inquiry = _lock_inquiry(db, order.inquiry_id)
    customer = db.get(Customer, inquiry.customer_id) if inquiry.customer_id else None
    product = db.get(Product, order.product_id)
    number = "INV-" + str(db.execute(text("select nextval('invoice_number_seq')")).scalar_one()).zfill(6)
    today = date.today()
    counterparty = (customer.company or customer.name) if customer else (inquiry.customer_name or "Customer")

    invoice = Invoice(
        direction="outbound", invoice_number=number, order_id=order.id, counterparty_name=counterparty,
        invoice_date=today, due_date=today + timedelta(days=get_settings().invoice_due_days),
        currency=order.currency, subtotal=order.subtotal, discount_pct=order.discount_pct,
        discount_amount=order.discount_amount, tax_amount=Decimal("0.00"), total=order.total,
        line_items=[{"sku": product.sku, "description": product.name, "quantity": order.quantity,
                     "unit_price": str(order.unit_price), "discount_pct": str(order.discount_pct),
                     "line_total": str(order.total)}],
        source="generated", terms_hash=order.terms_hash, status="draft",
        fingerprint=invoice_fingerprint(counterparty, number, order.total, today),
    )
    db.add(invoice)
    db.flush()
    inquiry.status = "invoiced"
    audit.record(db, action="invoice.generated", entity_type="invoice", entity_id=invoice.id,
                 inquiry_id=inquiry.id,
                 after={"invoice_number": number, "order_number": order.order_number, "total": order.total,
                        "discount_pct": order.discount_pct, "terms_hash": order.terms_hash, "status": "draft"})
    db.commit()
    log.info("Invoice %s generated (draft, pending validation)", number, extra={"inquiry_id": inquiry.id})
    return DocResult(invoice, False)


# --------------------------------------------------------------------------- evidence

def commercial_summary(db: Session, inquiry_id: uuid.UUID) -> dict:
    """Every document side by side + a consistency verdict: approved == quoted == ordered == invoiced."""
    inquiry = db.get(Inquiry, inquiry_id)
    if inquiry is None:
        raise NotFound("inquiry not found")
    approval = granted_approval(db, inquiry.id)
    quote = db.execute(select(Quote).where(Quote.inquiry_id == inquiry.id)).scalar_one_or_none()
    order = db.execute(select(Order).where(Order.inquiry_id == inquiry.id)).scalar_one_or_none()
    invoice = db.execute(select(Invoice).where(Invoice.order_id == order.id, Invoice.direction == "outbound")
                         ).scalars().first() if order else None
    appointment = db.execute(select(Appointment).where(Appointment.inquiry_id == inquiry.id)).scalar_one_or_none()

    def row(doc, number_attr=None):
        if doc is None:
            return None
        pct = getattr(doc, "approved_discount_pct", None)
        pct = pct if pct is not None else getattr(doc, "discount_pct", None)
        out = {"id": str(doc.id), "status": doc.status,
               "discount_pct": str(pricing.money(pct)) if pct is not None else None,
               "terms_hash": doc.terms_hash}
        if number_attr:
            out["number"] = getattr(doc, number_attr)
        if hasattr(doc, "total") and doc.total is not None:
            out["total"] = str(doc.total)
        return out

    docs = {"approval": row(approval), "quote": row(quote, "quote_number"),
            "order": row(order, "order_number"), "invoice": row(invoice, "invoice_number")}
    present = [d for d in docs.values() if d]
    consistent = bool(present) and len({d["discount_pct"] for d in present}) == 1 \
        and len({d["terms_hash"] for d in present}) == 1
    if approval and len(present) > 1:
        totals = {d["total"] for d in present if "total" in d}
        consistent = consistent and len(totals) <= 1

    return {
        "inquiry_id": str(inquiry.id), "inquiry_status": inquiry.status,
        "requested_discount_pct": (inquiry.extracted or {}).get("requested_discount_pct"),
        "documents": docs,
        "appointment": {"id": str(appointment.id), "status": appointment.status,
                        "start": appointment.scheduled_start.isoformat(),
                        "timezone": appointment.timezone} if appointment else None,
        "consistent": consistent,
        "verdict": "approved terms match every downstream document" if consistent
                   else "MISMATCH between approved terms and a downstream document",
    }
