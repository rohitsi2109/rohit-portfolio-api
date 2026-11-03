"""Sessions: why a refresh token was revoked, and when its device logged in

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-04
"""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE refresh_tokens
            ADD COLUMN session_started_at timestamptz NOT NULL DEFAULT now(),
            ADD COLUMN revoked_reason text
                CHECK (revoked_reason IN ('rotated', 'logout', 'revoked', 'reuse_detected'))
    """)
    # Existing rows: a session started when its token was issued; past revocations were rotations
    op.execute("UPDATE refresh_tokens SET session_started_at = created_at")
    op.execute("UPDATE refresh_tokens SET revoked_reason = 'rotated' WHERE revoked_at IS NOT NULL")
    op.execute("""
        ALTER TABLE refresh_tokens ADD CONSTRAINT refresh_tokens_revoked_reason_chk
            CHECK ((revoked_at IS NULL) = (revoked_reason IS NULL))
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE refresh_tokens DROP CONSTRAINT refresh_tokens_revoked_reason_chk")
    op.execute("ALTER TABLE refresh_tokens DROP COLUMN revoked_reason, DROP COLUMN session_started_at")
