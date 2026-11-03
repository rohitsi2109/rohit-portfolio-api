"""Scheduled jobs, called by Vercel Cron (see vercel.json).

Vercel sends `Authorization: Bearer $CRON_SECRET` when CRON_SECRET is set on the project;
anything else is refused."""

import hmac
from datetime import datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import Booking, Lead, Payment, Proposal
from app.services import push
from app.services.leads import OPEN_STATUSES, now, today

router = APIRouter(prefix="/v1/cron", tags=["cron"])


def require_cron_secret(authorization: str | None = Header(None)) -> None:
    secret = get_settings().cron_secret
    if not secret or not hmac.compare_digest(authorization or "", f"Bearer {secret}"):
        raise HTTPException(401, "Not authorized")


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def money(amounts: dict[str, Decimal]) -> str:
    return " + ".join(f"{cur} {amt:,.0f}" if amt == amt.to_integral() else f"{cur} {amt:,.2f}"
                      for cur, amt in sorted(amounts.items()))


def brief_items(db: Session) -> dict:
    """What tomorrow (IST) holds: the numbers behind the daily brief."""
    tz = ZoneInfo(get_settings().business_timezone)
    tomorrow = today() + timedelta(days=1)
    tomorrow_start = datetime.combine(tomorrow, time.min, tzinfo=tz)
    tomorrow_end = tomorrow_start + timedelta(days=1)

    calls = db.scalar(select(func.count()).select_from(Booking).where(
        Booking.status == "confirmed", Booking.start_at >= tomorrow_start, Booking.start_at < tomorrow_end))
    # Due by the end of tomorrow, including ones already overdue
    follow_ups = db.scalar(select(func.count()).select_from(Lead).where(
        Lead.status.in_(OPEN_STATUSES), Lead.next_follow_up_at < tomorrow_end))
    # New leads nobody has replied to for over 24 hours
    waiting = db.scalar(select(func.count()).select_from(Lead).where(
        Lead.status == "new", Lead.first_contacted_at.is_(None), Lead.created_at < now() - timedelta(hours=24)))
    # Overdue by tomorrow morning: due today or earlier and still unpaid
    overdue = {cur: amt for cur, amt in db.execute(
        select(Payment.currency, func.sum(Payment.amount))
        .where(Payment.status == "pending", Payment.due_on < tomorrow).group_by(Payment.currency)).all()}
    # Sent quotes running out within the week (or already past their date, still unanswered)
    proposals = db.scalar(select(func.count()).select_from(Proposal).where(
        and_(Proposal.status == "sent", Proposal.valid_until <= tomorrow + timedelta(days=6))))
    return {"date": tomorrow, "calls": calls, "follow_ups": follow_ups, "leads_waiting": waiting,
            "overdue": overdue, "proposals_expiring": proposals}


def brief_text(items: dict) -> tuple[str, str] | None:
    """("Tomorrow: 1 call, 2 follow-ups", "AUD 1,500 overdue · 1 quote expiring · 3 leads waiting"),
    or None when there's nothing to act on."""
    headline = [plural(items["calls"], "call") if items["calls"] else "",
                plural(items["follow_ups"], "follow-up") if items["follow_ups"] else ""]
    details = [f"{money(items['overdue'])} overdue" if items["overdue"] else "",
               f"{plural(items['proposals_expiring'], 'quote')} expiring" if items["proposals_expiring"] else "",
               f"{plural(items['leads_waiting'], 'lead')} waiting over 24 h" if items["leads_waiting"] else ""]
    headline, details = [h for h in headline if h], [d for d in details if d]
    if not headline and not details:
        return None
    title = f"Tomorrow: {', '.join(headline)}" if headline else "Tomorrow: no calls or follow-ups"
    return title, " · ".join(details) or "Nothing overdue."


@router.get("/daily-brief", dependencies=[Depends(require_cron_secret)])
def daily_brief(db: Session = Depends(get_db)):
    """Nightly look-ahead notification (11 pm IST). Sends nothing when there's nothing to act on."""
    items = brief_items(db)
    text = brief_text(items)
    if text is None:
        return {"sent": False, "reason": "nothing to report", "items": items}
    title, body = text
    devices = push.send(push.Notice(title=title, body=body, channel="daily_brief", route="/today",
                                    data={"type": "daily_brief", "date": items["date"].isoformat()}))
    return {"sent": devices > 0, "devices": devices, "title": title, "body": body, "items": items}
