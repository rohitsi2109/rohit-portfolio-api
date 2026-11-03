-- =============================================================================
-- rohit-portfolio-api : Neon Postgres schema (v1, for review)
-- Target: Postgres 17 on Neon, region AWS ap-southeast-2 (Sydney) or ap-southeast-1 (Singapore)
--
-- Conventions
--   * UUID primary keys (gen_random_uuid() is built into Postgres 13+)
--   * All timestamps are timestamptz, stored in UTC
--   * "Enum" columns are text + CHECK constraints: adding a value later is a
--     one-line migration, unlike Postgres ENUM types
--   * updated_at is maintained by a trigger
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS citext;   -- case-insensitive email


-- -----------------------------------------------------------------------------
-- Shared trigger: keep updated_at current
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;


-- =============================================================================
-- CONTACTS: one row per person (deduplicated by email)
-- A person who books twice, or books and later sends an enquiry, is one contact
-- with several leads. Email is optional for leads you add by hand (e.g. a
-- WhatsApp enquiry), but every contact needs an email or a phone.
-- =============================================================================
CREATE TABLE contacts (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    email           citext      UNIQUE,              -- multiple NULLs allowed
    name            text        NOT NULL CHECK (char_length(name) BETWEEN 1 AND 100),
    phone_e164      text        CHECK (phone_e164 ~ '^\+[1-9][0-9]{6,14}$'),
    phone_country   char(2),                         -- ISO 3166-1 alpha-2 picked in the form, e.g. 'AU'
    company         text,                            -- optional, filled from enquiry form or by admin
    timezone        text,                            -- IANA zone from the booking form, e.g. 'Australia/Sydney'
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT contacts_reachable CHECK (email IS NOT NULL OR phone_e164 IS NOT NULL)
);

-- Not unique (families / shared office lines), but lets the app warn "this number already exists"
CREATE INDEX contacts_phone_idx ON contacts (phone_e164) WHERE phone_e164 IS NOT NULL;

CREATE TRIGGER contacts_updated_at BEFORE UPDATE ON contacts
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();


-- =============================================================================
-- LEADS: one row per submission / sales opportunity
-- This is what the admin app lists and moves through the pipeline.
-- =============================================================================
CREATE TABLE leads (
    id                  uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    contact_id          uuid        NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,

    -- booking / enquiry come from the website; manual = added by you in the app
    type                text        NOT NULL CHECK (type IN ('booking', 'enquiry', 'manual')),
    -- Where the lead reached you. Website leads are 'website'; you pick it for manual ones.
    source_channel      text        NOT NULL DEFAULT 'website'
                        CHECK (source_channel IN ('website', 'linkedin', 'whatsapp', 'referral', 'upwork',
                                                  'email', 'event', 'other')),
    -- The client who sent this lead to you (FK added after the clients table, below)
    referred_by_client_id uuid,
    tags                text[]      NOT NULL DEFAULT '{}',   -- free labels, e.g. {ecommerce,urgent}
    status              text        NOT NULL DEFAULT 'new'
                        CHECK (status IN ('new', 'contacted', 'qualified', 'proposal_sent', 'won', 'lost', 'spam')),
    -- How promising the lead is, set by you (independent of pipeline status).
    -- NULL = not rated yet. Setting status to 'won' converts it (see clients / projects).
    temperature         text        CHECK (temperature IN ('hot', 'warm', 'cold')),
    converted_at        timestamptz,                 -- set when status becomes 'won'

    -- Exactly what was typed on the website for this lead. The website never overwrites an
    -- existing contact's details (anyone can type anyone's email), so they're kept here.
    submitted_name      text,
    submitted_phone     text,
    submitted_company   text,

    message             text        CHECK (char_length(message) <= 2000),  -- "What would you like to build?"
    budget_range        text,                        -- enquiry form (phase 4), e.g. 'AUD 2k-5k'
    estimated_value     numeric(12,2),               -- set by admin when qualifying
    currency            char(3),                     -- 'AUD' | 'SGD' | 'USD' ...
    -- Fixed list so lost deals can be counted; details go in lost_comment
    lost_reason         text        CHECK (lost_reason IN ('price', 'timing', 'chose_competitor', 'no_response',
                                                           'not_a_fit', 'other')),
    lost_comment        text,

    -- Attribution: where the visitor came from
    utm_source          text,
    utm_medium          text,
    utm_campaign        text,
    utm_term            text,
    utm_content         text,
    referrer            text,
    landing_page        text,
    visitor_country     char(2),                     -- from Vercel geo header, NOT from a stored IP

    -- Consent (Privacy Act / PDPA): what the visitor agreed to, and when.
    -- Required for website leads; manual leads gave their details to you directly.
    consent_at          timestamptz,
    consent_version     text,                        -- e.g. 'privacy-2026-11', bump when the policy text changes

    -- Follow-up tracking (used by the admin app)
    first_contacted_at  timestamptz,                 -- response time = first_contacted_at - created_at
    last_contacted_at   timestamptz,
    next_follow_up_at   timestamptz,

    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT leads_website_consent CHECK (
        type = 'manual' OR (consent_at IS NOT NULL AND consent_version IS NOT NULL)),
    CONSTRAINT leads_lost_reason_only_when_lost CHECK (
        lost_reason IS NULL OR status = 'lost')
);

