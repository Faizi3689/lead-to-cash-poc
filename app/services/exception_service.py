"""Open / resolve rows in the human-review exception queue."""
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit
from app.models import ExceptionRecord

OPEN = ("open", "in_review")


def open_exception(db: Session, *, entity_type: str, entity_id: uuid.UUID, exception_type: str,
                   details: dict, severity: str = "high", assigned_role: str = "sales_ops",
                   inquiry_id: uuid.UUID | None = None) -> uuid.UUID:
    """Idempotent: returns the existing open exception of the same type instead of duplicating it."""
    existing = db.execute(
        select(ExceptionRecord.id).where(
            ExceptionRecord.entity_type == entity_type,
            ExceptionRecord.entity_id == entity_id,
            ExceptionRecord.exception_type == exception_type,
            ExceptionRecord.status.in_(OPEN),
        )
    ).scalar_one_or_none()
    if existing:
        return existing
    record = ExceptionRecord(entity_type=entity_type, entity_id=entity_id, exception_type=exception_type,
                             severity=severity, details=audit._jsonable(details), assigned_role=assigned_role)
    db.add(record)
    db.flush()
    audit.record(db, action="exception.opened", entity_type="exception", entity_id=record.id,
                 inquiry_id=inquiry_id, after={"type": exception_type, "entity": entity_type,
                                               "entity_id": entity_id, "details": details})
    return record.id


def open_exceptions_for(db: Session, entity_type: str, entity_id: uuid.UUID) -> list[ExceptionRecord]:
    return list(db.execute(
        select(ExceptionRecord).where(ExceptionRecord.entity_type == entity_type,
                                      ExceptionRecord.entity_id == entity_id,
                                      ExceptionRecord.status.in_(OPEN))
        .order_by(ExceptionRecord.created_at)
    ).scalars())


def resolve(db: Session, record: ExceptionRecord, *, resolved_by: str, resolution: dict,
            inquiry_id: uuid.UUID | None = None) -> None:
    record.status = "resolved"
    record.resolved_by = resolved_by
    record.resolved_at = datetime.now(timezone.utc)
    record.resolution = audit._jsonable(resolution)
    audit.record(db, action="exception.resolved", entity_type="exception", entity_id=record.id,
                 inquiry_id=inquiry_id, actor=resolved_by,
                 after={"type": record.exception_type, "resolution": resolution})
