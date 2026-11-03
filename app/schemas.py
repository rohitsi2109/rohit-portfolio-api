"""Pydantic request/response models.

Public (website) payloads use camelCase to match the existing frontend; admin payloads use
snake_case, matching the database columns the Flutter app works with."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints, field_validator

LeadStatus = Literal["new", "contacted", "qualified", "proposal_sent", "won", "lost", "spam"]
LeadType = Literal["booking", "enquiry", "manual"]
Temperature = Literal["hot", "warm", "cold"]
SourceChannel = Literal["website", "linkedin", "whatsapp", "referral", "upwork", "email", "event", "other"]
LostReason = Literal["price", "timing", "chose_competitor", "no_response", "not_a_fit", "other"]
ProposalStatus = Literal["draft", "sent", "accepted", "declined", "expired"]
ProjectStatus = Literal["active", "on_hold", "completed", "cancelled"]
PaymentStatus = Literal["pending", "paid", "cancelled"]

Currency = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
CountryCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{2}$")]
Phone = Annotated[str, StringConstraints(pattern=r"^\+[1-9][0-9]{6,14}$")]


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Public
# ---------------------------------------------------------------------------

class Attribution(BaseModel):
    """Captured by the website on the visitor's first page view. Everything is optional."""
    utmSource: str | None = None
    utmMedium: str | None = None
    utmCampaign: str | None = None
    utmTerm: str | None = None
    utmContent: str | None = None
    referrer: str | None = None
    landingPage: str | None = None

    @field_validator("*", mode="before")
    @classmethod
    def clip(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = v.strip()[:500]
            return v or None
        return None if v is not None and not isinstance(v, str) else v


class BookingRequest(BaseModel):
    """Lenient on purpose: the endpoint validates by hand so it can return friendly messages."""
    name: str = ""
    email: str = ""
    phone: str = ""
    phoneCountry: str | None = None
    timeZone: str = ""
    start: str = ""
    notes: str = ""
    consentVersion: str | None = None
    attribution: Attribution | None = None
    honeypot: str | None = None


class BookingResult(BaseModel):
    uid: str | None
    start: str | None
    end: str | None
    meetingUrl: str | None = None


class EnquiryRequest(BaseModel):
    name: str = ""
    email: str = ""
    phone: str = ""
    phoneCountry: str | None = None
    company: str = ""
    message: str = ""
    budgetRange: str | None = None
    consentVersion: str | None = None
    attribution: Attribution | None = None
    honeypot: str | None = None


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)
    device_label: str | None = Field(None, max_length=100)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1, max_length=200)


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class SessionOut(BaseModel):
    """A signed-in device."""
    id: uuid.UUID
    device_label: str | None
    signed_in_at: datetime
    last_active_at: datetime  # last token refresh
    expires_at: datetime
    is_current: bool  # the device making this request


# ---------------------------------------------------------------------------
# Admin: shared shapes
# ---------------------------------------------------------------------------

class ContactOut(ORM):
    id: uuid.UUID
    name: str
    email: str | None
    phone_e164: str | None
    phone_country: str | None
    company: str | None
    timezone: str | None
    created_at: datetime


class BookingOut(ORM):
    id: uuid.UUID
    cal_booking_uid: str | None
    rescheduled_from_id: uuid.UUID | None
    status: str
    start_at: datetime
    end_at: datetime | None
    attendee_timezone: str
    meeting_url: str | None
    cancellation_reason: str | None
    error_message: str | None
    created_at: datetime


class ProposalOut(ORM):
    id: uuid.UUID
    lead_id: uuid.UUID
    title: str
    amount: Decimal
    currency: str
    status: str
    sent_at: datetime | None
    valid_until: date | None
    responded_at: datetime | None
    document_url: str | None
    notes: str | None
    created_at: datetime


class NoteOut(ORM):
    id: uuid.UUID
    body: str
    admin_user_id: uuid.UUID | None
    created_at: datetime


class EventOut(ORM):
    id: uuid.UUID
    type: str
    actor: str
    admin_user_id: uuid.UUID | None
    data: dict[str, Any]
    created_at: datetime


class LeadFields(ORM):
    id: uuid.UUID
    type: str
    status: str
    temperature: str | None
    source_channel: str
    referred_by_client_id: uuid.UUID | None
    tags: list[str]
    message: str | None
    budget_range: str | None
    estimated_value: Decimal | None
    currency: str | None
    lost_reason: str | None
    lost_comment: str | None
    visitor_country: str | None
    converted_at: datetime | None
    first_contacted_at: datetime | None
    last_contacted_at: datetime | None
    next_follow_up_at: datetime | None
    created_at: datetime
    updated_at: datetime


