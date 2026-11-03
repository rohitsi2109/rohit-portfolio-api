# rohit-portfolio-api

Backend for rohitsingh.in: lead capture, the Cal.com integration and the admin API for the
Flutter app. Design and decisions: [docs/PLAN.md](docs/PLAN.md). Schema: [db/schema.sql](db/schema.sql).

## Local development

```sh
py -3.12 -m venv .venv
.venv/Scripts/pip install -r requirements-dev.txt     # macOS/Linux: .venv/bin/pip
cp .env.example .env                                   # fill in the Neon dev-branch strings
.venv/Scripts/alembic upgrade head                     # creates the schema (uses DATABASE_URL_DIRECT)
.venv/Scripts/python -m app.cli create-admin --email you@example.com --name "Rohit Singh"
.venv/Scripts/uvicorn app.main:app --reload --port 8000
```

Interactive API docs: http://localhost:8000/docs. The website reads `VITE_API_URL=http://localhost:8000`.

## Tests

Tests need a throwaway Postgres (they wipe every table, so never point them at Neon):

```sh
docker run -d --name portfolio-test-pg -e POSTGRES_PASSWORD=test -e POSTGRES_DB=portfolio_test -p 55432:5432 postgres:17-alpine
.venv/Scripts/pytest
```

## Deploying (Vercel)

1. Create a Vercel project from this repo; add the domain `api.rohitsingh.in`.
2. Set the environment variables from `.env.example` (production uses the Neon `main` branch strings).
3. Run `alembic upgrade head` against production from your machine before the first deploy.
4. In Cal.com → Settings → Developer → Webhooks, add `https://api.rohitsingh.in/v1/webhooks/cal` with
   the same secret as `CAL_WEBHOOK_SECRET`, and enable *Booking created*, *Booking cancelled*,
   *Booking rescheduled*, *Meeting ended* and *No-show updated*.
5. In the website's Vercel project, set `VITE_API_URL=https://api.rohitsingh.in` and redeploy.

## Booking flow notes

- The lead is committed **before** calling Cal.com, and its id travels in the Cal.com booking
  metadata. Whichever arrives first (the API's own insert or the `BOOKING_CREATED` webhook) records
  the booking; the other is a no-op.
- If the database is down, the booking still goes through, and the webhook rebuilds the lead
  (with its attribution and consent) from that metadata.
- Bookings made directly on cal.com (not through the website) are logged in `webhook_events` but
  don't create leads, because there is no consent record for them.
