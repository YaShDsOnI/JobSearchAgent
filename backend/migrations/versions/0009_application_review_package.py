"""application review package (Phase 5C, no submission)

Revision ID: 0009_application_review_package
Revises: 0008_connector_schedule

Applications store only the candidate's decision (open/skipped); a check constraint rules
out any other stored state, including a submitted one.
"""

from alembic import op
import sqlalchemy as sa

revision = "0009_application_review_package"
down_revision = "0008_connector_schedule"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "applications",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("connector_id", sa.String(length=36), nullable=False),
        sa.Column("source_job_id", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("target_kind", sa.String(length=32), nullable=False),
        sa.Column("current_package_id", sa.String(length=36), nullable=True),
        sa.Column("candidate_answers_json", sa.JSON(), nullable=False),
        sa.Column("skip_reason", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["job_id"], ["job_postings.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("user_id", "job_id", name="uq_application_user_job"),
        sa.CheckConstraint("status IN ('open', 'skipped')", name="ck_applications_status"),
    )
    op.create_index("ix_applications_user_id", "applications", ["user_id"])
    op.create_index("ix_applications_job_id", "applications", ["job_id"])
    op.create_table(
        "application_packages",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("application_id", sa.String(length=36), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("review_hash", sa.String(length=64), nullable=False),
        sa.Column("package_json", sa.JSON(), nullable=False),
        sa.Column("job_snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("match_run_id", sa.String(length=36), nullable=False),
        sa.Column("cv_version_id", sa.String(length=36), nullable=False),
        sa.Column("profile_version_id", sa.String(length=36), nullable=False),
        sa.Column("preference_version_id", sa.String(length=36), nullable=True),
        sa.Column("connector_id", sa.String(length=36), nullable=False),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_reason", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["application_id"], ["applications.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["job_snapshot_id"], ["job_snapshots.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["match_run_id"], ["match_runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["cv_version_id"], ["cv_versions.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint(
            "application_id", "revision", name="uq_application_package_revision"
        ),
    )
    op.create_index(
        "ix_application_packages_application_id", "application_packages", ["application_id"]
    )
    op.create_index(
        "ix_application_packages_job_snapshot_id", "application_packages", ["job_snapshot_id"]
    )
    op.create_index(
        "ix_application_packages_match_run_id", "application_packages", ["match_run_id"]
    )
    op.create_index(
        "ix_application_packages_cv_version_id", "application_packages", ["cv_version_id"]
    )
    op.create_table(
        "approval_records",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("application_id", sa.String(length=36), nullable=False),
        sa.Column("package_id", sa.String(length=36), nullable=False),
        sa.Column("reviewed_payload_hash", sa.String(length=64), nullable=False),
        sa.Column("approved_by_user_id", sa.String(length=36), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(["application_id"], ["applications.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["package_id"], ["application_packages.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["approved_by_user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_approval_records_application_id", "approval_records", ["application_id"])
    op.create_index("ix_approval_records_package_id", "approval_records", ["package_id"])
    op.create_table(
        "application_events",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("application_id", sa.String(length=36), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["application_id"], ["applications.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "ix_application_events_application_id", "application_events", ["application_id"]
    )


def downgrade() -> None:
    op.drop_table("application_events")
    op.drop_table("approval_records")
    op.drop_table("application_packages")
    op.drop_table("applications")
