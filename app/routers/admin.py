"""Admin API for the Flutter app. Every route requires a valid access token."""

import base64
import json
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import and_, case, column, func, or_, select, table, true, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.db import get_db
from app.models import (
    AdminUser,
    Booking,
    Client,
    Contact,
    Lead,
    LeadNote,
    Payment,
    Project,
    Proposal,
    PushDevice,
)
from app.schemas import (
    ClientDetail,
    ClientLeadOut,
    ClientSummaryOut,
    ClientUpdate,
    ContactedRequest,
    ContactOut,
    ContactUpdate,
    ConvertRequest,
    ConvertResult,
    DeviceRegister,
    LeadDetail,
    LeadListItem,
    LeadPage,
    LeadStatus,
    LeadType,
    LeadUpdate,
    ManualLeadCreate,
    ManualLeadCreated,
    NoteCreate,
    NoteOut,
    PaymentCreate,
    PaymentListItem,
    PaymentOut,
    PaymentUpdate,
    ProjectCreate,
    ProjectOut,
    ProjectUpdate,
    ProposalCreate,
    ProposalOut,
    ProposalUpdate,
    SourceChannel,
    Stats,
    Temperature,
)
from app.services.leads import OPEN_STATUSES, convert_lead, log_event, mark_contacted, now
from app.services.leads import today as local_today
from app.services.security import current_admin

router = APIRouter(prefix="/v1/admin", tags=["admin"])
Admin = Depends(current_admin)

# hot -> warm -> cold -> unrated
TEMPERATURE_RANK = case((Lead.temperature == "hot", 0), (Lead.temperature == "warm", 1),
                        (Lead.temperature == "cold", 2), else_=3)


def get_or_404[T](db: Session, model: type[T], id_: uuid.UUID, what: str) -> T:
    obj = db.get(model, id_)
    if obj is None:
        raise HTTPException(404, f"{what} not found")
    return obj


def lead_detail(db: Session, lead_id: uuid.UUID) -> LeadDetail:
    lead = db.scalar(
        select(Lead).where(Lead.id == lead_id).options(
            selectinload(Lead.contact), selectinload(Lead.bookings), selectinload(Lead.proposals),
            selectinload(Lead.notes), selectinload(Lead.events)))
    if lead is None:
        raise HTTPException(404, "Lead not found")
    detail = LeadDetail.model_validate(lead)
    detail.client_id = db.scalar(select(Client.id).where(Client.contact_id == lead.contact_id))
    return detail


def check_client(db: Session, client_id: uuid.UUID | None) -> None:
    if client_id is not None and db.get(Client, client_id) is None:
        raise HTTPException(422, "Referring client not found")


# ---------------------------------------------------------------------------
# Leads
# ---------------------------------------------------------------------------

def encode_cursor(rank: int, created_at: datetime, id_: uuid.UUID) -> str:
    raw = json.dumps([rank, created_at.isoformat(), str(id_)]).encode()
    return base64.urlsafe_b64encode(raw).decode()


def decode_cursor(cursor: str) -> tuple[int, datetime, uuid.UUID]:
    try:
        rank, created_at, id_ = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        return int(rank), datetime.fromisoformat(created_at), uuid.UUID(id_)
    except (ValueError, TypeError):
        raise HTTPException(400, "Invalid cursor")


