"""add explicit match review workflow

Revision ID: 0004_match_reviews
Revises: 0003_phase2_matching
"""

from alembic import op
import sqlalchemy as sa

revision = "0004_match_reviews"
down_revision = "0003_phase2_matching"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "match_reviews",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("match_run_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("reason_json", sa.JSON(), nullable=False),
        sa.Column("resolution_json", sa.JSON(), nullable=True),
        sa.Column("resolved_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rerun_match_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["match_run_id"], ["match_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["resolved_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["rerun_match_id"], ["match_runs.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("match_run_id", name="uq_match_review_run"),
    )
    op.create_index("ix_match_reviews_user_id", "match_reviews", ["user_id"])
    op.create_index("ix_match_reviews_match_run_id", "match_reviews", ["match_run_id"])


def downgrade() -> None:
    op.drop_table("match_reviews")
