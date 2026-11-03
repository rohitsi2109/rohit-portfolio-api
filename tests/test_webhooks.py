import uuid

from sqlalchemy import select

from app.models import Booking, Lead, LeadEvent, WebhookEvent
from tests.conftest import booking_body, future, sign


def cal_event(trigger: str, **payload):
    return {"triggerEvent": trigger, "createdAt": "2026-11-03T10:00:00.000Z", "payload": payload}


def post(client, body):
    raw, headers = sign(body)
    return client.post("/v1/webhooks/cal", content=raw, headers=headers)


def test_rejects_bad_signature(client):
    res = client.post("/v1/webhooks/cal", json=cal_event("BOOKING_CREATED", uid="x"),
                      headers={"x-cal-signature-256": "deadbeef"})
    assert res.status_code == 401


def test_created_after_website_booking_is_a_no_op(client, db, fake_cal):
    client.post("/v1/bookings", json=booking_body())
    lead = db.scalar(select(Lead))
    body = cal_event("BOOKING_CREATED", uid="cal-uid-1", startTime=future(),
                     metadata={"source": "portfolio", "leadId": str(lead.id)})

    assert post(client, body).json()["result"] == "already recorded"
    assert post(client, body).json() == {"ok": True, "duplicate": True}
    assert len(db.scalars(select(Booking)).all()) == 1
    assert len(db.scalars(select(WebhookEvent)).all()) == 1


def test_created_backfills_lead_when_database_was_down(client, db):
    lead_id = uuid.uuid4()
    body = cal_event(
        "BOOKING_CREATED", uid="cal-uid-9", startTime=future(), endTime=future(),
        attendees=[{"name": "Jane", "email": "jane@example.com", "timeZone": "Australia/Sydney",
                    "phoneNumber": "+61412345678"}],
        responses={"notes": {"label": "Notes", "value": "Salon booking site"}},
        metadata={"source": "portfolio", "leadId": str(lead_id), "consentVersion": "privacy-2026-11",
                  "consentAt": "2026-11-03T09:59:00+00:00", "utmSource": "linkedin", "visitorCountry": "AU"},
        videoCallData={"url": "https://meet.google.com/xyz"})
    res = post(client, body)
    assert res.json()["result"] == "lead backfilled", res.text

    lead = db.get(Lead, lead_id)
    assert lead.utm_source == "linkedin" and lead.visitor_country == "AU" and lead.message == "Salon booking site"
    assert lead.consent_version == "privacy-2026-11"
    booking = db.scalar(select(Booking))
    assert booking.lead_id == lead_id and booking.meeting_url == "https://meet.google.com/xyz"


def test_created_outside_website_is_ignored(client, db):
    body = cal_event("BOOKING_CREATED", uid="cal-direct", startTime=future(),
                     attendees=[{"name": "X", "email": "x@example.com", "timeZone": "UTC"}])
    assert post(client, body).json()["result"] == "ignored: not booked through the website"
    assert db.scalar(select(Lead)) is None
    assert db.scalar(select(WebhookEvent)).processed_at is not None


def test_cancelled(client, db, fake_cal):
    client.post("/v1/bookings", json=booking_body())
    res = post(client, cal_event("BOOKING_CANCELLED", uid="cal-uid-1", cancellationReason="Found someone"))
    assert res.json()["result"] == "cancelled"
    booking = db.scalar(select(Booking))
    assert booking.status == "cancelled" and booking.cancellation_reason == "Found someone"
    assert db.scalar(select(LeadEvent).where(LeadEvent.type == "booking_cancelled")).actor == "cal_webhook"


def test_rescheduled_links_bookings(client, db, fake_cal):
    client.post("/v1/bookings", json=booking_body())
    new_start = future(5)
    res = post(client, cal_event("BOOKING_RESCHEDULED", uid="cal-uid-new", rescheduleUid="cal-uid-1",
                                 startTime=new_start))
    assert res.json()["result"] == "rescheduled"
    old = db.scalar(select(Booking).where(Booking.cal_booking_uid == "cal-uid-1"))
    new = db.scalar(select(Booking).where(Booking.cal_booking_uid == "cal-uid-new"))
    assert old.status == "rescheduled"
    assert new.status == "confirmed" and new.rescheduled_from_id == old.id and new.lead_id == old.lead_id
