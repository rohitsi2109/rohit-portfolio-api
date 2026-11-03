"""Cal.com webhooks: verify the signature, store the raw event (de-duplicated), then process it."""

import hashlib
import hmac
import json
import logging
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.db import get_db
from app.models import Booking, Lead, WebhookEvent
from app.routers.public import PHONE, country_code, parse_instant
from app.schemas import Attribution
from app.services import push
from app.services.leads import (
    ATTRIBUTION_COLUMNS,
    WebsiteLead,
    attach_confirmed_booking,
    create_website_lead,
    log_event,
    now,
    upsert_contact,
)

log = logging.getLogger(__name__)
router = APIRouter()

ACTOR = "cal_webhook"


def verify_signature(raw: bytes, signature: str | None) -> bool:
    secret = get_settings().cal_webhook_secret
    if not secret:
        log.error("CAL_WEBHOOK_SECRET is not set; rejecting webhook")
        return False
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, (signature or "").strip())


@router.post("/v1/webhooks/cal")
async def cal_webhook(request: Request, db: Session = Depends(get_db)):
    # The signature is over the exact raw bytes, so read them here, then do the
    # (blocking) database work off the event loop
    raw = await request.body()
    return await run_in_threadpool(handle_webhook, raw, request.headers.get("x-cal-signature-256"), db)


