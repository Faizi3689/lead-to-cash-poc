"""Inquiry intake: validate-once, store-once. Safe against duplicate webhooks and n8n retries."""
import hashlib
import json
import logging
import uuid

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import audit
from app.models import Customer, Inquiry
from app.request_context import current_request_id
from app.schemas import InquiryCreate

log = logging.getLogger("app.inquiries")


class IdempotencyConflict(Exception):
    """Same Idempotency-Key reused with a different request body."""


def request_hash(payload: InquiryCreate) -> str:
    canonical = json.dumps(payload.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _get_or_create_customer(db: Session, name: str | None, email: str) -> uuid.UUID:
    existing = db.execute(
        select(Customer.id).where(func.lower(Customer.email) == email.lower())
    ).scalar_one_or_none()
    if existing:
        return existing
    try:
        with db.begin_nested():  # savepoint: a concurrent insert of the same email must not break us
            customer = Customer(name=name or email.split("@")[0], email=email)
            db.add(customer)
            db.flush()
            audit.record(db, action="customer.created", entity_type="customer",
                         entity_id=customer.id, after={"name": customer.name, "email": email})
            return customer.id
    except IntegrityError:
        return db.execute(
            select(Customer.id).where(func.lower(Customer.email) == email.lower())
        ).scalar_one()


def create_inquiry(db: Session, payload: InquiryCreate, idempotency_key: str) -> tuple[Inquiry, bool]:
    """Returns (inquiry, is_duplicate). Raises IdempotencyConflict on key reuse with a different body."""
    body_hash = request_hash(payload)

    customer_id = None
    if payload.customer_email:
        customer_id = _get_or_create_customer(db, payload.customer_name, payload.customer_email)

    # INSERT ... ON CONFLICT DO NOTHING is atomic: two identical webhooks arriving at the
    # same moment can never both create an inquiry.
    new_id = db.execute(
        pg_insert(Inquiry)
        .values(
            idempotency_key=idempotency_key,
            request_id=current_request_id(),
            request_hash=body_hash,
            source=payload.source,
            customer_id=customer_id,
            customer_name=payload.customer_name,
            customer_email=payload.customer_email,
            raw_message=payload.message,
        )
        .on_conflict_do_nothing(index_elements=["idempotency_key"])
        .returning(Inquiry.id)
    ).scalar_one_or_none()

    if new_id is None:
        existing = db.execute(
            select(Inquiry).where(Inquiry.idempotency_key == idempotency_key)
        ).scalar_one()
        if existing.request_hash != body_hash:
            db.rollback()
            log.warning("Idempotency key reused with a different body", extra={"inquiry_id": existing.id})
            raise IdempotencyConflict(str(existing.id))
        audit.record(db, action="inquiry.duplicate_ignored", entity_type="inquiry",
                     entity_id=existing.id, inquiry_id=existing.id, actor="system",
                     meta={"idempotency_key": idempotency_key})
        db.commit()
        log.info("Duplicate inquiry delivery ignored", extra={"inquiry_id": existing.id})
        return existing, True

    audit.record(db, action="inquiry.received", entity_type="inquiry", entity_id=new_id,
                 inquiry_id=new_id, actor="n8n",
                 after={"source": payload.source, "customer_id": customer_id,
                        "message": payload.message, "idempotency_key": idempotency_key})
    db.commit()
    log.info("Inquiry received", extra={"inquiry_id": new_id})
    return db.get(Inquiry, new_id), False


def get_inquiry(db: Session, inquiry_id: uuid.UUID) -> Inquiry | None:
    return db.get(Inquiry, inquiry_id)
