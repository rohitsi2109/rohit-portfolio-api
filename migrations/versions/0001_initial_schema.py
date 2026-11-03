"""Initial schema: db/schema.sql

Revision ID: 0001
Revises:
Create Date: 2026-11-03
"""
from pathlib import Path

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

SCHEMA = Path(__file__).resolve().parents[2] / "db" / "schema.sql"

TABLES = [
    "webhook_events", "lead_events", "lead_notes", "push_devices", "refresh_tokens", "admin_users",
    "payments", "projects", "proposals", "bookings", "leads", "clients", "contacts",
]


def upgrade() -> None:
    # Run the file through the raw psycopg connection: with no parameters, psycopg sends it
    # as-is (multiple statements, $$ bodies and '%' in comments all stay untouched)
    op.get_bind().connection.driver_connection.execute(SCHEMA.read_text(encoding="utf-8"))


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS client_summary")
    op.execute(f"DROP TABLE IF EXISTS {', '.join(TABLES)} CASCADE")
    op.execute("DROP FUNCTION IF EXISTS set_updated_at()")