class LeadListItem(LeadFields):
    contact: ContactOut
    next_booking_at: datetime | None = None


class TemperatureCounts(BaseModel):
    hot: int = 0
    warm: int = 0
    cold: int = 0
    unrated: int = 0


class LeadPage(BaseModel):
    items: list[LeadListItem]
    next_cursor: str | None
    # Leads per temperature for the same filters, ignoring the temperature filter itself (for the
    # Hot / Warm / Cold pills). First page only; null when a cursor is passed.
    counts: TemperatureCounts | None = None


class LeadDetail(LeadFields):
    # What the visitor typed; can differ from the contact's stored details
    submitted_name: str | None
    submitted_phone: str | None
    submitted_company: str | None
    utm_source: str | None
    utm_medium: str | None
    utm_campaign: str | None
    utm_term: str | None
    utm_content: str | None
    referrer: str | None
    landing_page: str | None
    consent_at: datetime | None
    consent_version: str | None
    contact: ContactOut
    client_id: uuid.UUID | None = None
    bookings: list[BookingOut]
    proposals: list[ProposalOut]
    notes: list[NoteOut]
    events: list[EventOut]


# ---------------------------------------------------------------------------
# Admin: leads
# ---------------------------------------------------------------------------

class ManualLeadCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    email: EmailStr | None = None
    phone_e164: Phone | None = None
    phone_country: CountryCode | None = None
    company: str | None = None
    source_channel: SourceChannel
    referred_by_client_id: uuid.UUID | None = None
    message: str | None = Field(None, max_length=2000)
    temperature: Temperature | None = None
    tags: list[str] = []
    estimated_value: Decimal | None = Field(None, ge=0)
    currency: Currency | None = None
    next_follow_up_at: datetime | None = None


class ManualLeadCreated(BaseModel):
    lead: LeadDetail
    # Existing contacts that share the email or phone: the app shows "this person already exists"
    warnings: list[str]


class LeadUpdate(BaseModel):
    status: LeadStatus | None = None
    temperature: Temperature | None = None
    tags: list[str] | None = None
    estimated_value: Decimal | None = Field(None, ge=0)
    currency: Currency | None = None
    lost_reason: LostReason | None = None
    lost_comment: str | None = None
    referred_by_client_id: uuid.UUID | None = None
    next_follow_up_at: datetime | None = None


class ContactMatch(BaseModel):
    """An existing contact that shares an email or phone number with what's being typed."""
    contact: ContactOut
    matched_on: list[Literal["email", "phone"]]
    client_id: uuid.UUID | None
    open_leads: int
    last_lead_at: datetime | None


class ContactUpdate(BaseModel):
    name: str | None = Field(None, max_length=100)
    email: EmailStr | None = None
    phone_e164: Phone | None = None
    phone_country: CountryCode | None = None
    company: str | None = None
    timezone: str | None = None


class NoteCreate(BaseModel):
    body: str = Field(min_length=1, max_length=5000)


class ContactedRequest(BaseModel):
    channel: Literal["call", "whatsapp", "email", "other"] = "other"


class ConvertRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    value: Decimal | None = Field(None, ge=0)
    currency: Currency | None = None
    company: str | None = None
    industry: str | None = None
    started_on: date | None = None


class ConvertResult(BaseModel):
    client_id: uuid.UUID
    project_id: uuid.UUID
    client_created: bool


# ---------------------------------------------------------------------------
# Admin: proposals
# ---------------------------------------------------------------------------

class ProposalCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    amount: Decimal = Field(ge=0)
    currency: Currency
    status: Literal["draft", "sent"] = "draft"
    valid_until: date | None = None
    document_url: str | None = None
    notes: str | None = None


class ProposalUpdate(BaseModel):
    status: ProposalStatus | None = None
    valid_until: date | None = None
    document_url: str | None = None
    notes: str | None = None


# ---------------------------------------------------------------------------
# Admin: clients, projects, payments
# ---------------------------------------------------------------------------

class ClientSummaryOut(ORM):
    client_id: uuid.UUID
    name: str
    email: str | None
    phone_e164: str | None
    company: str | None
    country: str | None
    converted_at: datetime
    tags: list[str]
    project_count: int
    active_projects: int
    last_project_at: datetime | None
    contracted_value: dict[str, Decimal]
    total_paid: dict[str, Decimal]
    outstanding: dict[str, Decimal]
    overdue: dict[str, Decimal]
    referrals_sent: int


