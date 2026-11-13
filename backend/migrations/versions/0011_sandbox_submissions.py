"""sandbox application adapter (Phase 6): destinations and submissions

Revision ID: 0011_sandbox_submissions
Revises: 0010_application_tracker

The only destination is the local sandbox ATS. One submission row per application package
(unique idempotency key); the submit click is recorded before it happens.
"""

from alembic import op
import sqlalchemy as sa

revision = "0011_sandbox_submissions"
down_revision = "0010_application_tracker"
branch_labels = None
depends_on = None

STATES = (
    "'queued', 'filling', 'final_review', 'submit_queued', 'submitting', 'submitted', "
    "'submitted_unverified', 'not_received', 'paused', 'blocked', 'failed', 'cancelled'"
)


def upgrade() -> None:
    op.create_table(
        "application_destinations",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("origin", sa.String(length=255), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("policy_status", sa.String(length=32), nullable=False),
        sa.Column("adapter_version", sa.String(length=64), nullable=False),
        sa.Column("authorization_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "application_submissions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("application_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("package_id", sa.String(length=36), nullable=False),
        sa.Column("review_hash", sa.String(length=64), nullable=False),
        sa.Column("approval_id", sa.String(length=36), nullable=False),
        sa.Column("destination_name", sa.String(length=64), nullable=False),
        sa.Column("destination_url", sa.String(length=2048), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("pause_reason", sa.String(length=64), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("fill_manifest_json", sa.JSON(), nullable=True),
        sa.Column("fill_hash", sa.String(length=64), nullable=True),
        sa.Column("confirmed_fill_hash", sa.String(length=64), nullable=True),
        sa.Column("cv_sha256", sa.String(length=64), nullable=True),
        sa.Column("fill_runs", sa.Integer(), nullable=False),
        sa.Column("submit_clicked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmation_id", sa.String(length=128), nullable=True),
        sa.Column("verification", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["application_id"], ["applications.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("idempotency_key", name="uq_application_submission_idempotency"),
        sa.CheckConstraint(f"state IN ({STATES})", name="ck_application_submissions_state"),
    )
    op.create_index(
        "ix_application_submissions_application_id", "application_submissions", ["application_id"]
    )
    op.create_index("ix_application_submissions_user_id", "application_submissions", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_application_submissions_user_id", table_name="application_submissions")
    op.drop_index(
        "ix_application_submissions_application_id", table_name="application_submissions"
    )
    op.drop_table("application_submissions")
    op.drop_table("application_destinations")
