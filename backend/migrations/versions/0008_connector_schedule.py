"""connector run schedule

Revision ID: 0008_connector_schedule
Revises: 0007_cv_supersession_reason

Existing connectors are unscheduled (NULL interval) and keep running only on demand.
"""

from alembic import op
import sqlalchemy as sa

revision = "0008_connector_schedule"
down_revision = "0007_cv_supersession_reason"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("source_connectors") as batch:
        batch.add_column(sa.Column("schedule_interval_seconds", sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column("next_run_due_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch.create_index("ix_source_connectors_next_run_due_at", ["next_run_due_at"])


def downgrade() -> None:
    with op.batch_alter_table("source_connectors") as batch:
        batch.drop_index("ix_source_connectors_next_run_due_at")
        batch.drop_column("next_run_due_at")
        batch.drop_column("schedule_interval_seconds")
