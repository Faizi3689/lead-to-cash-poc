"""Mock scheduling service: turns "I'd like to talk tomorrow" into a concrete, conflict-free slot.

Business hours 10:00-16:00 (business timezone), 30-minute slots, weekdays only. If a slot is taken
the next free hour is used. MOCK_CALENDAR_FAIL=true simulates the calendar being unavailable.
"""
import logging
import secrets
import uuid
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app import audit
from app.config import get_settings
from app.models import Appointment, Inquiry
from app.services import exception_service

log = logging.getLogger("app.appointments")

FIRST_SLOT, LAST_SLOT = 10, 16
SLOT_MINUTES = 30
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


class NotFound(Exception):
    pass


class NotRequested(Exception):
    pass


class CalendarUnavailable(Exception):
    pass


def _next_business_day(d: date) -> date:
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def target_day(preference: str | None, now_local: datetime) -> date:
    """Deterministic interpretation of the customer's words."""
    p = (preference or "").lower()
    today = now_local.date()
    if "today" in p and now_local.hour < LAST_SLOT:
        return _next_business_day(today)
    if "tomorrow" in p:
        return _next_business_day(today + timedelta(days=1))
    if "next week" in p:
        return today + timedelta(days=7 - today.weekday())          # next Monday
    for i, name in enumerate(WEEKDAYS[:5]):
        if name in p:
            ahead = (i - today.weekday()) % 7 or 7
            return today + timedelta(days=ahead)
    return _next_business_day(today + timedelta(days=1))


def _is_free(db: Session, start: datetime, end: datetime) -> bool:
    clash = db.execute(select(Appointment.id).where(
        Appointment.status != "cancelled", Appointment.scheduled_start < end,
        Appointment.scheduled_end > start)).first()
    return clash is None


def schedule(db: Session, inquiry_id: uuid.UUID, now: datetime | None = None) -> tuple[Appointment, bool]:
    settings = get_settings()
    inquiry = db.execute(select(Inquiry).where(Inquiry.id == inquiry_id).with_for_update()).scalar_one_or_none()
    if inquiry is None:
        raise NotFound("inquiry not found")

    existing = db.execute(select(Appointment).where(Appointment.inquiry_id == inquiry.id)).scalar_one_or_none()
    if existing:
        db.rollback()
        return existing, True

    extracted = inquiry.extracted or {}
    if not extracted.get("appointment_requested"):
        db.rollback()
        raise NotRequested("the customer did not ask for a call or meeting")

    if settings.mock_calendar_fail:
        exception_service.open_exception(
            db, entity_type="inquiry", entity_id=inquiry.id, exception_type="downstream_failure",
            details={"service": "calendar", "error": "calendar service unavailable (simulated)"},
            severity="medium", inquiry_id=inquiry.id)
        audit.record(db, action="appointment.failed", entity_type="inquiry", entity_id=inquiry.id,
                     inquiry_id=inquiry.id, meta={"reason": "calendar unavailable"})
        db.commit()
        raise CalendarUnavailable("calendar service unavailable - safe to retry later")

    tz = ZoneInfo(settings.business_timezone)
    now_local = (now or datetime.now(timezone.utc)).astimezone(tz)
    day = target_day(extracted.get("appointment_preference"), now_local)

    # Serialise slot allocation so two requests can never grab the same slot.
    db.execute(text("select pg_advisory_xact_lock(hashtext('mock-calendar'))"))
    start = end = None
    for _ in range(10):                       # search up to 10 business days ahead
        for hour in range(FIRST_SLOT, LAST_SLOT):
            candidate = datetime.combine(day, time(hour, 0), tzinfo=tz)
            if candidate <= now_local:
                continue
            candidate_end = candidate + timedelta(minutes=SLOT_MINUTES)
            if _is_free(db, candidate, candidate_end):
                start, end = candidate, candidate_end
                break
        if start:
            break
        day = _next_business_day(day + timedelta(days=1))
    if start is None:
        db.rollback()
        raise CalendarUnavailable("no free slot in the next 10 business days")

    appointment = Appointment(
        inquiry_id=inquiry.id, customer_id=inquiry.customer_id,
        requested_text=extracted.get("appointment_preference"),
        scheduled_start=start, scheduled_end=end, timezone=settings.business_timezone,
        status="confirmed", external_ref=f"MOCK-CAL-{secrets.token_hex(4).upper()}",
    )
    db.add(appointment)
    db.flush()
    # A successful booking clears an earlier calendar outage for this inquiry.
    for exc in exception_service.open_exceptions_for(db, "inquiry", inquiry.id):
        if exc.exception_type == "downstream_failure" and (exc.details or {}).get("service") == "calendar":
            exception_service.resolve(db, exc, resolved_by="system", inquiry_id=inquiry.id,
                                      resolution={"reason": "appointment booked on retry"})
    audit.record(db, action="appointment.confirmed", entity_type="appointment", entity_id=appointment.id,
                 inquiry_id=inquiry.id,
                 after={"start": start, "end": end, "timezone": settings.business_timezone,
                        "requested": extracted.get("appointment_preference"),
                        "external_ref": appointment.external_ref})
    db.commit()
    log.info("Appointment booked for %s", start.isoformat(), extra={"inquiry_id": inquiry.id})
    return appointment, False