def handle_webhook(raw: bytes, signature: str | None, db: Session) -> dict[str, Any]:
    if not verify_signature(raw, signature):
        raise HTTPException(401, "Invalid signature")
    try:
        body = json.loads(raw)
    except ValueError:
        raise HTTPException(400, "Invalid JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "Invalid payload")

    event_type = str(body.get("triggerEvent") or "UNKNOWN")
    payload = body.get("payload") if isinstance(body.get("payload"), dict) else {}
    uid = payload.get("uid") or payload.get("bookingUid") or ""
    dedupe_key = f"{event_type}:{uid}:{body.get('createdAt') or hashlib.sha256(raw).hexdigest()}"

    # Store first, so every delivery is on record even if processing fails
    stmt = (
        insert(WebhookEvent)
        .values(id=uuid.uuid4(), provider="cal", event_type=event_type, dedupe_key=dedupe_key, payload=body)
        .on_conflict_do_nothing(index_elements=[WebhookEvent.provider, WebhookEvent.dedupe_key])
        .returning(WebhookEvent.id)
    )
    event_id = db.execute(stmt).scalar_one_or_none()
    if event_id is None:
        existing = db.scalar(select(WebhookEvent).where(
            WebhookEvent.provider == "cal", WebhookEvent.dedupe_key == dedupe_key))
        if existing is None or existing.processed_at is not None:
            db.commit()
            return {"ok": True, "duplicate": True}
        event_id = existing.id  # stored earlier but never processed: try again
    db.commit()

    notices: list[push.Notice] = []
    try:
        result = process(db, event_type, payload, parse_instant(str(body.get("createdAt") or "")) or now(), notices)
        event = db.get(WebhookEvent, event_id)
        event.processed_at = now()
        event.error = None
        db.commit()
    except Exception as e:
        db.rollback()
        log.exception("webhook %s (%s) failed", event_id, event_type)
        event = db.get(WebhookEvent, event_id)
        event.error = f"{type(e).__name__}: {e}"[:2000]
        db.commit()
        raise HTTPException(500, "Webhook processing failed")
    # Only after the commit, so a rolled-back change never sends a notification
    for notice in notices:
        push.send(notice)
    return {"ok": True, "result": result}


def process(db: Session, event_type: str, p: dict[str, Any], created_at: datetime,
            notices: list[push.Notice]) -> str:
    """Apply one event. Handlers queue notifications in `notices`; the caller sends them after committing."""
    handler = {
        "BOOKING_CREATED": booking_created,
        "BOOKING_CANCELLED": booking_cancelled,
        "BOOKING_RESCHEDULED": booking_rescheduled,
        "MEETING_ENDED": meeting_ended,
        "BOOKING_NO_SHOW_UPDATED": no_show_updated,
    }.get(event_type)
    if handler is None:
        return f"ignored: {event_type}"
    return handler(db, p, created_at, notices)


# --- payload helpers ---------------------------------------------------------

def booking_by_uid(db: Session, uid: str | None) -> Booking | None:
    if not uid:
        return None
    return db.scalar(select(Booking).where(Booking.cal_booking_uid == uid))


def response_value(p: dict[str, Any], key: str) -> str | None:
    """Booking form answers arrive as {"notes": "..."} or {"notes": {"label": ..., "value": "..."}}."""
    value = (p.get("responses") or {}).get(key)
    if isinstance(value, dict):
        value = value.get("value")
    return value if isinstance(value, str) and value.strip() else None


def meeting_url(p: dict[str, Any]) -> str | None:
    candidates = [(p.get("videoCallData") or {}).get("url"), (p.get("metadata") or {}).get("videoCallUrl"),
                  p.get("location")]
    return next((c for c in candidates if isinstance(c, str) and c.startswith("http")), None)


def first_attendee(p: dict[str, Any]) -> dict[str, Any]:
    attendees = p.get("attendees") or []
    return attendees[0] if attendees and isinstance(attendees[0], dict) else {}


def parse_uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


# --- handlers ----------------------------------------------------------------

def booking_created(db: Session, p: dict[str, Any], created_at: datetime, notices: list[push.Notice]) -> str:
    uid = p.get("uid")
    start = parse_instant(str(p.get("startTime") or ""))
    if not uid or start is None:
        return "ignored: no uid or start time"
    if booking_by_uid(db, uid):
        return "already recorded"

    attendee = first_attendee(p)
    timezone = attendee.get("timeZone") or "UTC"
    meta = p.get("metadata") or {}
    lead_id = parse_uuid(meta.get("leadId"))
    booking_args = dict(uid=uid, start_at=start, end_at=parse_instant(str(p.get("endTime") or "")),
                        timezone=timezone, meeting_url=meeting_url(p), actor=ACTOR)

    if lead_id and db.get(Lead, lead_id):
        attach_confirmed_booking(db, lead_id=lead_id, **booking_args)
        return "attached to lead"

    if meta.get("source") != "portfolio":
        # Booked directly on cal.com: no consent record from the website, so no lead
        return "ignored: not booked through the website"

    # The website booked this while the database was unreachable: rebuild the lead
    email = attendee.get("email")
    if not email:
        return "ignored: no attendee email"
    phone = attendee.get("phoneNumber") or response_value(p, "attendeePhoneNumber")
    phone = phone if phone and PHONE.match(phone) else None
    name = (attendee.get("name") or email)[:100]
    contact_id = upsert_contact(db, email=email, name=name, phone=phone,
                                phone_country=country_code(meta.get("phoneCountry")), timezone=timezone)
    w = WebsiteLead(
        type="booking",
        name=name,
        phone=phone,
        company=None,
        message=(response_value(p, "notes") or "")[:2000],
        attribution=Attribution(**{k: meta.get(k) for k in ATTRIBUTION_COLUMNS}),
        consent_version=meta.get("consentVersion") or get_settings().consent_version,
        consent_at=parse_instant(str(meta.get("consentAt") or "")) or created_at,
        visitor_country=country_code(meta.get("visitorCountry")),
    )
    lead_id = lead_id or uuid.uuid4()
    create_website_lead(db, lead_id, contact_id, w)
    attach_confirmed_booking(db, lead_id=lead_id, **booking_args)
    notices.append(push.new_booking_notice(lead_id, name, country_code(meta.get("phoneCountry")), start, w.message))
    return "lead backfilled"


def booking_cancelled(db: Session, p: dict[str, Any], _: datetime, notices: list[push.Notice]) -> str:
    booking = booking_by_uid(db, p.get("uid"))
    if booking is None:
        return "unknown booking"
    if booking.status == "cancelled":
        return "already cancelled"
    booking.status = "cancelled"
    booking.cancellation_reason = p.get("cancellationReason") or None
    log_event(db, booking.lead_id, "booking_cancelled", ACTOR,
              {"cal_booking_uid": booking.cal_booking_uid, "reason": booking.cancellation_reason})
    reason = f" · {booking.cancellation_reason}" if booking.cancellation_reason else ""
    notices.append(push.booking_notice(booking.lead_id, "booking_cancelled", f"Call cancelled: {booking.lead.contact.name}",
                                  f"Was {push.local_time(booking.start_at)} IST{reason}"))
    return "cancelled"


def booking_rescheduled(db: Session, p: dict[str, Any], _: datetime, notices: list[push.Notice]) -> str:
    new_uid = p.get("uid")
    start = parse_instant(str(p.get("startTime") or ""))
    old = booking_by_uid(db, p.get("rescheduleUid") or p.get("fromReschedule"))
    if old is None or not new_uid or start is None:
        return "unknown booking"
    old.status = "rescheduled"
    attach_confirmed_booking(
        db, lead_id=old.lead_id, uid=new_uid, start_at=start,
        end_at=parse_instant(str(p.get("endTime") or "")),
        timezone=first_attendee(p).get("timeZone") or old.attendee_timezone,
        meeting_url=meeting_url(p) or old.meeting_url, actor=ACTOR, rescheduled_from_id=old.id)
    notices.append(push.booking_notice(old.lead_id, "booking_rescheduled", f"Call moved: {old.lead.contact.name}",
                                  f"Now {push.local_time(start)} IST (was {push.local_time(old.start_at)})"))
    return "rescheduled"


def meeting_ended(db: Session, p: dict[str, Any], _: datetime, notices: list[push.Notice]) -> str:
    booking = booking_by_uid(db, p.get("uid"))
    if booking is None or booking.status != "confirmed":
        return "no confirmed booking"
    booking.status = "completed"
    log_event(db, booking.lead_id, "booking_completed", ACTOR, {"cal_booking_uid": booking.cal_booking_uid})
    return "completed"


def no_show_updated(db: Session, p: dict[str, Any], _: datetime, notices: list[push.Notice]) -> str:
    booking = booking_by_uid(db, p.get("bookingUid") or p.get("uid"))
    if booking is None:
        return "unknown booking"
    if not any(isinstance(a, dict) and a.get("noShow") for a in p.get("attendees") or []):
        return "attendee attended"
    booking.status = "no_show"
    log_event(db, booking.lead_id, "booking_no_show", ACTOR, {"cal_booking_uid": booking.cal_booking_uid})
    return "no show"
