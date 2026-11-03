from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from app.models import Booking, Contact, Lead, LeadEvent, RateLimit
from app.routers import public
from app.services import cal
from app.services.leads import today
from tests.conftest import booking_body


def test_api_and_database_agree_on_today_in_ist(db):
    # Overdue is decided in Python (payments list) and in SQL (client_summary); both use IST
    db_today = db.scalar(text("SELECT (now() AT TIME ZONE 'Asia/Kolkata')::date"))
    assert today() == db_today == datetime.now(ZoneInfo("Asia/Kolkata")).date()


def test_unknown_route_uses_the_error_shape(client):
    res = client.get("/no-such-route")
    assert res.status_code == 404 and res.json() == {"error": "Not Found"}


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
    assert len(contacts) == 1
    assert len(db.scalars(select(Lead)).all()) == 2


def test_website_never_overwrites_a_contact(client, db, fake_cal):
    # Anyone can type a known client's email; their stored details must survive
    client.post("/v1/bookings", json=booking_body())
    client.post("/v1/bookings", json=booking_body(name="Impostor", phone="+61499999999"))
    contact = db.scalar(select(Contact))
    assert (contact.name, contact.phone_e164) == ("Jane Smith", "+61412345678")
    latest = db.scalar(select(Lead).where(Lead.submitted_name == "Impostor"))
    assert latest.submitted_phone == "+61499999999"  # what was typed is kept on the lead


def test_website_fills_in_empty_contact_fields(client, db):
    client.post("/v1/enquiries", json={"name": "Sam", "email": "sam@example.com", "message": "Hi"})
    client.post("/v1/enquiries", json={"name": "Sam", "email": "sam@example.com", "message": "Again",
                                       "phone": "+6591234567", "company": "Sam's Gym"})
    contact = db.scalar(select(Contact))
    assert (contact.phone_e164, contact.company) == ("+6591234567", "Sam's Gym")


def test_bookings_are_rate_limited(client, fake_cal):
    for i in range(10):
        assert client.post("/v1/bookings", json=booking_body(email=f"p{i}@example.com")).status_code == 200
    res = client.post("/v1/bookings", json=booking_body(email="p11@example.com"))
    assert res.status_code == 429 and res.headers["retry-after"] == "3600"
    assert "Too many attempts" in res.json()["error"]
    # A different visitor is unaffected
    res = client.post("/v1/bookings", json=booking_body(email="p12@example.com"),
                      headers={"x-forwarded-for": "203.0.113.9"})
    assert res.status_code == 200
    assert len(fake_cal.bookings) == 11


def test_rate_limit_stores_no_raw_ip(client, db, fake_cal):
    client.post("/v1/bookings", json=booking_body(), headers={"x-forwarded-for": "203.0.113.7"})
    keys = db.scalars(select(RateLimit.key)).all()
    assert keys and all("203.0.113.7" not in k and len(k) == 64 for k in keys)


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
