"""add task lease ownership and snapshot hash uniqueness

Revision ID: 0002_task_leases_and_snapshot_dedup
Revises: 0001_initial
"""

from alembic import op
import sqlalchemy as sa

revision = "0002_task_leases_and_snapshot_dedup"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "lease_owner" not in {column["name"] for column in inspector.get_columns("tasks")}:
        with op.batch_alter_table("tasks") as batch_op:
            batch_op.add_column(sa.Column("lease_owner", sa.String(length=128), nullable=True))
    if "uq_job_snapshot_hash" not in {
        constraint["name"] for constraint in inspector.get_unique_constraints("job_snapshots")
    }:
        with op.batch_alter_table("job_snapshots") as batch_op:
            batch_op.create_unique_constraint("uq_job_snapshot_hash", ["job_id", "content_hash"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "uq_job_snapshot_hash" in {
        constraint["name"] for constraint in inspector.get_unique_constraints("job_snapshots")
    }:
        with op.batch_alter_table("job_snapshots") as batch_op:
            batch_op.drop_constraint("uq_job_snapshot_hash", type_="unique")
    if "lease_owner" in {column["name"] for column in inspector.get_columns("tasks")}:
        with op.batch_alter_table("tasks") as batch_op:
            batch_op.drop_column("lease_owner")