@router.get("/leads", response_model=LeadPage)
def list_leads(
    db: Session = Depends(get_db),
    _: AdminUser = Admin,
    status: list[LeadStatus] | None = Query(None, description="Default: open leads only"),
    temperature: list[Temperature | Literal["unrated"]] | None = Query(None),
    type: list[LeadType] | None = Query(None),
    channel: list[SourceChannel] | None = Query(None),
    source: str | None = Query(None, description="utm_source"),
    tag: str | None = None,
    country: str | None = Query(None, description="visitor_country"),
    referred_by: uuid.UUID | None = None,
    q: str | None = Query(None, description="Name, email or phone"),
    from_: datetime | None = Query(None, alias="from"),
    to: datetime | None = None,
    follow_up_due: bool = False,
    cursor: str | None = None,
    limit: int = Query(30, ge=1, le=100),
):
    rank = TEMPERATURE_RANK.label("rank")
    next_booking = (
        select(func.min(Booking.start_at))
        .where(Booking.lead_id == Lead.id, Booking.status == "confirmed", Booking.start_at >= func.now())
        .correlate(Lead).scalar_subquery()
    )
    stmt = select(Lead, rank, next_booking).join(Lead.contact).options(selectinload(Lead.contact))

    stmt = stmt.where(Lead.status.in_(status) if status else Lead.status.in_(OPEN_STATUSES))
    if temperature:
        rated = [t for t in temperature if t != "unrated"]
        stmt = stmt.where(or_(Lead.temperature.in_(rated),
                              *([Lead.temperature.is_(None)] if "unrated" in temperature else [])))
    if type:
        stmt = stmt.where(Lead.type.in_(type))
    if channel:
        stmt = stmt.where(Lead.source_channel.in_(channel))
    if source:
        stmt = stmt.where(Lead.utm_source == source)
    if tag:
        stmt = stmt.where(Lead.tags.contains([tag]))
    if country:
        stmt = stmt.where(Lead.visitor_country == country.upper())
    if referred_by:
        stmt = stmt.where(Lead.referred_by_client_id == referred_by)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Contact.name.ilike(like), Contact.email.ilike(like), Contact.phone_e164.ilike(like),
                              Contact.company.ilike(like)))
    if from_:
        stmt = stmt.where(Lead.created_at >= from_)
    if to:
        stmt = stmt.where(Lead.created_at < to)
    if follow_up_due:
        stmt = stmt.where(Lead.next_follow_up_at <= func.now())
    if cursor:
        c_rank, c_created, c_id = decode_cursor(cursor)
        stmt = stmt.where(or_(TEMPERATURE_RANK > c_rank,
                              and_(TEMPERATURE_RANK == c_rank,
                                   tuple_(Lead.created_at, Lead.id) < tuple_(c_created, c_id))))

    rows = db.execute(stmt.order_by(rank, Lead.created_at.desc(), Lead.id.desc()).limit(limit + 1)).all()
    items = []
    for lead, _rank, next_at in rows[:limit]:
        item = LeadListItem.model_validate(lead)
        item.next_booking_at = next_at
        items.append(item)
    next_cursor = None
    if len(rows) > limit:
        last, last_rank, _ = rows[limit - 1]
        next_cursor = encode_cursor(last_rank, last.created_at, last.id)
    return LeadPage(items=items, next_cursor=next_cursor)


@router.post("/leads", response_model=ManualLeadCreated, status_code=201)
def create_manual_lead(body: ManualLeadCreate, db: Session = Depends(get_db), admin: AdminUser = Admin):
    if body.email is None and body.phone_e164 is None:
        raise HTTPException(422, "Enter an email or a phone number")
    check_client(db, body.referred_by_client_id)

    warnings = []
    contact = None
    if body.email:
        contact = db.scalar(select(Contact).where(Contact.email == body.email))
        if contact:
            warnings.append(f"{contact.name} already exists with this email; the lead was added to them.")
    if body.phone_e164:
        same_phone = db.scalars(select(Contact).where(Contact.phone_e164 == body.phone_e164)).all()
        for other in same_phone:
            if contact is None or other.id != contact.id:
                warnings.append(f"{other.name} already has this phone number.")

    if contact is None:
        contact = Contact(name=body.name, email=body.email, phone_e164=body.phone_e164,
                          phone_country=body.phone_country, company=body.company)
        db.add(contact)
        db.flush()
    else:
        contact.phone_e164 = contact.phone_e164 or body.phone_e164
        contact.phone_country = contact.phone_country or body.phone_country
        contact.company = contact.company or body.company

    lead = Lead(contact_id=contact.id, type="manual", source_channel=body.source_channel,
                referred_by_client_id=body.referred_by_client_id, message=body.message,
                temperature=body.temperature, tags=body.tags, estimated_value=body.estimated_value,
                currency=body.currency, next_follow_up_at=body.next_follow_up_at, status="new")
    db.add(lead)
    db.flush()
    log_event(db, lead.id, "lead_created", "admin", {"type": "manual", "source_channel": body.source_channel},
              admin)
    db.commit()
    return ManualLeadCreated(lead=lead_detail(db, lead.id), warnings=warnings)


@router.get("/leads/{lead_id}", response_model=LeadDetail)
def get_lead(lead_id: uuid.UUID, db: Session = Depends(get_db), _: AdminUser = Admin):
    return lead_detail(db, lead_id)