class PaymentOut(ORM):
    id: uuid.UUID
    project_id: uuid.UUID
    description: str
    amount: Decimal
    currency: str
    status: str
    due_on: date | None
    paid_on: date | None
    method: str | None
    invoice_url: str | None
    reference: str | None
    created_at: datetime


class ProjectOut(ORM):
    id: uuid.UUID
    client_id: uuid.UUID
    lead_id: uuid.UUID | None
    title: str
    description: str | None
    status: str
    value: Decimal | None
    currency: str | None
    started_on: date | None
    completed_on: date | None
    live_url: str | None
    created_at: datetime
    payments: list[PaymentOut] = []


class ClientLeadOut(LeadFields):
    events: list[EventOut]


class ClientDetail(ORM):
    id: uuid.UUID
    company: str | None
    industry: str | None
    country: str | None
    converted_at: datetime
    testimonial: str | None
    testimonial_author: str | None
    testimonial_approved: bool
    show_on_portfolio: bool
    tags: list[str]
    notes: str | None
    first_lead_id: uuid.UUID | None
    contact: ContactOut
    projects: list[ProjectOut]
    leads: list[ClientLeadOut] = []


class ClientUpdate(BaseModel):
    company: str | None = None
    industry: str | None = None
    country: CountryCode | None = None
    testimonial: str | None = None
    testimonial_author: str | None = None
    testimonial_approved: bool | None = None
    show_on_portfolio: bool | None = None
    tags: list[str] | None = None
    notes: str | None = None


class ProjectCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str | None = None
    status: ProjectStatus = "active"
    value: Decimal | None = Field(None, ge=0)
    currency: Currency | None = None
    started_on: date | None = None
    live_url: str | None = None


class ProjectUpdate(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = None
    status: ProjectStatus | None = None
    value: Decimal | None = Field(None, ge=0)
    currency: Currency | None = None
    started_on: date | None = None
    completed_on: date | None = None
    live_url: str | None = None


class PaymentCreate(BaseModel):
    description: str = Field(min_length=1, max_length=200)
    amount: Decimal = Field(gt=0)
    currency: Currency
    due_on: date | None = None
    invoice_url: str | None = None
    reference: str | None = None


class PaymentUpdate(BaseModel):
    status: PaymentStatus | None = None
    description: str | None = Field(None, min_length=1, max_length=200)
    amount: Decimal | None = Field(None, gt=0)
    due_on: date | None = None
    paid_on: date | None = None
    method: str | None = None
    invoice_url: str | None = None
    reference: str | None = None


class PaymentListItem(PaymentOut):
    project_title: str
    client_id: uuid.UUID
    client_name: str
    is_overdue: bool


# ---------------------------------------------------------------------------
# Admin: today
# ---------------------------------------------------------------------------

class TodayCall(BaseModel):
    booking: BookingOut
    lead_id: uuid.UUID
    contact: ContactOut


class TodayProposal(ProposalOut):
    contact: ContactOut
    is_expired: bool  # valid_until has passed but it's still marked sent


class TodayCounts(BaseModel):
    new_leads: int
    hot_leads: int
    follow_ups_due: int
    calls_today: int
    overdue: dict[str, Decimal]  # per currency


class TodayOut(BaseModel):
    """Everything the app's Today screen needs, in one call. Dates are in the business timezone (IST)."""
    date: date
    timezone: str
    counts: TodayCounts
    new_leads: list[LeadListItem]  # status new and never contacted, oldest first
    follow_ups_due: list[LeadListItem]  # open leads with a follow-up due by the end of today, earliest first
    calls: list[TodayCall]  # confirmed calls today and tomorrow, soonest first
    payments_overdue: list[PaymentListItem]
    payments_due_soon: list[PaymentListItem]  # due in the next 7 days
    proposals_expiring: list[TodayProposal]  # sent, valid until within 7 days (or already lapsed)


# ---------------------------------------------------------------------------
# Admin: misc
# ---------------------------------------------------------------------------

class DeviceRegister(BaseModel):
    fcm_token: str = Field(min_length=1, max_length=4096)
    platform: Literal["android", "ios", "web"] = "android"


class Stats(BaseModel):
    period: dict[str, datetime | None]
    total_leads: int
    leads_by_status: dict[str, int]
    leads_by_channel: dict[str, int]
    leads_by_source: dict[str, int]
    leads_by_country: dict[str, int]
    won: int
    conversion_rate: float | None
    avg_response_seconds: float | None
    lost_reasons: dict[str, int]
    proposals: dict[str, int]
    proposal_win_rate: float | None
    money_received: dict[str, Decimal]
    money_outstanding: dict[str, Decimal]
    top_referrers: list[dict[str, Any]]
