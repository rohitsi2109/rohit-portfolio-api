"""SQLAlchemy models. They mirror db/schema.sql, which (via migration 0001) is the source of truth:
CHECK constraints, triggers and the client_summary view live in SQL, not here."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    FetchedValue,
    ForeignKey,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, CITEXT, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def created_at() -> Mapped[datetime]:
    return mapped_column(server_default=func.now())


def updated_at() -> Mapped[datetime]:
    # Maintained by the set_updated_at() trigger
    return mapped_column(server_default=func.now(), server_onupdate=FetchedValue())


class Contact(Base):
    __tablename__ = "contacts"

    id: Mapped[uuid.UUID] = pk()
    email: Mapped[str | None] = mapped_column(CITEXT, unique=True)
    name: Mapped[str] = mapped_column(Text)
    phone_e164: Mapped[str | None] = mapped_column(Text)
    phone_country: Mapped[str | None] = mapped_column(String(2))
    company: Mapped[str | None] = mapped_column(Text)
    timezone: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    leads: Mapped[list["Lead"]] = relationship(back_populates="contact", passive_deletes=True)
    client: Mapped["Client | None"] = relationship(back_populates="contact")


class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[uuid.UUID] = pk()
    contact_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contacts.id", ondelete="CASCADE"))
    type: Mapped[str] = mapped_column(Text)
    source_channel: Mapped[str] = mapped_column(Text, server_default="website")
    referred_by_client_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("clients.id", ondelete="SET NULL", use_alter=True))
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}", default=list)
    status: Mapped[str] = mapped_column(Text, server_default="new", default="new")
    temperature: Mapped[str | None] = mapped_column(Text)
    converted_at: Mapped[datetime | None]

    submitted_name: Mapped[str | None] = mapped_column(Text)
    submitted_phone: Mapped[str | None] = mapped_column(Text)
    submitted_company: Mapped[str | None] = mapped_column(Text)
    message: Mapped[str | None] = mapped_column(Text)
    budget_range: Mapped[str | None] = mapped_column(Text)
    estimated_value: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    currency: Mapped[str | None] = mapped_column(String(3))
    lost_reason: Mapped[str | None] = mapped_column(Text)
    lost_comment: Mapped[str | None] = mapped_column(Text)

    utm_source: Mapped[str | None] = mapped_column(Text)
    utm_medium: Mapped[str | None] = mapped_column(Text)
    utm_campaign: Mapped[str | None] = mapped_column(Text)
    utm_term: Mapped[str | None] = mapped_column(Text)
    utm_content: Mapped[str | None] = mapped_column(Text)
    referrer: Mapped[str | None] = mapped_column(Text)
    landing_page: Mapped[str | None] = mapped_column(Text)
    visitor_country: Mapped[str | None] = mapped_column(String(2))

    consent_at: Mapped[datetime | None]
    consent_version: Mapped[str | None] = mapped_column(Text)

    first_contacted_at: Mapped[datetime | None]
    last_contacted_at: Mapped[datetime | None]
    next_follow_up_at: Mapped[datetime | None]

    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    contact: Mapped[Contact] = relationship(back_populates="leads")
    bookings: Mapped[list["Booking"]] = relationship(
        back_populates="lead", order_by="Booking.start_at.desc()", passive_deletes=True)
    proposals: Mapped[list["Proposal"]] = relationship(
        back_populates="lead", order_by="Proposal.created_at.desc()", passive_deletes=True)
    notes: Mapped[list["LeadNote"]] = relationship(
        order_by="LeadNote.created_at.desc()", passive_deletes=True)
    events: Mapped[list["LeadEvent"]] = relationship(
        order_by="LeadEvent.created_at.desc()", passive_deletes=True)


class Booking(Base):
    __tablename__ = "bookings"

    id: Mapped[uuid.UUID] = pk()
    lead_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"))
    cal_booking_uid: Mapped[str | None] = mapped_column(Text, unique=True)
    rescheduled_from_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("bookings.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(Text)
    start_at: Mapped[datetime]
    end_at: Mapped[datetime | None]
    attendee_timezone: Mapped[str] = mapped_column(Text)
    meeting_url: Mapped[str | None] = mapped_column(Text)
    cancellation_reason: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    lead: Mapped[Lead] = relationship(back_populates="bookings")


class Proposal(Base):
    __tablename__ = "proposals"

    id: Mapped[uuid.UUID] = pk()
    lead_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(Text)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(3))
    status: Mapped[str] = mapped_column(Text, server_default="draft", default="draft")
    sent_at: Mapped[datetime | None]
    valid_until: Mapped[date | None] = mapped_column(Date)
    responded_at: Mapped[datetime | None]
    document_url: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    lead: Mapped[Lead] = relationship(back_populates="proposals")


class Client(Base):
    __tablename__ = "clients"

    id: Mapped[uuid.UUID] = pk()
    contact_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contacts.id", ondelete="RESTRICT"), unique=True)
    first_lead_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("leads.id", ondelete="SET NULL"))
    company: Mapped[str | None] = mapped_column(Text)
    industry: Mapped[str | None] = mapped_column(Text)
    country: Mapped[str | None] = mapped_column(String(2))
    converted_at: Mapped[datetime] = mapped_column(server_default=func.now())
    testimonial: Mapped[str | None] = mapped_column(Text)
    testimonial_author: Mapped[str | None] = mapped_column(Text)
    testimonial_approved: Mapped[bool] = mapped_column(Boolean, server_default="false", default=False)
    show_on_portfolio: Mapped[bool] = mapped_column(Boolean, server_default="false", default=False)
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}", default=list)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    contact: Mapped[Contact] = relationship(back_populates="client")
    projects: Mapped[list["Project"]] = relationship(
        back_populates="client", order_by="Project.created_at.desc()", passive_deletes=True)


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = pk()
    client_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"))
    lead_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("leads.id", ondelete="SET NULL"), unique=True)
    title: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default="active", default="active")
    value: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    currency: Mapped[str | None] = mapped_column(String(3))
    started_on: Mapped[date | None] = mapped_column(Date)
    completed_on: Mapped[date | None] = mapped_column(Date)
    live_url: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    client: Mapped[Client] = relationship(back_populates="projects")
    payments: Mapped[list["Payment"]] = relationship(
        back_populates="project", order_by="Payment.created_at", passive_deletes=True)


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[uuid.UUID] = pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    description: Mapped[str] = mapped_column(Text)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(3))
    status: Mapped[str] = mapped_column(Text, server_default="pending", default="pending")
    due_on: Mapped[date | None] = mapped_column(Date)
    paid_on: Mapped[date | None] = mapped_column(Date)
    method: Mapped[str | None] = mapped_column(Text)
    invoice_url: Mapped[str | None] = mapped_column(Text)
    reference: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    project: Mapped[Project] = relationship(back_populates="payments")


class AdminUser(Base):
    __tablename__ = "admin_users"

    id: Mapped[uuid.UUID] = pk()
    email: Mapped[str] = mapped_column(CITEXT, unique=True)
    name: Mapped[str] = mapped_column(Text)
    password_hash: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true", default=True)
    last_login_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = pk()
    admin_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("admin_users.id", ondelete="CASCADE"))
    token_hash: Mapped[str] = mapped_column(Text, unique=True)
    device_label: Mapped[str | None] = mapped_column(Text)
    session_started_at: Mapped[datetime] = mapped_column(server_default=func.now())
    expires_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None]
    # 'rotated' | 'logout' | 'revoked' | 'reuse_detected'
    revoked_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at()

    admin_user: Mapped[AdminUser] = relationship()


class PushDevice(Base):
    __tablename__ = "push_devices"

    id: Mapped[uuid.UUID] = pk()
    admin_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("admin_users.id", ondelete="CASCADE"))
    fcm_token: Mapped[str] = mapped_column(Text, unique=True)
    platform: Mapped[str] = mapped_column(Text, server_default="android")
    created_at: Mapped[datetime] = created_at()
    last_seen_at: Mapped[datetime] = mapped_column(server_default=func.now())


class LeadNote(Base):
    __tablename__ = "lead_notes"

    id: Mapped[uuid.UUID] = pk()
    lead_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"))
    admin_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("admin_users.id", ondelete="SET NULL"))
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()


class LeadEvent(Base):
    __tablename__ = "lead_events"

    id: Mapped[uuid.UUID] = pk()
    lead_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"))
    type: Mapped[str] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(Text)
    admin_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("admin_users.id", ondelete="SET NULL"))
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}", default=dict)
    created_at: Mapped[datetime] = created_at()


class RateLimit(Base):
    __tablename__ = "rate_limits"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    window_start: Mapped[datetime] = mapped_column(primary_key=True)
    hits: Mapped[int] = mapped_column(server_default="1", default=1)


class WebhookEvent(Base):
    __tablename__ = "webhook_events"

    id: Mapped[uuid.UUID] = pk()
    provider: Mapped[str] = mapped_column(Text, server_default="cal", default="cal")
    event_type: Mapped[str] = mapped_column(Text)
    dedupe_key: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    received_at: Mapped[datetime] = mapped_column(server_default=func.now())
    processed_at: Mapped[datetime | None]
    error: Mapped[str | None] = mapped_column(Text)