@router.patch("/leads/{lead_id}", response_model=LeadDetail)
def update_lead(lead_id: uuid.UUID, body: LeadUpdate, db: Session = Depends(get_db), admin: AdminUser = Admin):
    lead = get_or_404(db, Lead, lead_id, "Lead")
    changes = body.model_dump(exclude_unset=True)
    at = now()

    if "status" in changes and changes["status"] is None:
        raise HTTPException(422, "status cannot be empty")
    new_status = changes.get("status", lead.status)
    if new_status == "won" and lead.status != "won":
        raise HTTPException(409, "Use POST /v1/admin/leads/{id}/convert to mark a lead as won")
    if changes.get("lost_reason") and new_status != "lost":
        raise HTTPException(422, "lost_reason can only be set on a lost lead")
    if "referred_by_client_id" in changes:
        check_client(db, changes["referred_by_client_id"])

    if new_status != lead.status:
        log_event(db, lead.id, "status_changed", "admin", {"from": lead.status, "to": new_status}, admin)
        if new_status == "contacted":
            mark_contacted(lead, at)
        if lead.status == "won":
            lead.converted_at = None  # the client and project stay; they're removed by hand
        if new_status != "lost":
            lead.lost_reason = None
            lead.lost_comment = None
        lead.status = new_status

    if "temperature" in changes and changes["temperature"] != lead.temperature:
        log_event(db, lead.id, "temperature_changed", "admin",
                  {"from": lead.temperature, "to": changes["temperature"]}, admin)
        lead.temperature = changes["temperature"]
    if "tags" in changes:
        tags = sorted({t.strip() for t in changes["tags"] or [] if t.strip()})
        if tags != sorted(lead.tags):
            log_event(db, lead.id, "tags_changed", "admin", {"from": lead.tags, "to": tags}, admin)
            lead.tags = tags
    if "next_follow_up_at" in changes and changes["next_follow_up_at"] != lead.next_follow_up_at:
        follow_up = changes["next_follow_up_at"]
        log_event(db, lead.id, "follow_up_set", "admin",
                  {"at": follow_up.isoformat() if follow_up else None}, admin)
        lead.next_follow_up_at = follow_up
    for field in ("estimated_value", "currency", "referred_by_client_id"):
        if field in changes:
            setattr(lead, field, changes[field])
    if new_status == "lost":
        for field in ("lost_reason", "lost_comment"):
            if field in changes:
                setattr(lead, field, changes[field])

    db.commit()
    return lead_detail(db, lead.id)


@router.post("/leads/{lead_id}/proposals", response_model=ProposalOut, status_code=201)
def create_proposal(lead_id: uuid.UUID, body: ProposalCreate, db: Session = Depends(get_db),
                    admin: AdminUser = Admin):
    get_or_404(db, Lead, lead_id, "Lead")
    proposal = Proposal(lead_id=lead_id, **body.model_dump())
    if body.status == "sent":
        proposal.sent_at = now()
    db.add(proposal)
    db.flush()
    if body.status == "sent":
        log_event(db, lead_id, "proposal_sent", "admin", proposal_event_data(proposal), admin)
    db.commit()
    return proposal


def proposal_event_data(p: Proposal) -> dict:
    return {"proposal_id": str(p.id), "title": p.title, "amount": str(p.amount), "currency": p.currency}


@router.patch("/proposals/{proposal_id}", response_model=ProposalOut)
def update_proposal(proposal_id: uuid.UUID, body: ProposalUpdate, db: Session = Depends(get_db),
                    admin: AdminUser = Admin):
    proposal = get_or_404(db, Proposal, proposal_id, "Proposal")
    changes = body.model_dump(exclude_unset=True)
    new_status = changes.pop("status", None) or proposal.status
    for field, value in changes.items():
        setattr(proposal, field, value)

    if new_status != proposal.status:
        if new_status == "draft":
            raise HTTPException(409, "A sent proposal can't go back to draft; record a revised quote instead")
        at = now()
        if proposal.sent_at is None:
            proposal.sent_at = at
        if new_status in ("accepted", "declined"):
            proposal.responded_at = at
        proposal.status = new_status
        log_event(db, proposal.lead_id, f"proposal_{new_status}", "admin", proposal_event_data(proposal), admin)

    db.commit()
    return proposal


