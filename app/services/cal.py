"""Cal.com API v2 client (slots + bookings)."""

from dataclasses import dataclass
from typing import Any

import httpx

from app.config import get_settings

CAL_API = "https://api.cal.com/v2"
TIMEOUT = httpx.Timeout(15.0)


class CalError(Exception):
    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass
class CalBooking:
    uid: str
    start: str | None
    end: str | None
    meeting_url: str | None


def _headers(version: str) -> dict[str, str]:
    headers = {"cal-api-version": version, "Content-Type": "application/json"}
    if api_key := get_settings().cal_api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _error_message(body: Any, fallback: str) -> str:
    # Cal.com errors come back as { error: { message } } or { message }
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
        if body.get("message"):
            return str(body["message"])
    return fallback


def _json(res: httpx.Response) -> Any:
    try:
        return res.json()
    except ValueError:
        return None


def get_slots(start: str, end: str, time_zone: str) -> dict[str, list[str]]:
    """Available slot start times, grouped by date: {"YYYY-MM-DD": ["2026-11-04T14:00:00.000+11:00", ...]}."""
    s = get_settings()
    params = {"username": s.cal_username, "eventTypeSlug": s.cal_event_slug,
              "start": start, "end": end, "timeZone": time_zone}
    try:
        res = httpx.get(f"{CAL_API}/slots", params=params, headers=_headers("2024-09-04"), timeout=TIMEOUT)
    except httpx.HTTPError as e:
        raise CalError("Could not load availability", 502) from e
    body = _json(res)
    if not res.is_success:
        raise CalError(_error_message(body, "Could not load availability"), 502)
    data = (body or {}).get("data") or {}
    return {day: [slot["start"] for slot in slots] for day, slots in data.items()}


def create_booking(*, start: str, name: str, email: str, phone: str, time_zone: str,
                   notes: str, metadata: dict[str, str]) -> CalBooking:
    s = get_settings()
    payload = {
        "start": start,
        "username": s.cal_username,
        "eventTypeSlug": s.cal_event_slug,
        "attendee": {"name": name, "email": email, "phoneNumber": phone, "timeZone": time_zone, "language": "en"},
        # "title" is a required booking field on the event type ("What is this meeting about?")
        "bookingFieldsResponses": {
            "title": f"Project scope call with {name}",
            "attendeePhoneNumber": phone,
            **({"notes": notes} if notes else {}),
        },
        # Echoed back in Cal.com webhooks, so a booking can be matched to (or backfill) its lead
        "metadata": metadata,
    }
    fallback = "That time could not be booked. Please pick another slot."
    try:
        res = httpx.post(f"{CAL_API}/bookings", json=payload, headers=_headers("2024-08-13"), timeout=TIMEOUT)
    except httpx.HTTPError as e:
        raise CalError(fallback, 502) from e
    body = _json(res)
    if not res.is_success:
        raise CalError(_error_message(body, fallback), 502 if res.status_code >= 500 else 400)

    data = (body or {}).get("data")
    booking = data[0] if isinstance(data, list) and data else data
    if not isinstance(booking, dict) or not booking.get("uid"):
        raise CalError(fallback, 502)
    return CalBooking(
        uid=booking["uid"],
        start=booking.get("start"),
        end=booking.get("end"),
        meeting_url=booking.get("meetingUrl") or booking.get("location"),
    )