CREATE INDEX leads_created_at_idx   ON leads (created_at DESC);
CREATE INDEX leads_status_idx       ON leads (status, created_at DESC);
CREATE INDEX leads_contact_idx      ON leads (contact_id);
CREATE INDEX leads_follow_up_idx    ON leads (next_follow_up_at) WHERE next_follow_up_at IS NOT NULL;
CREATE INDEX leads_source_idx       ON leads (source_channel, utm_source);
CREATE INDEX leads_tags_idx         ON leads USING gin (tags);
CREATE INDEX leads_temperature_idx  ON leads (temperature, created_at DESC)
    WHERE status NOT IN ('won', 'lost', 'spam');    -- the active pipeline view

CREATE TRIGGER leads_updated_at BEFORE UPDATE ON leads
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();


-- =============================================================================
-- BOOKINGS: Cal.com meetings attached to a lead
-- A failed attempt is stored too (status 'failed'), so no lead is ever lost.
-- A reschedule creates a new Cal.com uid; we link it to the previous row.
-- =============================================================================
CREATE TABLE bookings (
    id                      uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    lead_id                 uuid        NOT NULL REFERENCES leads(id) ON DELETE CASCADE,

    cal_booking_uid         text        UNIQUE,      -- NULL when the Cal.com call failed
    rescheduled_from_id     uuid        REFERENCES bookings(id) ON DELETE SET NULL,

    status                  text        NOT NULL
                            CHECK (status IN ('confirmed', 'cancelled', 'rescheduled', 'completed', 'no_show', 'failed')),
    start_at                timestamptz NOT NULL,
    end_at                  timestamptz,
    attendee_timezone       text        NOT NULL,
    meeting_url             text,
    cancellation_reason     text,
    error_message           text,                    -- Cal.com error when status = 'failed'

    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX bookings_lead_idx      ON bookings (lead_id);
CREATE INDEX bookings_start_idx     ON bookings (start_at) WHERE status = 'confirmed';

CREATE TRIGGER bookings_updated_at BEFORE UPDATE ON bookings
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();


-- =============================================================================
-- PROPOSALS: what you quoted a lead, and what happened to it
-- Several per lead are allowed (a revised quote is a new row), giving win rate
-- by price over time.
-- =============================================================================
CREATE TABLE proposals (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    lead_id         uuid        NOT NULL REFERENCES leads(id) ON DELETE CASCADE,

    title           text        NOT NULL,            -- e.g. 'Website + admin panel, fixed price'
    amount          numeric(12,2) NOT NULL CHECK (amount >= 0),
    currency        char(3)     NOT NULL,
    status          text        NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft', 'sent', 'accepted', 'declined', 'expired')),
    sent_at         timestamptz,
    valid_until     date,
    responded_at    timestamptz,                     -- when accepted / declined
    document_url    text,                            -- link to the PDF / Google Doc
    notes           text,

    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT proposals_sent_has_date CHECK (status = 'draft' OR sent_at IS NOT NULL)
);

CREATE INDEX proposals_lead_idx ON proposals (lead_id, created_at DESC);
-- Proposals awaiting an answer, for "chase this quote" reminders
CREATE INDEX proposals_open_idx ON proposals (valid_until) WHERE status = 'sent';

CREATE TRIGGER proposals_updated_at BEFORE UPDATE ON proposals
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();


-- =============================================================================
-- CLIENTS: converted leads, kept as a permanent record
-- Created automatically (in the same transaction) when a lead's status is set
-- to 'won'. One row per person; a returning client gets another project, not
-- another client row.
-- =============================================================================
CREATE TABLE clients (
    id                      uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    -- RESTRICT: a paying client can't be erased by accident through the
    -- contact-deletion endpoint; erasing a client is a deliberate, separate step.
    contact_id              uuid        NOT NULL UNIQUE REFERENCES contacts(id) ON DELETE RESTRICT,
    first_lead_id           uuid        REFERENCES leads(id) ON DELETE SET NULL,  -- the lead that converted first

    company                 text,
    industry                text,                    -- e.g. 'Interior Design', 'Fitness'
    country                 char(2),
    converted_at            timestamptz NOT NULL DEFAULT now(),

    -- Portfolio follow-through: social proof for future leads
    testimonial             text,
    testimonial_author      text,                    -- e.g. 'Priya S., Founder, Royal Touchup'
    testimonial_approved    boolean     NOT NULL DEFAULT false,  -- written permission to publish
    show_on_portfolio       boolean     NOT NULL DEFAULT false,

    tags                    text[]      NOT NULL DEFAULT '{}',
    notes                   text,
    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX clients_converted_idx ON clients (converted_at DESC);
CREATE INDEX clients_tags_idx      ON clients USING gin (tags);

-- leads <-> clients reference each other, so the referral FK is added here
ALTER TABLE leads
    ADD CONSTRAINT leads_referred_by_client_fk
    FOREIGN KEY (referred_by_client_id) REFERENCES clients(id) ON DELETE SET NULL;
CREATE INDEX leads_referred_by_idx ON leads (referred_by_client_id) WHERE referred_by_client_id IS NOT NULL;

CREATE TRIGGER clients_updated_at BEFORE UPDATE ON clients
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Each piece of paid work for a client. A won lead creates one project;
-- repeat work from the same client adds more.
CREATE TABLE projects (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id       uuid        NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    lead_id         uuid        UNIQUE REFERENCES leads(id) ON DELETE SET NULL,  -- NULL for work added by hand

    title           text        NOT NULL,            -- e.g. 'Business website + admin panel'
    description     text,
    status          text        NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'on_hold', 'completed', 'cancelled')),
    value           numeric(12,2),                   -- agreed price
    currency        char(3),
    started_on      date,
    completed_on    date,
    live_url        text,                            -- link for the portfolio once shipped

    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX projects_client_idx ON projects (client_id, created_at DESC);
CREATE INDEX projects_status_idx ON projects (status);

CREATE TRIGGER projects_updated_at BEFORE UPDATE ON projects
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Money in: deposits, milestones, final payments for a project.
-- "Overdue" is not stored; it's computed (status 'pending' and due_on < today),
-- so it can never go stale. "Today" is the date in India (Asia/Kolkata), not UTC,
-- so payments turn overdue at midnight IST.
CREATE TABLE payments (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id      uuid        NOT NULL REFERENCES projects(id) ON DELETE CASCADE,

    description     text        NOT NULL,            -- e.g. '50% deposit', 'Milestone 2: admin panel'
    amount          numeric(12,2) NOT NULL CHECK (amount > 0),
    currency        char(3)     NOT NULL,
    status          text        NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'paid', 'cancelled')),
    due_on          date,
    paid_on         date,
    method          text,                            -- e.g. 'bank transfer', 'Wise', 'PayPal'
    invoice_url     text,                            -- link to the invoice in your invoicing tool
    reference       text,                            -- invoice number / transaction id

    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT payments_paid_has_date CHECK (status <> 'paid' OR paid_on IS NOT NULL)
);

