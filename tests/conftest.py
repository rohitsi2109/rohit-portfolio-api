"""Tests run against a real Postgres (the schema relies on citext, arrays, triggers and a view).

    docker run -d --name portfolio-test-pg -e POSTGRES_PASSWORD=test -e POSTGRES_DB=portfolio_test \
        -p 55432:5432 postgres:17-alpine
    pytest

Override the database with TEST_DATABASE_URL. Never point it at Neon: every table is wiped."""

import hashlib
import hmac
import json
import os
from datetime import UTC, datetime, timedelta

import pytest

TEST_DB = os.environ.get("TEST_DATABASE_URL", "postgresql://postgres:test@localhost:55432/portfolio_test")
if "neon.tech" in TEST_DB:
    raise RuntimeError("TEST_DATABASE_URL points at Neon; the tests wipe every table. Use a local Postgres.")
os.environ.update({
    "DATABASE_URL": TEST_DB,
    "DATABASE_URL_DIRECT": TEST_DB,
    "JWT_SECRET": "test-secret-" + "x" * 64,
    "CAL_WEBHOOK_SECRET": "whsec-test",
    "CAL_API_KEY": "",
})

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.db import get_engine, get_sessionmaker  # noqa: E402
from app.main import app  # noqa: E402
from app.models import AdminUser  # noqa: E402
from app.services import cal  # noqa: E402
from app.services.security import hash_password  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TABLES = ("webhook_events, lead_events, lead_notes, push_devices, refresh_tokens, admin_users, payments, "
          "projects, proposals, bookings, leads, clients, contacts")


@pytest.fixture(scope="session", autouse=True)
def schema():
    with get_engine().begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    cfg = Config(os.path.join(ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(ROOT, "migrations"))
    command.upgrade(cfg, "head")


@pytest.fixture(autouse=True)
def clean():
    with get_engine().begin() as conn:
        conn.execute(text(f"TRUNCATE {TABLES} CASCADE"))


@pytest.fixture
def db():
    with get_sessionmaker()() as session:
        yield session


@pytest.fixture
def client():
    return TestClient(app)


class FakeCal:
    def __init__(self):
        self.bookings: list[dict] = []
        self.error: cal.CalError | None = None
        self.counter = 0

    def create_booking(self, **kwargs):
        if self.error:
            raise self.error
        self.counter += 1
        self.bookings.append(kwargs)
        start = datetime.fromisoformat(kwargs["start"])
        return cal.CalBooking(uid=f"cal-uid-{self.counter}", start=start.isoformat(),
                              end=(start + timedelta(minutes=15)).isoformat(),
                              meeting_url="https://meet.google.com/abc-defg-hij")

    def get_slots(self, start, end, time_zone):
        return {start: [f"{start}T10:00:00.000+11:00"]}


@pytest.fixture
def fake_cal(monkeypatch):
    fake = FakeCal()
    monkeypatch.setattr(cal, "create_booking", fake.create_booking)
    monkeypatch.setattr(cal, "get_slots", fake.get_slots)
    return fake


def future(days: int = 3) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).replace(microsecond=0).isoformat()


def booking_body(**overrides):
    body = {
        "name": "Jane Smith",
        "email": "jane@example.com",
        "phone": "+61 412 345 678",
        "phoneCountry": "AU",
        "timeZone": "Australia/Sydney",
        "start": future(),
        "notes": "A booking site for my salon",
        "consentVersion": "privacy-2026-11",
        "attribution": {"utmSource": "linkedin", "utmMedium": "social", "referrer": "https://www.linkedin.com/",
                        "landingPage": "/?utm_source=linkedin"},
    }
    body.update(overrides)
    return body


def sign(body: dict) -> tuple[bytes, dict]:
    raw = json.dumps(body).encode()
    sig = hmac.new(b"whsec-test", raw, hashlib.sha256).hexdigest()
    return raw, {"x-cal-signature-256": sig, "content-type": "application/json"}


ADMIN_EMAIL = "rohit@example.com"
ADMIN_PASSWORD = "correct horse battery staple"


@pytest.fixture
def admin_user(db):
    user = AdminUser(email=ADMIN_EMAIL, name="Rohit", password_hash=hash_password(ADMIN_PASSWORD))
    db.add(user)
    db.commit()
    return user


@pytest.fixture
def auth(client, admin_user):
    res = client.post("/v1/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    assert res.status_code == 200, res.text
    return {"Authorization": f"Bearer {res.json()['access_token']}"}
