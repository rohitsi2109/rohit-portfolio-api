from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.models import Booking, Contact, Lead, LeadEvent
from app.routers import public
from app.services import cal
from tests.conftest import booking_body


def test_health(client):
    assert client.get("/health").json() == {"status": "ok", "db": "ok"}


def test_slots(client, fake_cal):
    res = client.get("/v1/slots", params={"start": "2026-11-10", "end": "2026-11-30", "timeZone": "Australia/Sydney"})
    assert res.status_code == 200
    assert res.json() == {"slots": {"2026-11-10": ["2026-11-10T10:00:00.000+11:00"]}}
    assert "s-maxage=60" in res.headers["cache-control"]


def test_slots_validation(client, fake_cal):
    res = client.get("/v1/slots", params={"start": "tomorrow", "end": "2026-11-30", "timeZone": "Mars/Base"})
    assert res.status_code == 400
    assert res.json() == {"error": "Invalid start, end or timeZone"}


def test_booking_saves_lead_with_attribution(client, db, fake_cal):
    res = client.post("/v1/bookings", json=booking_body(), headers={"x-vercel-ip-country": "au"})
    assert res.status_code == 200, res.text
    assert res.json()["uid"] == "cal-uid-1"

    contact = db.scalar(select(Contact))
    assert (contact.email, contact.phone_e164, contact.phone_country) == ("jane@example.com", "+61412345678", "AU")
    lead = db.scalar(select(Lead))
    assert lead.type == "booking" and lead.status == "new" and lead.source_channel == "website"
    assert (lead.utm_source, lead.utm_medium, lead.landing_page) == ("linkedin", "social", "/?utm_source=linkedin")
    assert lead.visitor_country == "AU"
    assert lead.consent_version == "privacy-2026-11" and lead.consent_at is not None
    assert lead.message == "A booking site for my salon"

    booking = db.scalar(select(Booking))
    assert booking.status == "confirmed" and booking.cal_booking_uid == "cal-uid-1"
    assert booking.meeting_url.startswith("https://meet.google.com")
    events = db.scalars(select(LeadEvent.type).order_by(LeadEvent.type)).all()
    assert events == ["booking_confirmed", "lead_created"]

    meta = fake_cal.bookings[0]["metadata"]
    assert meta["leadId"] == str(lead.id) and meta["source"] == "portfolio" and meta["utmSource"] == "linkedin"


def test_returning_visitor_is_one_contact(client, db, fake_cal):
    client.post("/v1/bookings", json=booking_body())
    client.post("/v1/bookings", json=booking_body(email="JANE@example.com", name="Jane S."))
    contacts = db.scalars(select(Contact)).all()
    assert len(contacts) == 1 and contacts[0].name == "Jane S."
    assert len(db.scalars(select(Lead)).all()) == 2


def test_booking_validation_messages(client, fake_cal):
    res = client.post("/v1/bookings", json=booking_body(email="not-an-email"))
    assert res.status_code == 400
    assert res.json() == {"error": "Please enter a valid email address."}
    res = client.post("/v1/bookings", json=booking_body(start="2020-01-01T10:00:00+00:00"))
    assert res.json() == {"error": "Please pick an available time."}
    assert fake_cal.bookings == []


def test_honeypot_saves_nothing(client, db, fake_cal):
    res = client.post("/v1/bookings", json=booking_body(honeypot="https://spam.example"))
    assert res.status_code == 200
    assert db.scalar(select(Lead)) is None
    assert fake_cal.bookings == []


def test_cal_failure_keeps_the_lead(client, db, fake_cal):
    fake_cal.error = cal.CalError("This slot is no longer available", 400)
    res = client.post("/v1/bookings", json=booking_body())
    assert res.status_code == 400
    assert res.json() == {"error": "This slot is no longer available"}
    booking = db.scalar(select(Booking))
    assert booking.status == "failed" and booking.error_message == "This slot is no longer available"
    assert db.scalar(select(Lead)) is not None


def test_database_down_still_books(client, db, fake_cal, monkeypatch):
    def broken(*args, **kwargs):
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))
    monkeypatch.setattr(public, "upsert_contact", broken)

    res = client.post("/v1/bookings", json=booking_body())
    assert res.status_code == 200
    assert res.json()["uid"] == "cal-uid-1"
    assert db.scalar(select(Lead)) is None  # backfilled later by the BOOKING_CREATED webhook


def test_enquiry(client, db):
    res = client.post("/v1/enquiries", json={
        "name": "Sam", "email": "sam@example.com", "company": "Sam's Gym", "message": "Need an app",
        "budgetRange": "AUD 5k-10k", "attribution": {"utmSource": "google"}})
    assert res.status_code == 200, res.text
    lead = db.scalar(select(Lead))
    assert lead.type == "enquiry" and lead.budget_range == "AUD 5k-10k" and lead.utm_source == "google"
    assert db.scalar(select(Contact)).company == "Sam's Gym"