@router.post("/leads/{lead_id}/convert", response_model=ConvertResult, status_code=201)
def convert(lead_id: uuid.UUID, body: ConvertRequest, db: Session = Depends(get_db), admin: AdminUser = Admin):
    lead = get_or_404(db, Lead, lead_id, "Lead")
    try:
        result = convert_lead(db, lead, body, admin)
    except ValueError as e:
        raise HTTPException(409, str(e))
    db.commit()
    return result


@router.post("/leads/{lead_id}/notes", response_model=NoteOut, status_code=201)
def add_note(lead_id: uuid.UUID, body: NoteCreate, db: Session = Depends(get_db), admin: AdminUser = Admin):
    get_or_404(db, Lead, lead_id, "Lead")
    note = LeadNote(lead_id=lead_id, admin_user_id=admin.id, body=body.body.strip())
    db.add(note)
    db.flush()
    log_event(db, lead_id, "note_added", "admin", {"note_id": str(note.id)}, admin)
    db.commit()
    return note


@router.post("/leads/{lead_id}/contacted", response_model=LeadDetail)
def contacted(lead_id: uuid.UUID, body: ContactedRequest, db: Session = Depends(get_db), admin: AdminUser = Admin):
    lead = get_or_404(db, Lead, lead_id, "Lead")
    mark_contacted(lead, now())
    log_event(db, lead.id, "contacted", "admin", {"channel": body.channel}, admin)
    db.commit()
    return lead_detail(db, lead.id)


# ---------------------------------------------------------------------------
# Clients, projects, payments
# ---------------------------------------------------------------------------

def client_summary_table():
    cols = ["client_id", "name", "email", "phone_e164", "company", "country", "converted_at", "tags",
            "project_count", "active_projects", "last_project_at", "contracted_value", "total_paid",
            "outstanding", "overdue", "referrals_sent"]
    return table("client_summary", *[column(c) for c in cols])


@router.get("/clients", response_model=list[ClientSummaryOut])
def list_clients(db: Session = Depends(get_db), _: AdminUser = Admin, country: str | None = None,
                 q: str | None = None, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    cs = client_summary_table()
    stmt = select(cs)
    if country:
        stmt = stmt.where(cs.c.country == country.upper())
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(cs.c.name.ilike(like), cs.c.email.ilike(like), cs.c.company.ilike(like)))
    rows = db.execute(stmt.order_by(cs.c.converted_at.desc()).limit(limit).offset(offset)).mappings().all()
    return [ClientSummaryOut.model_validate(dict(r)) for r in rows]


def client_detail(db: Session, client_id: uuid.UUID) -> ClientDetail:
    client = db.scalar(select(Client).where(Client.id == client_id).options(
        selectinload(Client.contact), selectinload(Client.projects).selectinload(Project.payments)))
    if client is None:
        raise HTTPException(404, "Client not found")
    leads = db.scalars(select(Lead).where(Lead.contact_id == client.contact_id)
                       .options(selectinload(Lead.events)).order_by(Lead.created_at)).all()
    detail = ClientDetail.model_validate(client)
    detail.leads = [ClientLeadOut.model_validate(lead) for lead in leads]
    return detail


@router.get("/clients/{client_id}", response_model=ClientDetail)
def get_client(client_id: uuid.UUID, db: Session = Depends(get_db), _: AdminUser = Admin):
    return client_detail(db, client_id)


@router.patch("/clients/{client_id}", response_model=ClientDetail)
def update_client(client_id: uuid.UUID, body: ClientUpdate, db: Session = Depends(get_db), _: AdminUser = Admin):
    client = get_or_404(db, Client, client_id, "Client")
    for field, value in body.model_dump(exclude_unset=True).items():
        if field in ("testimonial_approved", "show_on_portfolio", "tags") and value is None:
            raise HTTPException(422, f"{field} cannot be empty")
        setattr(client, field, value)
    db.commit()
    return client_detail(db, client_id)


@router.post("/clients/{client_id}/projects", response_model=ProjectOut, status_code=201)
def create_project(client_id: uuid.UUID, body: ProjectCreate, db: Session = Depends(get_db), _: AdminUser = Admin):
    get_or_404(db, Client, client_id, "Client")
    project = Project(client_id=client_id, **body.model_dump())
    db.add(project)
    db.commit()
    return ProjectOut.model_validate(project)


