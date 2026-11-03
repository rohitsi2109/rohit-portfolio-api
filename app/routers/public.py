"""Public endpoints called by the website. Errors are returned as {"error": "<friendly message>"}."""

import logging
import re
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import Booking
from app.schemas import BookingRequest, BookingResult, EnquiryRequest
from app.services import cal, push
from app.services.ratelimit import limit_by_ip
from app.services.leads import (
    WebsiteLead,
    attach_confirmed_booking,
    booking_metadata,
    create_website_lead,
    log_event,
    now,
    upsert_contact,
)

log = logging.getLogger(__name__)
router = APIRouter()

EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
# Cal.com expects E.164, e.g. +61412345678
PHONE = re.compile(r"^\+[1-9]\d{6,14}$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
COUNTRY = re.compile(r"^[A-Z]{2}$")


def bad_request(message: str) -> HTTPException:
    return HTTPException(400, message)


def is_valid_timezone(tz: str) -> bool:
    try:
        ZoneInfo(tz)
        return bool(tz)
    except (ZoneInfoNotFoundError, ValueError):
        return False


def parse_instant(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else None


def country_code(value: str | None) -> str | None:
    value = (value or "").strip().upper()
    return value if COUNTRY.match(value) else None


def clean_phone(value: str) -> str:
    return re.sub(r"[\s()-]", "", value or "")


def consent_version(value: str | None) -> str:
    value = (value or "").strip()[:50]
    return value or get_settings().consent_version


@router.get("/health")
def health(db: Session = Depends(get_db)):
    # Also wakes Neon's compute if it was sleeping
    try:
        db.execute(text("SELECT 1"))
        return {"status": "ok", "db": "ok"}
    except SQLAlchemyError:
        log.exception("health check: database unreachable")
        return JSONResponse({"status": "degraded", "db": "error"}, status_code=503)


@router.get("/v1/slots")
def slots(start: str = "", end: str = "", timeZone: str = "UTC"):
    if not DATE.match(start) or not DATE.match(end) or not is_valid_timezone(timeZone):
        raise bad_request("Invalid start, end or timeZone")
    try:
        data = cal.get_slots(start, end, timeZone)
    except cal.CalError as e:
        raise HTTPException(e.status_code, e.message)
    return JSONResponse({"slots": data}, headers={"Cache-Control": "s-maxage=60, stale-while-revalidate=300"})


@router.post("/v1/bookings", response_model=BookingResult,
             # 10, not 5: failed attempts count too (e.g. a slot taken a moment earlier),
             # and a real visitor may try several slots
             dependencies=[Depends(limit_by_ip("booking", limit=10, window_seconds=3600))])
def create_booking(body: BookingRequest, request: Request, db: Session = Depends(get_db)):
    name = body.name.strip()
    email = body.email.strip()
    phone = clean_phone(body.phone)
    notes = body.notes.strip()[:2000]
    tz = body.timeZone
    start = parse_instant(body.start)

    if not name or len(name) > 100:
        raise bad_request("Please enter your name.")
    if not EMAIL.match(email) or len(email) > 254:
        raise bad_request("Please enter a valid email address.")
    if not PHONE.match(phone):
        raise bad_request("Please enter your phone number with country code, e.g. +61 412 345 678.")
    if not is_valid_timezone(tz):
        raise bad_request("Invalid time zone.")
    if start is None or start <= now():
        raise bad_request("Please pick an available time.")

    # Bots fill the hidden field: pretend it worked, store nothing
    if body.honeypot:
        return BookingResult(uid=None, start=start.isoformat(), end=None)

    phone_country = country_code(body.phoneCountry)
    w = WebsiteLead(
        type="booking",
        name=name,
        phone=phone,
        company=None,
        message=notes,
        attribution=body.attribution,
        consent_version=consent_version(body.consentVersion),
        consent_at=now(),
        visitor_country=country_code(request.headers.get("x-vercel-ip-country")),
    )
    lead_id = uuid.uuid4()

    # The lead is committed before calling Cal.com, so a BOOKING_CREATED webhook that races
    # ahead of this request always finds it. If the database is down, still book: the
    # client's meeting matters most, and the webhook rebuilds the lead from the metadata.
    db_ok = True
    try:
        contact_id = upsert_contact(db, email=email, name=name, phone=phone,
                                    phone_country=phone_country, timezone=tz)
        create_website_lead(db, lead_id, contact_id, w)
        db.commit()
    except SQLAlchemyError:
        log.exception("booking: could not save lead %s before booking", lead_id)
        db.rollback()
        db_ok = False

    try:
        booking = cal.create_booking(start=start.isoformat(), name=name, email=email, phone=phone,
                                     time_zone=tz, notes=notes,
                                     metadata=booking_metadata(lead_id, w, phone_country))
    except cal.CalError as e:
        if db_ok:
            try:
                db.add(Booking(lead_id=lead_id, status="failed", start_at=start, attendee_timezone=tz,
                               error_message=e.message))
                log_event(db, lead_id, "booking_failed", "system", {"error": e.message})
                db.commit()
            except SQLAlchemyError:
                log.exception("booking: could not record failed booking for lead %s", lead_id)
                db.rollback()
        raise HTTPException(e.status_code, e.message)

    if db_ok:
        try:
            attach_confirmed_booking(
                db, lead_id=lead_id, uid=booking.uid,
                start_at=parse_instant(booking.start or "") or start,
                end_at=parse_instant(booking.end or ""),
                timezone=tz, meeting_url=booking.meeting_url, actor="system")
            db.commit()
        except SQLAlchemyError:
            log.exception("booking: could not save booking %s for lead %s", booking.uid, lead_id)
            db.rollback()
        push.send(push.new_booking_notice(lead_id, name, phone_country, start, notes))

    return BookingResult(uid=booking.uid, start=booking.start, end=booking.end, meetingUrl=booking.meeting_url)


@router.post("/v1/enquiries", dependencies=[Depends(limit_by_ip("enquiry", limit=5, window_seconds=3600))])
def create_enquiry(body: EnquiryRequest, request: Request, db: Session = Depends(get_db)):
    name = body.name.strip()
    email = body.email.strip()
    phone = clean_phone(body.phone) or None
    company = body.company.strip()[:200] or None
    message = body.message.strip()

    if not name or len(name) > 100:
        raise bad_request("Please enter your name.")
    if not EMAIL.match(email) or len(email) > 254:
        raise bad_request("Please enter a valid email address.")
    if phone and not PHONE.match(phone):
        raise bad_request("Please enter your phone number with country code, e.g. +61 412 345 678.")
    if not message or len(message) > 2000:
        raise bad_request("Please tell me a little about your project (up to 2000 characters).")

    if body.honeypot:
        return {"ok": True}

    w = WebsiteLead(
        type="enquiry",
        name=name,
        phone=phone,
        company=company,
        message=message,
        attribution=body.attribution,
        consent_version=consent_version(body.consentVersion),
        consent_at=now(),
        visitor_country=country_code(request.headers.get("x-vercel-ip-country")),
        budget_range=(body.budgetRange or "").strip()[:50] or None,
    )
    lead_id = uuid.uuid4()
    try:
        contact_id = upsert_contact(db, email=email, name=name, phone=phone,
                                    phone_country=country_code(body.phoneCountry), company=company)
        create_website_lead(db, lead_id, contact_id, w)
        db.commit()
    except SQLAlchemyError:
        log.exception("enquiry: could not save lead")
        db.rollback()
        raise HTTPException(503, "Sorry, your message could not be sent. Please email connect@rohitsingh.in.")

    push.notify_new_lead(lead_id, f"New enquiry: {name}", message[:100])
    return {"ok": True}
