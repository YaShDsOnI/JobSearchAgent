"""support access grants (Phase 7): candidate-granted, time-boxed, read-only summaries

Revision ID: 0012_support_access
Revises: 0011_sandbox_submissions

A candidate grants one support account a redacted, read-only view of their case for at most
72 hours. Every view is audited and counted on the grant, which the candidate can see and
revoke. Grants are deleted with the profile.
"""

from alembic import op
import sqlalchemy as sa

revision = "0012_support_access"
down_revision = "0011_sandbox_submissions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "support_access_grants",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("support_user_id", sa.String(length=36), nullable=False),
        sa.Column("scope", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_viewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("view_count", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["support_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.CheckConstraint("scope IN ('summary')", name="ck_support_access_grants_scope"),
    )
    op.create_index("ix_support_access_grants_user_id", "support_access_grants", ["user_id"])
    op.create_index(
        "ix_support_access_grants_support_user_id", "support_access_grants", ["support_user_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_support_access_grants_support_user_id", table_name="support_access_grants")
    op.drop_index("ix_support_access_grants_user_id", table_name="support_access_grants")
    op.drop_table("support_access_grants")
