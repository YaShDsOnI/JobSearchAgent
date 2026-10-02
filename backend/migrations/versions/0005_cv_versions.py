"""add immutable factual CV versions and claims

Revision ID: 0005_cv_versions
Revises: 0004_match_reviews
"""

from alembic import op
import sqlalchemy as sa

revision = "0005_cv_versions"
down_revision = "0004_match_reviews"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cv_versions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("job_snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("profile_version_id", sa.String(length=36), nullable=False),
        sa.Column("match_run_id", sa.String(length=36), nullable=True),
        sa.Column("generator_version", sa.String(length=64), nullable=False),
        sa.Column("template_version", sa.String(length=64), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("docx_artifact_id", sa.String(length=36), nullable=True),
        sa.Column("pdf_artifact_id", sa.String(length=36), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("source_manifest_json", sa.JSON(), nullable=False),
        sa.Column("approved_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejection_note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["job_id"], ["job_postings.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["job_snapshot_id"], ["job_snapshots.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["profile_version_id"], ["profile_versions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["match_run_id"], ["match_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["docx_artifact_id"], ["stored_artifacts.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["pdf_artifact_id"], ["stored_artifacts.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["approved_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
    )
    op.create_table(
        "cv_claims",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("cv_version_id", sa.String(length=36), nullable=False),
        sa.Column("section", sa.String(length=64), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("source_fact_ids", sa.JSON(), nullable=False),
        sa.Column("job_requirement_ids", sa.JSON(), nullable=False),
        sa.Column("validation_status", sa.String(length=32), nullable=False),
        sa.Column("validation_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["cv_version_id"], ["cv_versions.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_cv_versions_user_id", "cv_versions", ["user_id"])
    op.create_index("ix_cv_versions_job_id", "cv_versions", ["job_id"])
    op.create_index("ix_cv_versions_job_snapshot_id", "cv_versions", ["job_snapshot_id"])
    op.create_index("ix_cv_versions_profile_version_id", "cv_versions", ["profile_version_id"])
    op.create_index("ix_cv_versions_match_run_id", "cv_versions", ["match_run_id"])
    op.create_index("ix_cv_versions_content_hash", "cv_versions", ["content_hash"])
    op.create_index("ix_cv_versions_status", "cv_versions", ["status"])
    op.create_index("ix_cv_claims_cv_version_id", "cv_claims", ["cv_version_id"])


def downgrade() -> None:
    op.drop_table("cv_claims")
    op.drop_table("cv_versions")
