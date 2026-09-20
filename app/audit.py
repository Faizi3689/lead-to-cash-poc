"""Append-only audit trail helper. Call inside the same transaction as the change it records."""
import json
import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.models import AuditLog
from app.request_context import current_request_id


def _jsonable(value: Any) -> Any:
    """Make UUIDs, Decimals and datetimes JSON-safe for JSONB columns."""
    if value is None:
        return None
    return json.loads(json.dumps(value, default=str))


def record(
    db: Session,
    *,
    action: str,
    entity_type: str,
    entity_id: uuid.UUID | None,
    actor: str = "system",
    inquiry_id: uuid.UUID | None = None,
    before: dict | None = None,
    after: dict | None = None,
    meta: dict | None = None,
) -> None:
    db.add(
        AuditLog(
            request_id=current_request_id(),
            inquiry_id=inquiry_id,
            entity_type=entity_type,
            entity_id=entity_id,
            action=action,
            actor=actor,
            before=_jsonable(before),
            after=_jsonable(after),
            meta=_jsonable(meta or {}),
        )
    )
