"""record why a CV version was superseded

Revision ID: 0007_cv_supersession_reason
Revises: 0006_job_discovery

CV versions superseded before this revision have no recorded cause; they are marked
`legacy_unrecorded` rather than guessed.
"""

from alembic import op
import sqlalchemy as sa

revision = "0007_cv_supersession_reason"
down_revision = "0006_job_discovery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("cv_versions") as batch:
        batch.add_column(sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("superseded_reason", sa.String(length=64), nullable=True))
        batch.add_column(
            sa.Column("superseded_detail_json", sa.JSON(), nullable=False, server_default="{}")
        )
    cv_versions = sa.table(
        "cv_versions",
        sa.column("status", sa.String),
        sa.column("superseded_reason", sa.String),
    )
    op.get_bind().execute(
        cv_versions.update()
        .where(cv_versions.c.status == "superseded")
        .values(superseded_reason="legacy_unrecorded")
    )


def downgrade() -> None:
    with op.batch_alter_table("cv_versions") as batch:
        batch.drop_column("superseded_detail_json")
        batch.drop_column("superseded_reason")
        batch.drop_column("superseded_at")
