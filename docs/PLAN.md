# rohit-portfolio-api: Plan (v1, for review)

The backend for rohitsingh.in. It owns lead capture, the Cal.com integration and the
admin API used by the Flutter Android app.

```
Website (React, Vercel) ──► rohit-portfolio-api (FastAPI, Vercel) ──► Neon Postgres
                                 │   ▲
                                 │   └── Cal.com webhooks (cancelled / rescheduled / ...)
                                 ├──► Cal.com API v2 (slots + bookings)
                                 └──► Firebase Cloud Messaging ──► Flutter admin app
                                                                        │
                            Flutter admin app ◄── /v1/admin/* (JWT) ────┘
```

## 1. Stack

| Concern | Choice | Why |
|---|---|---|
| API | FastAPI (Python 3.12) | Async, typed, auto-generated OpenAPI docs that the Flutter client can be generated from |
| ORM / migrations | SQLAlchemy 2 + Alembic | Mature; migrations reviewed as code |
| DB driver | psycopg 3 | Native Postgres; works with Neon's pooled endpoint |
| Database | Neon Postgres 17, AWS **ap-southeast-2 (Sydney)** or **ap-southeast-1 (Singapore)** | Data stays in the region the FAQ promises |
| Hosting | Vercel (Python serverless), separate project on `api.rohitsingh.in` | Free, no sleeping, same platform as the website |
| Auth | Email + password (argon2id) → JWT access token (15 min) + refresh token (30 days, stored hashed) | Only the API can reach the DB; a lost phone can be revoked |
| Push | Firebase Cloud Messaging (free) | New lead → notification on your phone |
| Validation | Pydantic v2 | Shared request/response models |

**Serverless + Neon:** use the **pooled** connection string (`-pooler` host) and SQLAlchemy `NullPool`,
so each function invocation doesn't hold connections open. Neon's compute sleeping after
~5 min idle only adds ~0.5–1 s to the first request; data is never affected.

## 2. Database

Full DDL: [`db/schema.sql`](../db/schema.sql). Summary:

| Table | Purpose |
|---|---|
| `contacts` | One row per person, unique by email (case-insensitive). Email is optional for manual leads, but every contact needs an email or a phone |
| `leads` | One row per opportunity: type (`booking` / `enquiry` / `manual`), source channel, referring client, tags, pipeline status, temperature, message, lost reason (fixed list + comment), attribution (UTM, referrer, landing page, visitor country), consent, first-contacted / last-contacted / follow-up dates |
| `bookings` | Cal.com meetings for a lead, **including failed attempts**; reschedules are linked |
| `proposals` | Quotes sent to a lead: amount + currency, draft / sent / accepted / declined / expired, valid-until date, document link. A revised quote is a new row |
| `clients` | **Converted leads**, a permanent record: company, industry, country, converted date, tags, testimonial (+ permission to publish), show-on-portfolio flag |
| `projects` | Each piece of paid work for a client: title, value + currency, status (active / on hold / completed / cancelled), dates, live URL |
| `payments` | Deposits and milestones per project: amount + currency, due date, paid date, method, invoice link. "Overdue" is computed, never stored |
| `client_summary` (view) | One row per client: project counts, contracted value, total paid, outstanding and overdue (all per currency), referrals sent |
| `lead_notes` | Your private notes on a lead |
| `lead_events` | Append-only timeline (created, status changes, booking changes, notes) |
| `admin_users` / `refresh_tokens` / `push_devices` | Admin app login and push notifications |
| `webhook_events` | Raw Cal.com webhooks, de-duplicated, for idempotency and debugging |

**Pipeline statuses:** `new → contacted → qualified → proposal_sent → won | lost`, plus `spam`.

**Source channel:** `website | linkedin | whatsapp | referral | upwork | email | event | other`.
Website leads are tagged automatically; you pick it for leads you add by hand in the app.

**Lost reasons:** `price | timing | chose_competitor | no_response | not_a_fit | other`, plus an optional comment.

**Response time:** `first_contacted_at - created_at`, set the first time you mark a lead as contacted.

