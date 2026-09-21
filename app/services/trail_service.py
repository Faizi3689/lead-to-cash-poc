"""Evidence trail for one inquiry: request -> AI output -> rule -> approval -> executed values ->
exceptions -> resolution, plus the raw append-only audit timeline behind it."""
import uuid

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import (AiRun, Appointment, Approval, AuditLog, Customer, ExceptionRecord, Inquiry, Invoice,
                        Order, Quote, RuleDecision)
from app.services import document_service


class NotFound(Exception):
    pass


def _s(v):
    return None if v is None else str(v)


def build_trail(db: Session, inquiry_id: uuid.UUID) -> dict:
    inquiry = db.get(Inquiry, inquiry_id)
    if inquiry is None:
        raise NotFound("inquiry not found")
    customer = db.get(Customer, inquiry.customer_id) if inquiry.customer_id else None

    ai_runs = db.execute(select(AiRun).where(AiRun.inquiry_id == inquiry.id).order_by(AiRun.created_at)).scalars().all()
    decision = db.execute(select(RuleDecision).where(RuleDecision.inquiry_id == inquiry.id)
                          .order_by(RuleDecision.created_at.desc())).scalars().first()
    approvals = db.execute(select(Approval).where(Approval.inquiry_id == inquiry.id)
                           .order_by(Approval.created_at)).scalars().all()
    quote = db.execute(select(Quote).where(Quote.inquiry_id == inquiry.id)).scalar_one_or_none()
    order = db.execute(select(Order).where(Order.inquiry_id == inquiry.id)).scalar_one_or_none()
    invoices = db.execute(select(Invoice).where(Invoice.order_id == order.id)).scalars().all() if order else []
    appointment = db.execute(select(Appointment).where(Appointment.inquiry_id == inquiry.id)).scalar_one_or_none()

    related = [inquiry.id] + [a.id for a in approvals] + [i.id for i in invoices]
    exceptions = db.execute(select(ExceptionRecord).where(ExceptionRecord.entity_id.in_(related))
                            .order_by(ExceptionRecord.created_at)).scalars().all()
    tracked = related + [e.id for e in exceptions]      # exception open/resolve events too
    timeline = db.execute(select(AuditLog).where(or_(AuditLog.inquiry_id == inquiry.id,
                                                     AuditLog.entity_id.in_(tracked)))
                          .order_by(AuditLog.occurred_at, AuditLog.id)).scalars().all()

    granted = next((a for a in approvals if a.status in ("approved", "modified")), None)
    summary = document_service.commercial_summary(db, inquiry.id)

    return {
        "inquiry_id": str(inquiry.id),
        "status": inquiry.status,
        "consistent": summary["consistent"],
        "verdict": summary["verdict"],
        "story": {
            "1_request": {"received_at": _s(inquiry.received_at), "source": inquiry.source,
                          "message": inquiry.raw_message, "idempotency_key": inquiry.idempotency_key,
                          "customer": {"name": customer.name, "email": customer.email, "company": customer.company}
                          if customer else None},
            "2_ai_output": {
                "attempts": [{"attempt": r.attempt, "provider": r.provider, "model": r.model,
                              "prompt_version": r.prompt_version, "outcome": r.outcome,
                              "confidence": _s(r.confidence), "latency_ms": r.latency_ms,
                              "validation_errors": r.validation_errors, "error": r.error_message}
                             for r in ai_runs],
                "accepted_run_id": _s(inquiry.accepted_ai_run_id),
                "extracted": inquiry.extracted,
            },
            "3_rule_decision": {"rule_version": decision.rule_version, "outcome": decision.outcome,
                                "reasons": decision.reasons, "inputs": decision.inputs} if decision else None,
            "4_approval": [{"approval_id": str(a.id), "required_role": a.required_role, "status": a.status,
                            "requested_discount_pct": _s(a.requested_discount_pct),
                            "approved_discount_pct": _s(a.approved_discount_pct),
                            "decided_by": a.decided_by, "decided_at": _s(a.decided_at),
                            "comment": a.decision_comment, "terms_hash": a.terms_hash} for a in approvals],
            "5_executed": {
                "approved_discount_pct": _s(granted.approved_discount_pct) if granted else None,
                "documents": summary["documents"],
                "all_invoices": [{"invoice_number": i.invoice_number, "direction": i.direction,
                                  "status": i.status, "discount_pct": _s(i.discount_pct), "total": _s(i.total)}
                                 for i in invoices],
                "appointment": {"start": _s(appointment.scheduled_start), "timezone": appointment.timezone,
                                "status": appointment.status, "requested": appointment.requested_text}
                if appointment else None,
            },
            "6_exceptions": [{"exception_id": str(e.id), "entity_type": e.entity_type, "type": e.exception_type,
                              "severity": e.severity, "status": e.status,
                              "message": (e.details or {}).get("message"), "opened_at": _s(e.created_at),
                              "resolved_by": e.resolved_by, "resolved_at": _s(e.resolved_at),
                              "resolution": e.resolution} for e in exceptions],
        },
        "timeline": [{"at": _s(a.occurred_at), "action": a.action, "actor": a.actor,
                      "entity_type": a.entity_type, "entity_id": _s(a.entity_id), "request_id": a.request_id,
                      "before": a.before, "after": a.after, "meta": a.meta} for a in timeline],
    }
