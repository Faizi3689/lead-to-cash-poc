"""AI extraction with guardrails.

Flow: lock inquiry -> call LLM (outside any DB transaction) -> parse -> schema-validate ->
ground numbers against the message -> resolve product deterministically -> decide:
  extracted     : everything valid, confident and grounded -> ready for the rules engine
  needs_review  : anything uncertain -> human review queue (exception opened)
The AI only proposes data. It never approves, prices or discounts anything.
"""
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import audit
from app.config import get_settings
from app.extraction.prompt import PROMPT_VERSION, build_messages, retry_feedback
from app.extraction.validation import ExtractedInquiry, grounding_issues, parse_and_validate
from app.llm.base import LLMClient, LLMUnavailable
from app.models import AiRun, Inquiry, Product
from app.services import exception_service

log = logging.getLogger("app.extraction")

# Exceptions a re-run is allowed to clear (system/AI failures). Anything else needs a human.
RETRYABLE_EXCEPTIONS = {"llm_unavailable", "invalid_ai_output"}
STALE_EXTRACTING_AFTER = timedelta(minutes=5)


class InquiryNotFound(Exception):
    pass


class ExtractionNotAllowed(Exception):
    """Raised when the inquiry's state does not allow (re-)extraction."""


@dataclass
class ExtractionResult:
    inquiry: Inquiry
    outcome: str                      # 'extracted' | 'needs_review'
    attempts: int
    replay: bool = False
    confidence: float | None = None
    product: Product | None = None
    reasons: list[str] = field(default_factory=list)
    exception_ids: list[uuid.UUID] = field(default_factory=list)


def resolve_product(db: Session, product_name: str | None) -> Product | None:
    """Deterministic catalog match on SKU, name or alias (case-insensitive). No fuzzy guessing."""
    if not product_name:
        return None
    needle = product_name.strip().lower()
    return db.execute(
        select(Product).where(
            Product.is_active.is_(True),
            (func.lower(Product.sku) == needle)
            | (func.lower(Product.name) == needle)
            | (Product.aliases.any(needle)),
        )
    ).scalars().first()


# --------------------------------------------------------------------------- state handling

def _start(db: Session, inquiry_id: uuid.UUID) -> Inquiry | ExtractionResult:
    """Lock the inquiry and move it to 'extracting'. Returns a replay result if already done."""
    inquiry = db.execute(select(Inquiry).where(Inquiry.id == inquiry_id).with_for_update()).scalar_one_or_none()
    if inquiry is None:
        raise InquiryNotFound(str(inquiry_id))

    if inquiry.status not in ("received", "needs_review", "extracting"):
        # Already extracted (or further): duplicate call from an n8n retry -> return stored result.
        db.rollback()
        return _replay(db, inquiry)

    if inquiry.status == "extracting":
        updated = inquiry.updated_at if inquiry.updated_at.tzinfo else inquiry.updated_at.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - updated
        if age < STALE_EXTRACTING_AFTER:
            db.rollback()
            raise ExtractionNotAllowed("extraction already in progress for this inquiry")
        log.warning("Recovering stale 'extracting' inquiry", extra={"inquiry_id": inquiry.id})

    if inquiry.status == "needs_review":
        open_items = exception_service.open_exceptions_for(db, "inquiry", inquiry.id)
        blocking = [e.exception_type for e in open_items if e.exception_type not in RETRYABLE_EXCEPTIONS]
        if blocking:
            db.rollback()
            raise ExtractionNotAllowed(
                f"inquiry requires human review ({', '.join(blocking)}); re-running the AI cannot clear it")
        for exc in open_items:  # only system failures remain: a re-run supersedes them
            exception_service.resolve(db, exc, resolved_by="system", inquiry_id=inquiry.id,
                                      resolution={"reason": "superseded by re-extraction"})

    before = inquiry.status
    inquiry.status = "extracting"
    inquiry.updated_at = datetime.now(timezone.utc)
    audit.record(db, action="ai.extraction_started", entity_type="inquiry", entity_id=inquiry.id,
                 inquiry_id=inquiry.id, before={"status": before}, after={"status": "extracting"})
    db.commit()
    return inquiry


def _replay(db: Session, inquiry: Inquiry) -> ExtractionResult:
    extracted = inquiry.extracted or {}
    attempts = db.execute(select(func.count()).select_from(AiRun)
                          .where(AiRun.inquiry_id == inquiry.id,
                                 AiRun.purpose == "inquiry_extraction")).scalar_one()
    product = db.get(Product, uuid.UUID(extracted["product_id"])) if extracted.get("product_id") else None
    open_ids = [e.id for e in exception_service.open_exceptions_for(db, "inquiry", inquiry.id)]
    outcome = "needs_review" if inquiry.status == "needs_review" else "extracted"
    return ExtractionResult(inquiry=inquiry, outcome=outcome, attempts=attempts, replay=True,
                            confidence=extracted.get("confidence"), product=product,
                            reasons=extracted.get("review_reasons", []), exception_ids=open_ids)


# --------------------------------------------------------------------------- main entry point

