"""application tracker (Phase 6): where the candidate has applied

Revision ID: 0010_application_tracker
Revises: 0009_application_review_package

One row per application the candidate made: link-out applications they report after
reviewing a package (bound to that package revision and hash), applications made outside
the app, and later the sandbox adapter's verified submissions. Nothing is sent.
"""

from alembic import op
import sqlalchemy as sa

revision = "0010_application_tracker"
down_revision = "0009_application_review_package"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tracked_applications",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("application_id", sa.String(length=36), nullable=True),
        sa.Column("package_id", sa.String(length=36), nullable=True),
        sa.Column("review_hash", sa.String(length=64), nullable=True),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("company", sa.String(length=255), nullable=False),
        sa.Column("role_title", sa.String(length=255), nullable=False),
        sa.Column("location", sa.String(length=255), nullable=True),
        sa.Column("job_url", sa.String(length=2048), nullable=True),
        sa.Column("applied_on", sa.Date(), nullable=False),
        sa.Column("stage", sa.String(length=32), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["application_id"], ["applications.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("application_id"),
        sa.CheckConstraint(
            "channel IN ('link_out', 'external', 'sandbox_adapter')",
            name="ck_tracked_applications_channel",
        ),
        sa.CheckConstraint(
            "stage IN ('applied', 'interviewing', 'offer', 'accepted', 'rejected', 'withdrawn')",
            name="ck_tracked_applications_stage",
        ),
    )
    op.create_index("ix_tracked_applications_user_id", "tracked_applications", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_tracked_applications_user_id", table_name="tracked_applications")
    op.drop_table("tracked_applications")