@router.patch("/projects/{project_id}", response_model=ProjectOut)
def update_project(project_id: uuid.UUID, body: ProjectUpdate, db: Session = Depends(get_db), _: AdminUser = Admin):
    project = get_or_404(db, Project, project_id, "Project")
    for field, value in body.model_dump(exclude_unset=True).items():
        if field in ("title", "status") and value is None:
            raise HTTPException(422, f"{field} cannot be empty")
        setattr(project, field, value)
    if project.status == "completed" and project.completed_on is None:
        project.completed_on = local_today()
    db.commit()
    return ProjectOut.model_validate(project)


@router.post("/projects/{project_id}/payments", response_model=PaymentOut, status_code=201)
def create_payment(project_id: uuid.UUID, body: PaymentCreate, db: Session = Depends(get_db), _: AdminUser = Admin):
    get_or_404(db, Project, project_id, "Project")
    payment = Payment(project_id=project_id, status="pending", **body.model_dump())
    db.add(payment)
    db.commit()
    return payment


@router.patch("/payments/{payment_id}", response_model=PaymentOut)
def update_payment(payment_id: uuid.UUID, body: PaymentUpdate, db: Session = Depends(get_db), _: AdminUser = Admin):
    payment = get_or_404(db, Payment, payment_id, "Payment")
    changes = body.model_dump(exclude_unset=True)
    for field in ("status", "description", "amount"):
        if field in changes and changes[field] is None:
            raise HTTPException(422, f"{field} cannot be empty")
    for field, value in changes.items():
        setattr(payment, field, value)
    if payment.status == "paid" and payment.paid_on is None:
        payment.paid_on = local_today()
    if payment.status != "paid":
        payment.paid_on = None
    db.commit()
    return payment


@router.get("/payments", response_model=list[PaymentListItem])
def list_payments(db: Session = Depends(get_db), _: AdminUser = Admin,
                  state: Literal["overdue", "due_soon", "pending", "received"] = "pending",
                  due_soon_days: int = Query(14, ge=1, le=90),
                  received_since: date | None = Query(None, description="state=received only; default: 90 days ago")):
    today = local_today()
    stmt = (select(Payment, Project.title, Client.id, Contact.name)
            .join(Project, Payment.project_id == Project.id)
            .join(Client, Project.client_id == Client.id)
            .join(Contact, Client.contact_id == Contact.id))
    if state == "received":
        since = received_since or today - timedelta(days=90)
        stmt = stmt.where(Payment.status == "paid", Payment.paid_on >= since)
        rows = db.execute(stmt.order_by(Payment.paid_on.desc(), Payment.created_at.desc())).all()
    else:
        stmt = stmt.where(Payment.status == "pending")
        if state == "overdue":
            stmt = stmt.where(Payment.due_on < today)
        elif state == "due_soon":
            stmt = stmt.where(Payment.due_on >= today, Payment.due_on <= today + timedelta(days=due_soon_days))
        rows = db.execute(stmt.order_by(Payment.due_on.asc().nulls_last(), Payment.created_at)).all()
    return [
        PaymentListItem(**PaymentOut.model_validate(p).model_dump(), project_title=title, client_id=cid,
                        client_name=name,
                        is_overdue=p.status == "pending" and p.due_on is not None and p.due_on < today)
        for p, title, cid, name in rows
    ]


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