def extract_inquiry(db: Session, inquiry_id: uuid.UUID, llm: LLMClient) -> ExtractionResult:
    started = _start(db, inquiry_id)
    if isinstance(started, ExtractionResult):
        return started
    inquiry = started
    settings = get_settings()

    catalog = list(db.execute(select(Product.name).where(Product.is_active.is_(True))).scalars())
    messages = build_messages(inquiry.raw_message, catalog)
    max_attempts = 1 + settings.llm_max_retries

    valid: ExtractedInquiry | None = None
    valid_run: AiRun | None = None
    last_failure = None          # 'invalid' | 'unavailable'
    last_errors: list[str] = []
    attempt = 0

    for attempt in range(1, max_attempts + 1):
        run = AiRun(purpose="inquiry_extraction", inquiry_id=inquiry.id, attempt=attempt,
                    provider=llm.provider, model=getattr(llm, "model", None),
                    prompt_version=PROMPT_VERSION, input_text=inquiry.raw_message, outcome="error")
        try:
            response = llm.complete_json(messages)   # no DB transaction is held during this call
        except LLMUnavailable as exc:
            run.outcome = "timeout" if exc.kind == "timeout" else "error"
            run.error_message = str(exc)[:1000]
            db.add(run)
            db.commit()
            last_failure, last_errors = "unavailable", [str(exc)[:300]]
            log.warning("LLM unavailable (attempt %s/%s): %s", attempt, max_attempts, exc,
                        extra={"inquiry_id": inquiry.id})
            if not exc.retryable:
                break
            time.sleep(settings.llm_retry_backoff_seconds * attempt)
            continue

        run.model, run.raw_output, run.latency_ms = response.model, response.text[:20000], response.latency_ms
        result = parse_and_validate(response.text)
        run.parsed_output = audit._jsonable(result.parsed_json)
        if not result.ok:
            run.outcome, run.validation_errors = "invalid", result.errors
            db.add(run)
            db.commit()
            last_failure, last_errors = "invalid", result.errors
            log.warning("Invalid AI output (attempt %s/%s): %s", attempt, max_attempts, result.errors,
                        extra={"inquiry_id": inquiry.id})
            messages = messages + retry_feedback(response.text, result.errors)
            continue

        valid = result.data
        run.outcome = "valid"
        run.confidence = Decimal(str(round(valid.confidence, 3)))
        db.add(run)
        db.flush()
        valid_run = run
        break

    # ------------------------------------------------------------------ decide
    reasons: list[str] = []
    exceptions: list[tuple[str, dict]] = []
    product = None
    extracted: dict = {"source": "ai", "prompt_version": PROMPT_VERSION}

    if valid is None:
        if last_failure == "unavailable":
            reasons.append("AI service unavailable after retries")
            exceptions.append(("llm_unavailable", {"attempts": attempt, "last_error": last_errors}))
        else:
            reasons.append("AI output failed validation after retries")
            exceptions.append(("invalid_ai_output", {"attempts": attempt, "errors": last_errors}))
    else:
        product = resolve_product(db, valid.product_name)
        grounding = grounding_issues(valid, inquiry.raw_message)
        extracted.update({
            "ai_run_id": str(valid_run.id),
            "product_name_raw": valid.product_name,
            "product_id": str(product.id) if product else None,
            "sku": product.sku if product else None,
            "quantity": valid.quantity,
            "requested_discount_pct": str(valid.requested_discount_pct)
            if valid.requested_discount_pct is not None else "0",
            "discount_stated": valid.requested_discount_pct is not None,
            "delivery_timeframe": valid.delivery_timeframe,
            "appointment_requested": valid.appointment_requested,
            "appointment_preference": valid.appointment_preference,
            "intent": valid.intent,
            "confidence": valid.confidence,
        })
        missing = [f for f, v in (("product_name", valid.product_name), ("quantity", valid.quantity)) if not v]
        if missing:
            reasons.append(f"missing required fields: {', '.join(missing)}")
            exceptions.append(("missing_field", {"fields": missing}))
        if valid.product_name and product is None:
            reasons.append(f"product '{valid.product_name}' is not in the catalog")
            exceptions.append(("unknown_product", {"product_name": valid.product_name}))
        if grounding:
            reasons.extend(grounding)
            exceptions.append(("low_confidence", {"grounding": grounding, "model_confidence": valid.confidence}))
        elif valid.confidence < settings.llm_min_confidence:
            reasons.append(f"AI confidence {valid.confidence:.2f} below threshold {settings.llm_min_confidence}")
            exceptions.append(("low_confidence", {"model_confidence": valid.confidence,
                                                  "threshold": settings.llm_min_confidence}))

    outcome = "needs_review" if exceptions else "extracted"
    extracted["review_reasons"] = reasons

    inquiry = db.execute(select(Inquiry).where(Inquiry.id == inquiry.id).with_for_update()).scalar_one()
    inquiry.status = outcome
    inquiry.extracted = audit._jsonable(extracted)
    inquiry.accepted_ai_run_id = valid_run.id if (valid_run and outcome == "extracted") else None
    inquiry.updated_at = datetime.now(timezone.utc)

    exception_ids = [
        exception_service.open_exception(db, entity_type="inquiry", entity_id=inquiry.id,
                                         exception_type=etype, details=details, inquiry_id=inquiry.id,
                                         severity="medium" if etype == "low_confidence" else "high")
        for etype, details in exceptions
    ]
    audit.record(db, action=f"ai.extraction_{outcome}", entity_type="inquiry", entity_id=inquiry.id,
                 inquiry_id=inquiry.id, actor=f"ai:{getattr(llm, 'model', llm.provider)}",
                 before={"status": "extracting"}, after={"status": outcome, "extracted": extracted},
                 meta={"attempts": attempt, "reasons": reasons})
    db.commit()
    log.info("Extraction finished: %s", outcome, extra={"inquiry_id": inquiry.id})

    return ExtractionResult(inquiry=inquiry, outcome=outcome, attempts=attempt,
                            confidence=valid.confidence if valid else None, product=product,
                            reasons=reasons, exception_ids=exception_ids)
