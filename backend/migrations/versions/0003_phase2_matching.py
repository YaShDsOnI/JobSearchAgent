"""add phase 2 analysis, requirements, and match records

Revision ID: 0003_phase2_matching
Revises: 0002_task_leases_and_snapshot_dedup
"""

from alembic import op
import sqlalchemy as sa

revision = "0003_phase2_matching"
down_revision = "0002_task_leases_and_snapshot_dedup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("profile_facts") as batch_op:
        batch_op.add_column(sa.Column("evidence_text", sa.Text(), nullable=True))

    op.create_table(
        "skills",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("canonical_name", sa.String(length=128), nullable=False),
        sa.Column("taxonomy_version", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("canonical_name", "taxonomy_version", name="uq_skill_taxonomy"),
    )
    op.create_table(
        "job_analyses",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("job_snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("extractor_version", sa.String(length=64), nullable=False),
        sa.Column("structured_json", sa.JSON(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["job_snapshot_id"], ["job_snapshots.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "job_snapshot_id",
            "extractor_version",
            name="uq_job_analysis_snapshot_version",
        ),
    )
    op.create_table(
        "job_evidence",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("analysis_id", sa.String(length=36), nullable=False),
        sa.Column("field_path", sa.String(length=255), nullable=False),
        sa.Column("quote", sa.Text(), nullable=False),
        sa.Column("start_offset", sa.Integer(), nullable=True),
        sa.Column("end_offset", sa.Integer(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["analysis_id"], ["job_analyses.id"], ondelete="CASCADE"),
    )
    op.create_table(
        "job_requirements",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("analysis_id", sa.String(length=36), nullable=False),
        sa.Column("skill_id", sa.String(length=36), nullable=True),
        sa.Column("skill_text", sa.String(length=128), nullable=False),
        sa.Column("requirement_kind", sa.String(length=32), nullable=False),
        sa.Column("importance", sa.Float(), nullable=False),
        sa.Column("minimum_years", sa.Float(), nullable=True),
        sa.Column("evidence_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["analysis_id"], ["job_analyses.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["skill_id"], ["skills.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["evidence_id"], ["job_evidence.id"], ondelete="SET NULL"),
    )
    op.create_table(
        "match_runs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("job_snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("profile_version_id", sa.String(length=36), nullable=False),
        sa.Column("preference_version_id", sa.String(length=36), nullable=True),
        sa.Column("policy_version", sa.String(length=64), nullable=False),
        sa.Column("extractor_version", sa.String(length=64), nullable=False),
        sa.Column("eligibility_status", sa.String(length=32), nullable=False),
        sa.Column("fit_score", sa.Float(), nullable=False),
        sa.Column("preference_score", sa.Float(), nullable=False),
        sa.Column("scores_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["job_snapshot_id"], ["job_snapshots.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["profile_version_id"], ["profile_versions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["preference_version_id"], ["preference_versions.id"], ondelete="SET NULL"
        ),
    )
    op.create_index("ix_job_analyses_job_snapshot_id", "job_analyses", ["job_snapshot_id"])
    op.create_index("ix_job_evidence_analysis_id", "job_evidence", ["analysis_id"])
    op.create_index("ix_job_requirements_analysis_id", "job_requirements", ["analysis_id"])
    op.create_index("ix_job_requirements_skill_id", "job_requirements", ["skill_id"])
    op.create_index("ix_match_runs_user_id", "match_runs", ["user_id"])
    op.create_index("ix_match_runs_job_snapshot_id", "match_runs", ["job_snapshot_id"])
    op.create_index("ix_match_runs_profile_version_id", "match_runs", ["profile_version_id"])
    op.create_index("ix_match_runs_preference_version_id", "match_runs", ["preference_version_id"])


def downgrade() -> None:
    for table in ("match_runs", "job_requirements", "job_evidence", "job_analyses", "skills"):
        op.drop_table(table)
    with op.batch_alter_table("profile_facts") as batch_op:
        batch_op.drop_column("evidence_text")