@router.get("/stats", response_model=Stats)
def stats(db: Session = Depends(get_db), _: AdminUser = Admin,
          from_: datetime | None = Query(None, alias="from"), to: datetime | None = None):
    def in_range(col):
        conds = []
        if from_:
            conds.append(col >= from_)
        if to:
            conds.append(col < to)
        return and_(true(), *conds)

    lead_range = in_range(Lead.created_at)

    def count_by(col) -> dict[str, int]:
        rows = db.execute(select(col, func.count()).where(lead_range).group_by(col)).all()
        return {str(k) if k is not None else "unknown": n for k, n in rows}

    by_status = count_by(Lead.status)
    total = sum(by_status.values())
    won = by_status.get("won", 0)
    non_spam = total - by_status.get("spam", 0)

    avg_response = db.scalar(
        select(func.avg(func.extract("epoch", Lead.first_contacted_at - Lead.created_at)))
        .where(lead_range, Lead.first_contacted_at.is_not(None)))

    lost = db.execute(select(Lead.lost_reason, func.count())
                      .where(lead_range, Lead.status == "lost").group_by(Lead.lost_reason)).all()

    proposals = dict(db.execute(select(Proposal.status, func.count())
                                .where(in_range(Proposal.created_at)).group_by(Proposal.status)).all())
    decided = proposals.get("accepted", 0) + proposals.get("declined", 0)

    def money(stmt) -> dict[str, Decimal]:
        return {cur: amount for cur, amount in db.execute(stmt).all()}

    paid_range = and_(true(), *([Payment.paid_on >= from_.date()] if from_ else []),
                      *([Payment.paid_on < to.date()] if to else []))
    received = money(select(Payment.currency, func.sum(Payment.amount))
                     .where(Payment.status == "paid", paid_range).group_by(Payment.currency))
    outstanding = money(select(Payment.currency, func.sum(Payment.amount))
                        .where(Payment.status == "pending").group_by(Payment.currency))

    referrers = db.execute(
        select(Client.id, Contact.name, Client.company, func.count(Lead.id).label("n"))
        .join(Lead, Lead.referred_by_client_id == Client.id)
        .join(Contact, Client.contact_id == Contact.id)
        .where(lead_range)
        .group_by(Client.id, Contact.name, Client.company)
        .order_by(func.count(Lead.id).desc()).limit(5)).all()

    return Stats(
        period={"from": from_, "to": to},
        total_leads=total,
        leads_by_status=by_status,
        leads_by_channel=count_by(Lead.source_channel),
        leads_by_source=count_by(func.coalesce(Lead.utm_source, "(direct)")),
        leads_by_country=count_by(Lead.visitor_country),
        won=won,
        conversion_rate=round(won / non_spam, 4) if non_spam else None,
        avg_response_seconds=float(avg_response) if avg_response is not None else None,
        lost_reasons={(r or "unspecified"): n for r, n in lost},
        proposals=proposals,
        proposal_win_rate=round(proposals.get("accepted", 0) / decided, 4) if decided else None,
        money_received=received,
        money_outstanding=outstanding,
        top_referrers=[{"client_id": str(cid), "name": name, "company": company, "leads": n}
                       for cid, name, company, n in referrers],
    )


# ---------------------------------------------------------------------------
# Devices + privacy
# ---------------------------------------------------------------------------

@router.post("/devices", status_code=204)
def register_device(body: DeviceRegister, db: Session = Depends(get_db), admin: AdminUser = Admin):
    stmt = insert(PushDevice).values(id=uuid.uuid4(), admin_user_id=admin.id, fcm_token=body.fcm_token,
                                     platform=body.platform)
    stmt = stmt.on_conflict_do_update(
        index_elements=[PushDevice.fcm_token],
        set_={"admin_user_id": admin.id, "platform": body.platform, "last_seen_at": func.now()})
    db.execute(stmt)
    db.commit()
    return Response(status_code=204)


@router.patch("/contacts/{contact_id}", response_model=ContactOut)
def update_contact(contact_id: uuid.UUID, body: ContactUpdate, db: Session = Depends(get_db), _: AdminUser = Admin):
    """Correct a contact's details. The website never changes an existing contact, so this is how
    a new name, number or time zone (often visible on a lead's submitted_* fields) gets applied."""
    contact = get_or_404(db, Contact, contact_id, "Contact")
    changes = body.model_dump(exclude_unset=True)
    if "name" in changes and not changes["name"]:
        raise HTTPException(422, "name cannot be empty")
    if "timezone" in changes and changes["timezone"]:
        try:
            ZoneInfo(changes["timezone"])
        except (ZoneInfoNotFoundError, ValueError):
            raise HTTPException(422, "Unknown time zone")
    for field, value in changes.items():
        setattr(contact, field, value)
    if contact.email is None and contact.phone_e164 is None:
        raise HTTPException(422, "A contact needs an email or a phone number")
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Another contact already has this email")
    return contact


@router.delete("/contacts/{contact_id}", status_code=204)
def erase_contact(contact_id: uuid.UUID, db: Session = Depends(get_db), _: AdminUser = Admin):
    """Right-to-erasure: deletes the contact and (by cascade) their leads, bookings, notes and events."""
    contact = get_or_404(db, Contact, contact_id, "Contact")
    if db.scalar(select(Client.id).where(Client.contact_id == contact_id)):
        raise HTTPException(409, "This person is a client. Their projects and payments must be removed "
                                 "deliberately before they can be erased.")
    db.delete(contact)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "This contact is still referenced by client records")
    return Response(status_code=204)
