"""Push notifications via Firebase Cloud Messaging (HTTP v1 API).

Uses the service account in FIREBASE_SERVICE_ACCOUNT (the JSON key file's contents) to get a
short-lived Google access token, then sends one message per registered device. Without that
variable, sending is skipped. Nothing here ever raises: a push failure must not fail the
request that triggered it.

Android channels match the app (docs/PLAN.md, section 9): new_leads, bookings, daily_brief.
Every message carries a `route` the app opens when the notification is tapped."""

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import jwt
from sqlalchemy import delete, select

from app.config import get_settings
from app.db import get_sessionmaker
from app.models import AdminUser, PushDevice

log = logging.getLogger(__name__)

SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
TIMEOUT = httpx.Timeout(5.0)
ACCENT = "#00D4FF"

_token_cache: dict[str, tuple[str, float]] = {}  # client_email -> (access token, expires at)


@dataclass
class Notice:
    title: str
    body: str
    channel: str  # 'new_leads' | 'bookings' | 'daily_brief'
    route: str  # opened in the app on tap, e.g. '/leads/<id>' or '/today'
    data: dict[str, str] = field(default_factory=dict)


def _service_account() -> dict | None:
    raw = get_settings().firebase_service_account.strip()
    if not raw:
        return None
    try:
        account = json.loads(raw)
        if all(account.get(k) for k in ("project_id", "client_email", "private_key")):
            return account
    except ValueError:
        pass
    log.error("FIREBASE_SERVICE_ACCOUNT is not a valid service account JSON; push disabled")
    return None


def _access_token(account: dict) -> str:
    cached = _token_cache.get(account["client_email"])
    if cached and cached[1] > time.time() + 60:
        return cached[0]
    token_uri = account.get("token_uri") or "https://oauth2.googleapis.com/token"
    issued = int(time.time())
    assertion = jwt.encode(
        {"iss": account["client_email"], "scope": SCOPE, "aud": token_uri, "iat": issued, "exp": issued + 3600},
        account["private_key"], algorithm="RS256")
    res = httpx.post(token_uri, data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                                      "assertion": assertion}, timeout=TIMEOUT)
    res.raise_for_status()
    body = res.json()
    _token_cache[account["client_email"]] = (body["access_token"], time.time() + int(body.get("expires_in", 3600)))
    return body["access_token"]


def _message(device_token: str, notice: Notice) -> dict:
    high = notice.channel != "daily_brief"
    return {"message": {
        "token": device_token,
        "notification": {"title": notice.title, "body": notice.body},
        # FCM data values must be strings
        "data": {"route": notice.route, "channel": notice.channel, **notice.data},
        "android": {
            "priority": "high" if high else "normal",
            "notification": {"channel_id": notice.channel, "color": ACCENT},
        },
    }}


def _is_dead_token(res: httpx.Response) -> bool:
    """FCM says the token will never work again (app uninstalled, token rotated)."""
    if res.status_code == 404:
        return True
    try:
        details = res.json().get("error", {}).get("details", [])
    except ValueError:
        return False
    return any(d.get("errorCode") in ("UNREGISTERED", "INVALID_ARGUMENT") for d in details if isinstance(d, dict))


def send(notice: Notice) -> int:
    """Send to every device of every active admin. Returns how many devices accepted it."""
    try:
        account = _service_account()
        if account is None:
            log.info("push skipped (FIREBASE_SERVICE_ACCOUNT not set): %s", notice.title)
            return 0
        with get_sessionmaker()() as db:
            tokens = db.scalars(select(PushDevice.fcm_token).join(AdminUser, PushDevice.admin_user_id == AdminUser.id)
                                .where(AdminUser.is_active)).all()
            if not tokens:
                return 0
            url = f"https://fcm.googleapis.com/v1/projects/{account['project_id']}/messages:send"
            headers = {"Authorization": f"Bearer {_access_token(account)}"}
            sent, dead = 0, []
            for device_token in tokens:
                res = httpx.post(url, json=_message(device_token, notice), headers=headers, timeout=TIMEOUT)
                if res.is_success:
                    sent += 1
                elif _is_dead_token(res):
                    dead.append(device_token)
                else:
                    log.warning("push failed (%s): %s", res.status_code, res.text[:300])
            if dead:
                db.execute(delete(PushDevice).where(PushDevice.fcm_token.in_(dead)))
                db.commit()
                log.info("removed %d dead push token(s)", len(dead))
            return sent
    except Exception:
        log.exception("push notification failed: %s", notice.title)
        return 0


# --- the notifications the API sends -------------------------------------------

def local_time(at: datetime) -> str:
    """'Thu 6 Nov, 3:30 pm' in the business time zone (IST), for notification text."""
    t = at.astimezone(ZoneInfo(get_settings().business_timezone))
    return f"{t:%a} {t.day} {t:%b}, {t.hour % 12 or 12}:{t:%M} {'am' if t.hour < 12 else 'pm'}"


def notify_new_lead(lead_id: uuid.UUID, title: str, body: str) -> None:
    send(Notice(title=title, body=body, channel="new_leads", route=f"/leads/{lead_id}",
                data={"type": "new_lead", "lead_id": str(lead_id)}))


def new_booking_notice(lead_id: uuid.UUID, name: str, country: str | None, start: datetime,
                       message: str | None) -> Notice:
    """"Jane (AU) booked a call" / "Thu 6 Nov, 3:30 pm IST · A booking site for my salon"."""
    who = f"{name} ({country})" if country else name
    body = f"{local_time(start)} IST" + (f" · {message[:80]}" if message else "")
    return Notice(title=f"{who} booked a call", body=body, channel="new_leads", route=f"/leads/{lead_id}",
                  data={"type": "new_lead", "lead_id": str(lead_id)})


def booking_notice(lead_id: uuid.UUID, type_: str, title: str, body: str) -> Notice:
    """A booking was cancelled or moved (Cal.com webhook). type_: booking_cancelled | booking_rescheduled,
    the names the app routes on."""
    return Notice(title=title, body=body, channel="bookings", route=f"/leads/{lead_id}",
                  data={"type": type_, "lead_id": str(lead_id)})