CREATE INDEX payments_project_idx ON payments (project_id);
CREATE INDEX payments_due_idx     ON payments (due_on) WHERE status = 'pending';

CREATE TRIGGER payments_updated_at BEFORE UPDATE ON payments
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- One row per client for the Clients screen. Money is summed per currency,
-- since AUD and SGD can't be added together:
--   contracted_value = agreed project prices (excluding cancelled projects)
--   total_paid       = payments received
--   outstanding      = pending payments; overdue = pending and past due
CREATE VIEW client_summary AS
SELECT
    cl.id                                                   AS client_id,
    co.name,
    co.email,
    co.phone_e164,
    cl.company,
    cl.country,
    cl.converted_at,
    cl.tags,
    pc.project_count,
    pc.active_projects,
    pc.last_project_at,
    lv.contracted_value,                                    -- e.g. {"AUD": 8500, "SGD": 3000}
    pay.total_paid,
    pay.outstanding,
    pay.overdue,
    (SELECT count(*) FROM leads r WHERE r.referred_by_client_id = cl.id) AS referrals_sent
FROM clients cl
JOIN contacts co ON co.id = cl.contact_id
CROSS JOIN LATERAL (
    SELECT count(*)                                     AS project_count,
           count(*) FILTER (WHERE p.status = 'active') AS active_projects,
           max(p.created_at)                           AS last_project_at
    FROM projects p
    WHERE p.client_id = cl.id
) pc
CROSS JOIN LATERAL (
    SELECT coalesce(jsonb_object_agg(t.currency, t.total), '{}'::jsonb) AS contracted_value
    FROM (
        SELECT p.currency, sum(p.value) AS total
        FROM projects p
        WHERE p.client_id = cl.id AND p.status <> 'cancelled'
          AND p.value IS NOT NULL AND p.currency IS NOT NULL
        GROUP BY p.currency
    ) t
) lv
CROSS JOIN LATERAL (
    SELECT
        coalesce(jsonb_object_agg(t.currency, t.paid)        FILTER (WHERE t.paid > 0),        '{}'::jsonb) AS total_paid,
        coalesce(jsonb_object_agg(t.currency, t.outstanding) FILTER (WHERE t.outstanding > 0), '{}'::jsonb) AS outstanding,
        coalesce(jsonb_object_agg(t.currency, t.overdue)     FILTER (WHERE t.overdue > 0),     '{}'::jsonb) AS overdue
    FROM (
        SELECT pm.currency,
               coalesce(sum(pm.amount) FILTER (WHERE pm.status = 'paid'), 0)    AS paid,
               coalesce(sum(pm.amount) FILTER (WHERE pm.status = 'pending'), 0) AS outstanding,
               coalesce(sum(pm.amount) FILTER (WHERE pm.status = 'pending'
                                                 AND pm.due_on < (now() AT TIME ZONE 'Asia/Kolkata')::date), 0) AS overdue
        FROM payments pm
        JOIN projects p ON p.id = pm.project_id
        WHERE p.client_id = cl.id
        GROUP BY pm.currency
    ) t
) pay;


