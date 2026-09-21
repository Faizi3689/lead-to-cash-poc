"""Operational endpoints used by n8n itself (failure reporting)."""
import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app import audit
from app.db import get_db
from app.models import Inquiry
from app.security import require_api_key
from app.services import exception_service

router = APIRouter(prefix="/v1/ops", tags=["ops"], dependencies=[Depends(require_api_key)])


class WorkflowFailureIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    workflow: str = Field(max_length=200)
    stage: str | None = Field(default=None, max_length=200)
    error: str | None = Field(default=None, max_length=2000)
    execution_id: str | None = Field(default=None, max_length=100)
    execution_url: str | None = Field(default=None, max_length=500)
    inquiry_id: uuid.UUID | None = None


class WorkflowFailureOut(BaseModel):
    recorded: bool
    exception_id: uuid.UUID | None = None


@router.post("/workflow-failures", response_model=WorkflowFailureOut)
def workflow_failure(payload: WorkflowFailureIn, db: Session = Depends(get_db)) -> WorkflowFailureOut:
    """n8n reports a step that still failed after its retries. It lands in the audit log and,
    when the inquiry is known, in the human exception queue - nothing fails silently."""
    exception_id = None
    inquiry = db.get(Inquiry, payload.inquiry_id) if payload.inquiry_id else None
    if inquiry:
        exception_id = exception_service.open_exception(
            db, entity_type="inquiry", entity_id=inquiry.id, exception_type="downstream_failure",
            details={"service": "n8n", **payload.model_dump(mode="json")}, severity="high",
            assigned_role="ops", inquiry_id=inquiry.id)
    audit.record(db, action="n8n.workflow_failed", entity_type="inquiry" if inquiry else "workflow",
                 entity_id=inquiry.id if inquiry else None, inquiry_id=inquiry.id if inquiry else None,
                 actor="n8n", meta=payload.model_dump(mode="json"))
    db.commit()
    return WorkflowFailureOut(recorded=True, exception_id=exception_id)
