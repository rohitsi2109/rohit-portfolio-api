"""Lead, contact and booking logic shared by the website, webhook and admin routes."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import AdminUser, Booking, Client, Contact, Lead, LeadEvent, Project
from app.schemas import Attribution, ConvertRequest, ConvertResult

OPEN_STATUSES = ("new", "contacted", "qualified", "proposal_sent")

# Attribution field (website, camelCase) -> leads column
ATTRIBUTION_COLUMNS = {
    "utmSource": "utm_source",
    "utmMedium": "utm_medium",
    "utmCampaign": "utm_campaign",
    "utmTerm": "utm_term",
    "utmContent": "utm_content",
    "referrer": "referrer",
    "landingPage": "landing_page",
}


def now() -> datetime:
    return datetime.now(UTC)


def log_event(db: Session, lead_id: uuid.UUID, type_: str, actor: str,
              data: dict[str, Any] | None = None, admin: AdminUser | None = None) -> None:
    db.add(LeadEvent(lead_id=lead_id, type=type_, actor=actor, data=data or {},
                     admin_user_id=admin.id if admin else None))


def upsert_contact(db: Session, *, email: str, name: str, phone: str | None = None,
                   phone_country: str | None = None, timezone: str | None = None,
                   company: str | None = None) -> uuid.UUID:
    """Find the contact by email (case-insensitive) and refresh their details, or create them."""
    values = {"email": email, "name": name, "phone_e164": phone, "phone_country": phone_country,
              "timezone": timezone, "company": company}
    stmt = insert(Contact).values(id=uuid.uuid4(), **values)
    # Only overwrite with values we actually have (an enquiry without a phone keeps the old phone)
    refresh = {k: stmt.excluded[k] for k, v in values.items() if v is not None and k != "email"}
    stmt = stmt.on_conflict_do_update(index_elements=[Contact.email], set_=refresh).returning(Contact.id)
    return db.execute(stmt).scalar_one()


@dataclass
class WebsiteLead:
    """Everything the website sends about a visitor, as stored on the lead."""
    type: str  # 'booking' | 'enquiry'
    message: str | None
    attribution: Attribution | None
    consent_version: str
    consent_at: datetime
    visitor_country: str | None
    budget_range: str | None = None


def create_website_lead(db: Session, lead_id: uuid.UUID, contact_id: uuid.UUID, w: WebsiteLead) -> Lead:
    attribution = w.attribution.model_dump() if w.attribution else {}
    lead = Lead(
        id=lead_id,
        contact_id=contact_id,
        type=w.type,
        source_channel="website",
        status="new",
        tags=[],
        message=w.message or None,
        budget_range=w.budget_range,
        visitor_country=w.visitor_country,
        consent_at=w.consent_at,
        consent_version=w.consent_version,
        **{col: attribution.get(field) for field, col in ATTRIBUTION_COLUMNS.items()},
    )
    db.add(lead)
    db.flush()
    log_event(db, lead.id, "lead_created", "system", {"type": w.type, "source_channel": "website"})
    return lead


def booking_metadata(lead_id: uuid.UUID, w: WebsiteLead, phone_country: str | None) -> dict[str, str]:
    """Sent to Cal.com with the booking and echoed back in its webhooks. If the database was down
    when the visitor booked, the BOOKING_CREATED webhook rebuilds the lead from this."""
    meta: dict[str, str | None] = {
        "source": "portfolio",
        "leadId": str(lead_id),
        "consentVersion": w.consent_version,
        "consentAt": w.consent_at.isoformat(),
        "visitorCountry": w.visitor_country,
        "phoneCountry": phone_country,
        **(w.attribution.model_dump() if w.attribution else {}),
    }
    # Cal.com metadata: string values, at most 500 characters each
    return {k: v[:500] for k, v in meta.items() if v}


def attach_confirmed_booking(db: Session, *, lead_id: uuid.UUID, uid: str, start_at: datetime,
                             end_at: datetime | None, timezone: str, meeting_url: str | None,
                             actor: str, rescheduled_from_id: uuid.UUID | None = None) -> bool:
    """Insert a confirmed booking unless one with this Cal.com uid exists.

    Both the booking endpoint and the BOOKING_CREATED webhook call this, in either order;
    whichever runs first records the booking and its event. Returns True if it inserted."""
    stmt = (
        insert(Booking)
        .values(id=uuid.uuid4(), lead_id=lead_id, cal_booking_uid=uid, status="confirmed",
                start_at=start_at, end_at=end_at, attendee_timezone=timezone, meeting_url=meeting_url,
                rescheduled_from_id=rescheduled_from_id)
        .on_conflict_do_nothing(index_elements=[Booking.cal_booking_uid])
        .returning(Booking.id)
    )
    inserted = db.execute(stmt).scalar_one_or_none() is not None
    if inserted:
        log_event(db, lead_id, "booking_rescheduled" if rescheduled_from_id else "booking_confirmed", actor,
                  {"cal_booking_uid": uid, "start_at": start_at.isoformat()})
    return inserted


def mark_contacted(lead: Lead, at: datetime) -> None:
    lead.last_contacted_at = at
    if lead.first_contacted_at is None:
        lead.first_contacted_at = at  # response time = first_contacted_at - created_at


def convert_lead(db: Session, lead: Lead, req: ConvertRequest, admin: AdminUser) -> ConvertResult:
    """Mark a lead as won and create (or reuse) the client and a project. Caller commits."""
    if db.scalar(select(Project.id).where(Project.lead_id == lead.id)):
        raise ValueError("This lead has already been converted")

    at = now()
    previous = lead.status
    lead.status = "won"
    lead.converted_at = at
    lead.lost_reason = None
    lead.lost_comment = None

    client = db.scalar(select(Client).where(Client.contact_id == lead.contact_id))
    created = client is None
    if client is None:
        contact = lead.contact
        client = Client(
            contact_id=lead.contact_id,
            first_lead_id=lead.id,
            company=req.company or contact.company,
            industry=req.industry,
            country=contact.phone_country or lead.visitor_country,
            converted_at=at,
            tags=list(lead.tags),
        )
        db.add(client)
        db.flush()
    else:
        if req.company and not client.company:
            client.company = req.company
        if req.industry and not client.industry:
            client.industry = req.industry

    project = Project(client_id=client.id, lead_id=lead.id, title=req.title, status="active",
                      value=req.value, currency=req.currency or lead.currency,
                      started_on=req.started_on)
    db.add(project)
    db.flush()

    if previous != "won":
        log_event(db, lead.id, "status_changed", "admin", {"from": previous, "to": "won"}, admin)
    log_event(db, lead.id, "converted", "admin",
              {"client_id": str(client.id), "project_id": str(project.id), "client_created": created}, admin)
    return ConvertResult(client_id=client.id, project_id=project.id, client_created=created)
