from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, inspect, pool, text

from app.core.config import get_settings
from app.db.models import Base

config = context.config
config.set_main_option("sqlalchemy.url", get_settings().database_url.replace("%", "%%"))
if config.config_file_name:
    fileConfig(config.config_file_name)
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=get_settings().database_url, target_metadata=target_metadata, literal_binds=True
    )
    with context.begin_transaction():
        context.run_migrations()


VERSION_NUM_LENGTH = 255


def ensure_version_table(connection) -> None:
    """Allow revision IDs longer than Alembic's default VARCHAR(32).

    Existing revision IDs (e.g. `0002_task_leases_and_snapshot_dedup`) exceed 32
    characters. SQLite ignores VARCHAR lengths, but PostgreSQL rejects the version
    update, so the table is created (or widened) before any migration runs. Revision IDs
    are never renamed because databases have already recorded them.
    """
    if connection.dialect.name == "sqlite":
        return
    inspector = inspect(connection)
    if not inspector.has_table("alembic_version"):
        connection.execute(
            text(
                f"CREATE TABLE alembic_version (version_num VARCHAR({VERSION_NUM_LENGTH}) "
                "NOT NULL, CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
            )
        )
    else:
        column = next(
            item for item in inspector.get_columns("alembic_version")
            if item["name"] == "version_num"
        )
        if (getattr(column["type"], "length", None) or 0) < VERSION_NUM_LENGTH:
            connection.execute(
                text(
                    "ALTER TABLE alembic_version ALTER COLUMN version_num "
                    f"TYPE VARCHAR({VERSION_NUM_LENGTH})"
                )
            )
    connection.commit()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        ensure_version_table(connection)
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