**Temperature:** `hot | warm | cold` (or unrated), set by you on any open lead, independent of status.
The default lead list shows only **open** leads (not won, lost or spam), sorted hot → warm → cold → unrated, then newest.

**Conversion: what "won" does.** Setting a lead to `won` runs in one transaction:
1. Sets `leads.converted_at`.
2. Finds or creates the `client` for that contact (a returning client is not duplicated).
3. Creates a `project` linked to the lead, with the title and value you enter in the app.
4. Logs a `converted` event.

The lead then leaves the open-leads list and appears under **Clients**, with all its history intact.
Setting `won` back to another status is allowed (to fix a mistake), but it doesn't delete the client
or project; those are removed by hand.

**Privacy decisions:**
- Data is stored **only when the visitor presses Submit**. Nothing is captured while they type.
- No IP addresses are stored. Only the country, from Vercel's geo header.
- Every lead records `consent_at` and `consent_version` (which privacy policy text they saw).
- Deleting a contact cascades to their leads, bookings, notes and events (a right-to-erasure request).
  It is **blocked** for anyone who has become a client, because business records (projects, values) need a deliberate decision before erasure.

## 3. API

### Public (called by the website, CORS: `rohitsingh.in`, `localhost:5179`)

| Method | Path | Notes |
|---|---|---|
| GET | `/v1/slots?start&end&timeZone` | Proxies Cal.com, same response shape as today's `/api/slots` |
| POST | `/v1/bookings` | Body: name, email, phone (E.164), phoneCountry, timeZone, start, notes, attribution, honeypot |
| POST | `/v1/enquiries` | Phase 4: "message without a call" form |
| POST | `/v1/webhooks/cal` | Verifies the `X-Cal-Signature-256` HMAC; stores, then processes |
| GET | `/health` | Uptime check; also wakes Neon |