-- =============================================================================
-- ADMIN USERS + AUTH
-- Only you to start with, but a table (not a hard-coded password) makes it
-- possible to add a collaborator later.
-- =============================================================================
CREATE TABLE admin_users (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    email           citext      NOT NULL UNIQUE,
    name            text        NOT NULL,
    password_hash   text        NOT NULL,            -- argon2id
    is_active       boolean     NOT NULL DEFAULT true,
    last_login_at   timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TRIGGER admin_users_updated_at BEFORE UPDATE ON admin_users
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Long-lived refresh tokens for the Flutter app (short-lived JWT access tokens are not stored).
-- Stored hashed, so a database leak does not leak usable tokens. Revoke = log a lost phone out.
CREATE TABLE refresh_tokens (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    admin_user_id   uuid        NOT NULL REFERENCES admin_users(id) ON DELETE CASCADE,
    token_hash      text        NOT NULL UNIQUE,     -- sha256 of the token
    device_label    text,                            -- e.g. 'Pixel 8'
    expires_at      timestamptz NOT NULL,
    revoked_at      timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX refresh_tokens_user_idx ON refresh_tokens (admin_user_id) WHERE revoked_at IS NULL;

-- Firebase Cloud Messaging tokens for push notifications on new leads
CREATE TABLE push_devices (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    admin_user_id   uuid        NOT NULL REFERENCES admin_users(id) ON DELETE CASCADE,
    fcm_token       text        NOT NULL UNIQUE,
    platform        text        NOT NULL DEFAULT 'android' CHECK (platform IN ('android', 'ios', 'web')),
    created_at      timestamptz NOT NULL DEFAULT now(),
    last_seen_at    timestamptz NOT NULL DEFAULT now()
);


-- =============================================================================
-- NOTES + ACTIVITY TIMELINE (shown on the lead detail screen)
-- =============================================================================
CREATE TABLE lead_notes (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    lead_id         uuid        NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    admin_user_id   uuid        REFERENCES admin_users(id) ON DELETE SET NULL,
    body            text        NOT NULL CHECK (char_length(body) BETWEEN 1 AND 5000),
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX lead_notes_lead_idx ON lead_notes (lead_id, created_at DESC);

CREATE TRIGGER lead_notes_updated_at BEFORE UPDATE ON lead_notes
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Append-only history: who did what, when. Never updated.
CREATE TABLE lead_events (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    lead_id         uuid        NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    type            text        NOT NULL CHECK (type IN (
                        'lead_created', 'status_changed', 'temperature_changed', 'converted',
                        'note_added', 'contacted', 'tags_changed',
                        'proposal_sent', 'proposal_accepted', 'proposal_declined', 'proposal_expired',
                        'booking_confirmed', 'booking_failed', 'booking_cancelled',
                        'booking_rescheduled', 'booking_completed', 'booking_no_show',
                        'follow_up_set')),
    actor           text        NOT NULL CHECK (actor IN ('system', 'admin', 'cal_webhook')),
    admin_user_id   uuid        REFERENCES admin_users(id) ON DELETE SET NULL,
    data            jsonb       NOT NULL DEFAULT '{}'::jsonb,  -- e.g. {"from": "new", "to": "contacted"}
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX lead_events_lead_idx ON lead_events (lead_id, created_at DESC);


-- =============================================================================
-- WEBHOOK LOG: every Cal.com webhook received
-- Gives idempotency (the same delivery is processed once) and a debugging trail.
-- =============================================================================
CREATE TABLE webhook_events (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    provider        text        NOT NULL DEFAULT 'cal',
    event_type      text        NOT NULL,            -- BOOKING_CREATED, BOOKING_CANCELLED, ...
    dedupe_key      text        NOT NULL,            -- event_type + booking uid + createdAt
    payload         jsonb       NOT NULL,
    received_at     timestamptz NOT NULL DEFAULT now(),
    processed_at    timestamptz,
    error           text,
    UNIQUE (provider, dedupe_key)
);

CREATE INDEX webhook_events_unprocessed_idx ON webhook_events (received_at) WHERE processed_at IS NULL;


-- =============================================================================
-- RATE LIMITS: request counters per fixed time window (login, booking, enquiry)
-- The key is a keyed hash, never a raw IP address or email. Rows older than a
-- day are deleted by the API as it goes.
-- =============================================================================
CREATE TABLE rate_limits (
    key             text        NOT NULL,            -- sha256(secret, scope, ip-or-email)
    window_start    timestamptz NOT NULL,
    hits            integer     NOT NULL DEFAULT 1,
    PRIMARY KEY (key, window_start)
);

CREATE INDEX rate_limits_window_idx ON rate_limits (window_start);
