"""Keep typed-in details on leads, add rate limits, compute "overdue" on the India date

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-03
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

VIEW = """
CREATE OR REPLACE VIEW client_summary AS
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
"""

OLD_VIEW = """
CREATE OR REPLACE VIEW client_summary AS
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
               coalesce(sum(pm.amount) FILTER (WHERE pm.status = 'pending' AND pm.due_on < current_date), 0) AS overdue
        FROM payments pm
        JOIN projects p ON p.id = pm.project_id
        WHERE p.client_id = cl.id
        GROUP BY pm.currency
    ) t
) pay;
"""


def upgrade() -> None:
    op.execute("""
        ALTER TABLE leads
            ADD COLUMN submitted_name    text,
            ADD COLUMN submitted_phone   text,
            ADD COLUMN submitted_company text
    """)
    op.execute("""
        CREATE TABLE rate_limits (
            key             text        NOT NULL,
            window_start    timestamptz NOT NULL,
            hits            integer     NOT NULL DEFAULT 1,
            PRIMARY KEY (key, window_start)
        )
    """)
    op.execute("CREATE INDEX rate_limits_window_idx ON rate_limits (window_start)")
    op.execute(VIEW)


def downgrade() -> None:
    op.execute(OLD_VIEW)
    op.execute("DROP TABLE rate_limits")
    op.execute("ALTER TABLE leads DROP COLUMN submitted_name, DROP COLUMN submitted_phone, DROP COLUMN submitted_company")