**`POST /v1/bookings` flow:**
1. Validate the input. If the hidden honeypot field is filled, return a fake success and save nothing.
2. Find or create the `contact` by email. An existing contact's details are **never overwritten**
   (anyone can type anyone's email); only empty fields are filled in. What was typed is stored on
   the lead (`submitted_name`, `submitted_phone`, `submitted_company`).
3. Insert the `lead` (`type=booking`, `status=new`) and a `lead_created` event, and **commit**
   (so a `BOOKING_CREATED` webhook that races ahead of this request always finds the lead).
4. Call Cal.com to create the booking, passing the lead id, consent and attribution as booking metadata.
5. Insert the `booking` row: `confirmed` with the uid, or `failed` with the error. Add an event. Commit.
6. Send a push notification. A push failure never fails the request.
7. Return the booking result (or the Cal.com error) to the website.

**If the database is down:** still book with Cal.com (the client's meeting matters most) and log
the error. The `BOOKING_CREATED` webhook then backfills the lead once the database is reachable,
because webhooks upsert by `cal_booking_uid`.

### Admin (called by the Flutter app, `Authorization: Bearer <jwt>`)

| Method | Path | Notes |
|---|---|---|
| POST | `/v1/auth/login` | email + password → access + refresh tokens |
| POST | `/v1/auth/refresh` | Rotates the refresh token |
| POST | `/v1/auth/logout` | Revokes the refresh token |
| GET | `/v1/admin/leads` | Default: open leads only. Filters: `status`, `temperature`, `type`, `channel`, `source`, `tag`, `country`, `referred_by`, `q` (name/email/phone), `from`, `to`, `follow_up_due`; cursor pagination |
| POST | `/v1/admin/leads` | Add a manual lead (WhatsApp, LinkedIn, referral...). Warns if the email or phone matches an existing contact |
| GET | `/v1/admin/leads/{id}` | Lead + contact + bookings + proposals + notes + events |
| PATCH | `/v1/admin/leads/{id}` | status, temperature, tags, estimated_value, currency, lost_reason + lost_comment, referred_by_client_id, next_follow_up_at |
| POST | `/v1/admin/leads/{id}/proposals` | Record a quote |
| PATCH | `/v1/admin/proposals/{id}` | Mark sent / accepted / declined / expired |
| POST | `/v1/admin/leads/{id}/convert` | Marks as won and creates the client + project in one step. Body: project title, value, currency, company |
| GET | `/v1/admin/clients` | Converted leads (from `client_summary`), newest first; filter by `country`, `q` |
| GET | `/v1/admin/clients/{id}` | Client + contact + projects + the original lead history |
| PATCH | `/v1/admin/clients/{id}` | company, industry, testimonial, testimonial_approved, show_on_portfolio, notes |
| POST | `/v1/admin/clients/{id}/projects` | Add repeat work for an existing client |
| PATCH | `/v1/admin/projects/{id}` | status, value, dates, live_url |
| POST | `/v1/admin/projects/{id}/payments` | Add a deposit / milestone |
| PATCH | `/v1/admin/payments/{id}` | Mark paid (date, method, reference) or cancelled |
| GET | `/v1/admin/payments?state=overdue\|due_soon\|pending` | Money to chase, across all clients |
| POST | `/v1/admin/leads/{id}/notes` | Add a note |
| POST | `/v1/admin/leads/{id}/contacted` | Sets `last_contacted_at` and logs an event (tapped call / WhatsApp / email in the app) |
| GET | `/v1/admin/stats?from&to` | Leads by status, channel, source and country; conversion rate; average response time; lost reasons; proposal win rate; money received / outstanding per currency; top referring clients |
| POST | `/v1/admin/devices` | Register an FCM token |
| PATCH | `/v1/admin/contacts/{id}` | Correct name, email, phone, company, timezone (the website never changes an existing contact) |
| DELETE | `/v1/admin/contacts/{id}` | Privacy erasure |

The first admin user is created with a one-off CLI command (`python -m app.cli create-admin`).
Use an email that is **not** published on the website (not connect@rohitsingh.in): the per-account
login limit means anyone who knows the admin email can lock it out for an hour.
There is no public sign-up endpoint.

## 4. Repository layout

```
rohit-portfolio-api/
├── api/index.py              # Vercel entry point: exposes the FastAPI app
├── app/
│   ├── main.py               # app factory, CORS, routers
│   ├── config.py             # settings from env vars
│   ├── db.py                 # engine (NullPool), session dependency
│   ├── models.py             # SQLAlchemy models (mirror db/schema.sql)
│   ├── schemas.py            # Pydantic request/response models
│   ├── routers/              # public.py, admin.py, auth.py, webhooks.py
│   ├── services/             # cal.py, leads.py, push.py, security.py
│   └── cli.py                # create-admin
├── migrations/               # Alembic; 0001 = db/schema.sql
├── tests/                    # pytest: validation, booking flow (Cal mocked), auth, webhooks
├── db/schema.sql             # human-readable schema (this review)
├── docs/PLAN.md
├── requirements.txt
├── vercel.json               # route everything to api/index.py
└── .env.example
```

### Environment variables

| Name | Example / note |
|---|---|
| `DATABASE_URL` | Neon **pooled** connection string |
| `DATABASE_URL_DIRECT` | Neon direct (non-pooled) string, for Alembic migrations only |
| `CAL_USERNAME` / `CAL_EVENT_SLUG` | `rohit-singh-g2fz4h` / `15min` |
| `CAL_API_KEY` | Optional |
| `CAL_WEBHOOK_SECRET` | Set the same value in Cal.com → Settings → Developer → Webhooks |
| `JWT_SECRET` | 64 random bytes |
| `CORS_ORIGINS` | `https://rohitsingh.in,https://www.rohitsingh.in,http://localhost:5179` |
| `FIREBASE_SERVICE_ACCOUNT` | JSON for FCM (phase 2) |

## 5. Website changes (phase 1)

- Point the website's API calls at `VITE_API_URL` (e.g. `https://api.rohitsingh.in`), and delete the Node `api/` folder and the Vite dev middleware.
- Capture UTM parameters, referrer and landing page on first visit (sessionStorage) and send them with the booking.
- Add a hidden honeypot field to the booking form.
- Add the consent line under "Confirm Booking" and a `/privacy` page.

## 6. Phases

| Phase | Scope | Done when |
|---|---|---|
| **1. Backend + DB** | Everything above except push; website switched over; Cal.com webhook live | A real booking appears in Neon with its source, and cancelling it in Cal.com updates its status |
| **2. Admin app v1** | Flutter: login, lead list, lead detail, tap to call / WhatsApp / email, push on new lead | You get a notification and can open the lead within seconds of a booking |
| **3. Pipeline + Clients** | Status and hot/warm/cold markers, tags, "Add lead" (manual), proposals, "Convert to client", Clients tab with projects, payments and money owed, referrals, notes, follow-up dates, filters | You can run your whole sales pipeline and money from the app, and every converted lead sits in Clients |
| **4. Insights + enquiries** | Stats screen, "message without a call" form, CSV export | You can see which channels bring clients and how fast you reply |
| **5. Operations** | Automated backups, data clean-up, daily reminder push (section 7) | Data is safe, privacy-compliant, and nothing slips |

### Flutter app: token refresh rule (phase 2)

The API treats a reused refresh token as stolen and **ends every session** for that admin. So the
app must never send two refreshes at once (e.g. two API calls both getting a 401 at the same moment):
- Run refresh through a **single-flight lock**: the first caller refreshes; any others wait for that
  same result and retry with the new access token.
- Save the new refresh token (secure storage) **before** releasing the lock, so no caller ever sends
  the old one again.
- If the refresh itself returns 401, go to the login screen; don't retry.

### Rate limits (built into the API)

Counted in Postgres (`rate_limits`), keyed by a hash of the IP or email; exceeding one returns
`429` with `Retry-After`. Login: 10 per 15 min per IP and 20 per hour per account. Bookings: 10 per
hour per IP (failed attempts count, so a visitor can retry a few slots). Enquiries: 5 per hour per IP. Refresh: 30 per 15 min per IP. If the database is down, the limits
are skipped, so bookings still go through.

## 7. Operations (after phase 2)

1. **Backups.** A weekly GitHub Actions workflow runs `pg_dump` against Neon and stores the encrypted dump in private storage (e.g. a private Cloudflare R2 bucket), keeping the last 12. Neon's free point-in-time restore window is short, and this database becomes your record of clients and money. Test a restore once.
2. **Data clean-up (privacy).** A monthly Vercel Cron job deletes contacts whose leads **never converted** and haven't been touched for **24 months**, and logs the count. Clients are never auto-deleted. The privacy policy states this period.
3. **Daily reminder push.** A Vercel Cron job each night at **11:00 pm IST** (`30 17 * * *`, since Vercel Cron runs in UTC) sends one notification looking ahead to tomorrow: calls and follow-ups due tomorrow, overdue payments, proposals expiring this week, and new leads not contacted for over 24 h. It only sends when there's something to act on.

## 8. Neon setup checklist (for you)

1. Create project **rohit-portfolio**: Postgres 17, region **AWS Asia Pacific (Sydney)** or **(Singapore)**.
2. Rename the default database to `portfolio` (or create it).
3. Create a branch **`dev`** off `main`, for local development and testing migrations.
4. Copy both connection strings (pooled and direct) for each branch into
   `rohit-portfolio-api/.env` (gitignored). Don't paste them in chat.

## 9. Open questions for review

1. ~~**Sydney or Singapore** region?~~ **Singapore**: Neon project `rohit-portfolio` (`weathered-dawn-41430202`,
   `aws-ap-southeast-1`, database `portfolio`) with the API pinned to Vercel's `sin1` region beside it.
   Singapore sits between India and Australia and supports Neon's newer services; Sydney doesn't.
2. Pipeline statuses: is `new → contacted → qualified → proposal_sent → won/lost/spam` right for how you sell?
   Temperature labels: `hot / warm / cold`, or `hot / mid / cold`?
3. Should the booking form ask for **company name** (optional)? The column exists either way.
4. Should there be a **budget range** dropdown on the booking form now, or only on the enquiry form later?
