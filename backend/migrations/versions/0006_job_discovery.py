"""job discovery: source authorization/health, runs, lifecycle, dedup, snapshot versions

Revision ID: 0006_job_discovery
Revises: 0005_cv_versions

Existing snapshots receive per-job versions ordered by retrieval time and are marked
`legacy-v0` (pre-normalizer). The `(job_id, content_hash)` uniqueness is replaced by
`(job_id, version)` so a revert to earlier content becomes a new version instead of a
constraint violation. Only the bundled `phase1-fixture` connector is given an authorization
record; any other pre-existing connector stays unauthorized and therefore fails closed.
"""

from alembic import op
import sqlalchemy as sa

revision = "0006_job_discovery"
down_revision = "0005_cv_versions"
branch_labels = None
depends_on = None

FIXTURE_AUTHORIZATION = {
    "basis": "local_fixture",
    "evidence_ref": "docs/source-connectors.md#fixture-bundled-sanitized-fixture-catalog",
    "reviewed_by": "phase4-source-policy",
    "reviewed_at": "2026-09-29T00:00:00+00:00",
    "terms_version": "fixture-v1",
}
FIXTURE_CONFIG = {"dataset": "jobs", "allowed_url_hosts": ["example.invalid"]}


def upgrade() -> None:
    with op.batch_alter_table("source_connectors") as batch:
        batch.add_column(sa.Column("owner_user_id", sa.String(length=36), nullable=True))
        batch.add_column(
            sa.Column("authorization_json", sa.JSON(), nullable=False, server_default="{}")
        )
        batch.add_column(sa.Column("config_json", sa.JSON(), nullable=False, server_default="{}"))
        batch.add_column(
            sa.Column(
                "health_status", sa.String(length=32), nullable=False, server_default="unknown"
            )
        )
        batch.add_column(
            sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("last_error_code", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("active_run_id", sa.String(length=36), nullable=True))
        batch.create_foreign_key(
            "fk_source_connectors_owner_user_id", "users", ["owner_user_id"], ["id"],
            ondelete="CASCADE",
        )
        batch.create_index("ix_source_connectors_owner_user_id", ["owner_user_id"])

    with op.batch_alter_table("source_runs") as batch:
        batch.add_column(sa.Column("idempotency_key", sa.String(length=255), nullable=True))
        batch.add_column(sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"))
        batch.add_column(sa.Column("requested_by_user_id", sa.String(length=36), nullable=True))
        batch.add_column(sa.Column("provider_version", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("authorization_basis", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("error_code", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("error_detail", sa.String(length=512), nullable=True))
        batch.create_foreign_key(
            "fk_source_runs_requested_by_user_id", "users", ["requested_by_user_id"], ["id"],
            ondelete="SET NULL",
        )
        batch.create_index("ix_source_runs_connector_id", ["connector_id"])
        batch.create_index("ix_source_runs_idempotency_key", ["idempotency_key"])

    with op.batch_alter_table("job_postings") as batch:
        batch.add_column(sa.Column("status_reason", sa.String(length=64), nullable=True))
        batch.add_column(
            sa.Column("status_changed_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch.add_column(sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("owner_user_id", sa.String(length=36), nullable=True))
        batch.add_column(sa.Column("url_fingerprint", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("content_fingerprint", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("duplicate_of_job_id", sa.String(length=36), nullable=True))
        batch.add_column(sa.Column("dedup_reason", sa.String(length=32), nullable=True))
        batch.create_foreign_key(
            "fk_job_postings_owner_user_id", "users", ["owner_user_id"], ["id"],
            ondelete="CASCADE",
        )
        batch.create_foreign_key(
            "fk_job_postings_duplicate_of_job_id", "job_postings", ["duplicate_of_job_id"],
            ["id"], ondelete="SET NULL",
        )
        batch.create_index("ix_job_postings_owner_user_id", ["owner_user_id"])
        batch.create_index("ix_job_postings_url_fingerprint", ["url_fingerprint"])
        batch.create_index("ix_job_postings_content_fingerprint", ["content_fingerprint"])
        batch.create_index("ix_job_postings_duplicate_of_job_id", ["duplicate_of_job_id"])

    with op.batch_alter_table("job_snapshots") as batch:
        batch.add_column(sa.Column("version", sa.Integer(), nullable=False, server_default="1"))
        batch.add_column(sa.Column("raw_content_hash", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("normalizer_version", sa.String(length=64), nullable=True))
        batch.add_column(
            sa.Column("normalization_warnings", sa.JSON(), nullable=False, server_default="[]")
        )
        batch.add_column(sa.Column("connector_id", sa.String(length=36), nullable=True))
        batch.add_column(sa.Column("source_run_id", sa.String(length=36), nullable=True))
        batch.add_column(
            sa.Column("provenance_json", sa.JSON(), nullable=False, server_default="{}")
        )

    bind = op.get_bind()
    snapshots = sa.table(
        "job_snapshots",
        sa.column("id", sa.String),
        sa.column("job_id", sa.String),
        sa.column("version", sa.Integer),
        sa.column("normalizer_version", sa.String),
        sa.column("connector_id", sa.String),
        sa.column("retrieved_at", sa.DateTime),
    )
    postings = sa.table(
        "job_postings", sa.column("id", sa.String), sa.column("connector_id", sa.String)
    )
    connector_by_job = dict(bind.execute(sa.select(postings.c.id, postings.c.connector_id)).all())
    rows = bind.execute(
        sa.select(snapshots.c.id, snapshots.c.job_id).order_by(
            snapshots.c.job_id, snapshots.c.retrieved_at, snapshots.c.id
        )
    ).all()
    versions: dict[str, int] = {}
    for snapshot_id, job_id in rows:
        versions[job_id] = versions.get(job_id, 0) + 1
        bind.execute(
            snapshots.update()
            .where(snapshots.c.id == snapshot_id)
            .values(
                version=versions[job_id],
                normalizer_version="legacy-v0",
                connector_id=connector_by_job.get(job_id),
            )
        )

    connectors = sa.table(
        "source_connectors",
        sa.column("name", sa.String),
        sa.column("kind", sa.String),
        sa.column("tier", sa.String),
        sa.column("authorization_json", sa.JSON),
        sa.column("config_json", sa.JSON),
    )
    bind.execute(
        connectors.update()
        .where(
            connectors.c.name == "phase1-fixture",
            connectors.c.kind == "fixture",
            connectors.c.tier == "local",
        )
        .values(authorization_json=FIXTURE_AUTHORIZATION, config_json=FIXTURE_CONFIG)
    )

    inspector = sa.inspect(bind)
    existing_uniques = {
        item["name"] for item in inspector.get_unique_constraints("job_snapshots")
    }
    with op.batch_alter_table("job_snapshots") as batch:
        if "uq_job_snapshot_hash" in existing_uniques:
            batch.drop_constraint("uq_job_snapshot_hash", type_="unique")
        batch.create_unique_constraint("uq_job_snapshot_version", ["job_id", "version"])
        batch.create_foreign_key(
            "fk_job_snapshots_connector_id", "source_connectors", ["connector_id"], ["id"],
            ondelete="SET NULL",
        )
        batch.create_foreign_key(
            "fk_job_snapshots_source_run_id", "source_runs", ["source_run_id"], ["id"],
            ondelete="SET NULL",
        )
        batch.create_index("ix_job_snapshots_source_run_id", ["source_run_id"])


def downgrade() -> None:
    bind = op.get_bind()
    # Restoring (job_id, content_hash) uniqueness requires dropping revert-duplicates first.
    snapshots = sa.table(
        "job_snapshots",
        sa.column("id", sa.String),
        sa.column("job_id", sa.String),
        sa.column("content_hash", sa.String),
        sa.column("version", sa.Integer),
    )
    seen: set[tuple[str, str]] = set()
    for snapshot_id, job_id, content_hash in bind.execute(
        sa.select(snapshots.c.id, snapshots.c.job_id, snapshots.c.content_hash).order_by(
            snapshots.c.job_id, snapshots.c.version
        )
    ).all():
        if (job_id, content_hash) in seen:
            bind.execute(snapshots.delete().where(snapshots.c.id == snapshot_id))
        seen.add((job_id, content_hash))

    with op.batch_alter_table("job_snapshots") as batch:
        batch.drop_index("ix_job_snapshots_source_run_id")
        batch.drop_constraint("fk_job_snapshots_source_run_id", type_="foreignkey")
        batch.drop_constraint("fk_job_snapshots_connector_id", type_="foreignkey")
        batch.drop_constraint("uq_job_snapshot_version", type_="unique")
        batch.create_unique_constraint("uq_job_snapshot_hash", ["job_id", "content_hash"])
        for column in (
            "provenance_json", "source_run_id", "connector_id", "normalization_warnings",
            "normalizer_version", "raw_content_hash", "version",
        ):
            batch.drop_column(column)

    with op.batch_alter_table("job_postings") as batch:
        for index in (
            "ix_job_postings_duplicate_of_job_id", "ix_job_postings_content_fingerprint",
            "ix_job_postings_url_fingerprint", "ix_job_postings_owner_user_id",
        ):
            batch.drop_index(index)
        batch.drop_constraint("fk_job_postings_duplicate_of_job_id", type_="foreignkey")
        batch.drop_constraint("fk_job_postings_owner_user_id", type_="foreignkey")
        for column in (
            "dedup_reason", "duplicate_of_job_id", "content_fingerprint", "url_fingerprint",
            "owner_user_id", "closed_at", "status_changed_at", "status_reason",
        ):
            batch.drop_column(column)

    with op.batch_alter_table("source_runs") as batch:
        batch.drop_index("ix_source_runs_idempotency_key")
        batch.drop_index("ix_source_runs_connector_id")
        batch.drop_constraint("fk_source_runs_requested_by_user_id", type_="foreignkey")
        for column in (
            "error_detail", "error_code", "authorization_basis", "provider_version",
            "requested_by_user_id", "attempt", "idempotency_key",
        ):
            batch.drop_column(column)

    with op.batch_alter_table("source_connectors") as batch:
        batch.drop_index("ix_source_connectors_owner_user_id")
        batch.drop_constraint("fk_source_connectors_owner_user_id", type_="foreignkey")
        for column in (
            "active_run_id", "last_error_code", "last_failure_at", "last_success_at",
            "last_run_at", "consecutive_failures", "health_status", "config_json",
            "authorization_json", "owner_user_id",
        ):
            batch.drop_column(column)

