import json
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select, update

from app.config import get_settings
from app.models import Client, Contact, Lead, Payment, Project, PushDevice
from app.services import push
from tests.conftest import booking_body, sign

FCM_SEND = "https://fcm.googleapis.com/v1/projects/test-proj/messages:send"


class FakeGoogle:
    """Google's token endpoint and FCM, faked at the HTTP layer."""

    def __init__(self, public_key):
        self.public_key = public_key
        self.sent: list[dict] = []
        self.responses: dict[str, httpx.Response] = {}  # device token -> canned FCM reply

    def post(self, url, json=None, data=None, headers=None, timeout=None):
        request = httpx.Request("POST", url)
        if url == "https://oauth2.googleapis.com/token":
            claims = jwt.decode(data["assertion"], self.public_key, algorithms=["RS256"],
                                audience="https://oauth2.googleapis.com/token")
            assert claims["scope"] == push.SCOPE
            return httpx.Response(200, json={"access_token": "ya29.test", "expires_in": 3600}, request=request)
        assert url == FCM_SEND and headers["Authorization"] == "Bearer ya29.test"
        self.sent.append(json["message"])
        return self.responses.get(json["message"]["token"], httpx.Response(200, json={}, request=request))


@pytest.fixture
def fcm(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    account = {"type": "service_account", "project_id": "test-proj", "private_key": pem,
               "client_email": "push@test-proj.iam.gserviceaccount.com",
               "token_uri": "https://oauth2.googleapis.com/token"}
    monkeypatch.setattr(get_settings(), "firebase_service_account", json.dumps(account))
    monkeypatch.setattr(push, "_token_cache", {})
    fake = FakeGoogle(key.public_key())
    monkeypatch.setattr(push.httpx, "post", fake.post)
    return fake


@pytest.fixture
def phone(db, admin_user):
    db.add(PushDevice(admin_user_id=admin_user.id, fcm_token="phone-token"))
    db.commit()
    return "phone-token"


def test_new_booking_pushes_to_the_phone(client, db, fake_cal, fcm, phone):
    res = client.post("/v1/bookings", json=booking_body())
    assert res.status_code == 200
    [msg] = fcm.sent
    lead = db.scalar(select(Lead))
    assert msg["token"] == "phone-token"
    assert msg["notification"]["title"] == "Jane Smith (AU) booked a call"
    assert " IST · A booking site for my salon" in msg["notification"]["body"]
    assert msg["data"] == {"route": f"/leads/{lead.id}", "channel": "new_leads", "type": "new_lead",
                           "lead_id": str(lead.id)}
    assert msg["android"] == {"priority": "high", "notification": {"channel_id": "new_leads", "color": "#00D4FF"}}


def test_enquiry_pushes(client, fcm, phone):
    client.post("/v1/enquiries", json={"name": "Sam", "email": "sam@example.com", "message": "Need an app"})
    assert [m["notification"]["title"] for m in fcm.sent] == ["New enquiry: Sam"]


def test_no_service_account_means_no_push(client, fake_cal, monkeypatch, phone):
    def explode(*args, **kwargs):
        raise AssertionError("no HTTP call expected")
    monkeypatch.setattr(push.httpx, "post", explode)
    assert client.post("/v1/bookings", json=booking_body()).status_code == 200


def test_dead_tokens_are_removed(client, db, admin_user, fake_cal, fcm):
    db.add_all([PushDevice(admin_user_id=admin_user.id, fcm_token="old-phone"),
                PushDevice(admin_user_id=admin_user.id, fcm_token="new-phone")])
    db.commit()
    fcm.responses["old-phone"] = httpx.Response(
        404, json={"error": {"details": [{"errorCode": "UNREGISTERED"}]}}, request=httpx.Request("POST", FCM_SEND))
    client.post("/v1/bookings", json=booking_body())
    assert db.scalars(select(PushDevice.fcm_token)).all() == ["new-phone"]


def test_fcm_outage_never_fails_a_booking(client, fake_cal, fcm, phone):
    fcm.responses["phone-token"] = httpx.Response(503, text="unavailable", request=httpx.Request("POST", FCM_SEND))
    assert client.post("/v1/bookings", json=booking_body()).status_code == 200


def test_cancellation_and_reschedule_push_on_the_bookings_channel(client, fake_cal, fcm, phone):
    client.post("/v1/bookings", json=booking_body())
    fcm.sent.clear()
    raw, headers = sign({"triggerEvent": "BOOKING_CANCELLED", "createdAt": "2026-11-04T10:00:00Z",
                         "payload": {"uid": "cal-uid-1", "cancellationReason": "Found someone"}})
    client.post("/v1/webhooks/cal", content=raw, headers=headers)
    [msg] = fcm.sent
    assert msg["notification"]["title"] == "Call cancelled: Jane Smith"
    assert msg["notification"]["body"].endswith("IST · Found someone")
    assert msg["android"]["notification"]["channel_id"] == "bookings"
    assert msg["data"]["type"] == "booking_cancelled"  # the name the app routes on


def test_failed_webhook_sends_nothing(client, fake_cal, fcm, phone, monkeypatch):
    client.post("/v1/bookings", json=booking_body())
    fcm.sent.clear()
    from app.routers import webhooks
    def broken(*args, **kwargs):
        raise RuntimeError("boom")
    monkeypatch.setattr(webhooks, "log_event", broken)
    raw, headers = sign({"triggerEvent": "BOOKING_CANCELLED", "createdAt": "2026-11-04T11:00:00Z",
                         "payload": {"uid": "cal-uid-1"}})
    assert client.post("/v1/webhooks/cal", content=raw, headers=headers).status_code == 500
    assert fcm.sent == []


# --- daily brief ---------------------------------------------------------------

CRON = {"Authorization": "Bearer cron-test-secret"}


@pytest.fixture
def cron_secret(monkeypatch):
    monkeypatch.setattr(get_settings(), "cron_secret", "cron-test-secret")


def test_daily_brief_needs_the_cron_secret(client, cron_secret):
    assert client.get("/v1/cron/daily-brief").status_code == 401
    assert client.get("/v1/cron/daily-brief", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_daily_brief_refuses_everything_without_a_configured_secret(client):
    assert client.get("/v1/cron/daily-brief", headers={"Authorization": "Bearer "}).status_code == 401


def test_daily_brief_stays_quiet_with_nothing_to_do(client, cron_secret, fcm, phone):
    res = client.get("/v1/cron/daily-brief", headers=CRON).json()
    assert res["sent"] is False and res["reason"] == "nothing to report"
    assert fcm.sent == []


def test_daily_brief_summarises_tomorrow(client, db, cron_secret, fake_cal, fcm, phone):
    ist = ZoneInfo("Asia/Kolkata")
    tomorrow = datetime.now(ist).date() + timedelta(days=1)
    # A call tomorrow at noon IST
    client.post("/v1/bookings", json=booking_body(start=datetime.combine(tomorrow, time(12), tzinfo=ist).isoformat()))
    fcm.sent.clear()
    # ...the same lead is a day old and nobody has replied, with a follow-up due tomorrow
    db.execute(update(Lead).values(created_at=datetime.now(ist) - timedelta(hours=30),
                                   next_follow_up_at=datetime.combine(tomorrow, time(10), tzinfo=ist)))
    # An unpaid deposit that was due yesterday
    contact = Contact(name="Priya", email="priya@example.com")
    db.add(contact)
    db.flush()
    client_row = Client(contact_id=contact.id)
    db.add(client_row)
    db.flush()
    project = Project(client_id=client_row.id, title="Site")
    db.add(project)
    db.flush()
    db.add(Payment(project_id=project.id, description="Deposit", amount=1500, currency="AUD",
                   due_on=tomorrow - timedelta(days=2)))
    db.commit()

    res = client.get("/v1/cron/daily-brief", headers=CRON).json()
    assert res["sent"] is True and res["devices"] == 1
    [msg] = fcm.sent
    assert msg["notification"]["title"] == "Tomorrow: 1 call, 1 follow-up"
    assert msg["notification"]["body"] == "AUD 1,500 overdue · 1 lead waiting over 24 h"
    assert msg["data"]["route"] == "/today" and msg["android"]["priority"] == "normal"
    assert msg["android"]["notification"]["channel_id"] == "daily_brief"
