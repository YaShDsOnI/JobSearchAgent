import base64
import os
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# SQLite in-memory by default. Set TEST_DATABASE_URL (e.g. a disposable PostgreSQL
# database) to run the same suite against a schema built by the Alembic migrations.
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "sqlite+pysqlite:///:memory:")
IS_SQLITE = TEST_DATABASE_URL.startswith("sqlite")
BACKEND_ROOT = Path(__file__).resolve().parent.parent

os.environ.setdefault("APP_ENV", "test")
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ.setdefault("SESSION_SECRET", "test-session-secret-that-is-long-enough-123456")
os.environ.setdefault("ARTIFACT_ENCRYPTION_KEY", base64.urlsafe_b64encode(b"0" * 32).decode())
os.environ.setdefault("ARTIFACT_STORAGE_PATH", str(Path(__file__).parent / ".artifacts"))

from app.db.models import Base  # noqa: E402
from app.db.session import get_db  # noqa: E402
from app.main import app  # noqa: E402

if IS_SQLITE:
    test_engine = create_engine(
        TEST_DATABASE_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(test_engine)
else:
    from alembic import command
    from alembic.config import Config

    test_engine = create_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    with test_engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    alembic_config = Config(str(BACKEND_ROOT / "alembic.ini"))
    alembic_config.set_main_option("script_location", str(BACKEND_ROOT / "migrations"))
    command.upgrade(alembic_config, "head")

TestingSessionLocal = sessionmaker(
    bind=test_engine, autoflush=False, autocommit=False, expire_on_commit=False
)


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_get_db

requires_postgres = pytest.mark.skipif(
    IS_SQLITE, reason="needs a real multi-connection database (set TEST_DATABASE_URL)"
)


@pytest.fixture(scope="session", autouse=True)
def clean_artifact_store():
    """Encrypted test blobs must not accumulate between runs (rows are truncated)."""
    root = Path(os.environ["ARTIFACT_STORAGE_PATH"])
    default_root = Path(__file__).parent / ".artifacts"
    if root.resolve() == default_root.resolve():
        shutil.rmtree(root, ignore_errors=True)
    yield
    if root.resolve() == default_root.resolve():
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture()
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def clean_database():
    yield
    if IS_SQLITE:
        for table in reversed(Base.metadata.sorted_tables):
            with test_engine.begin() as connection:
                connection.execute(table.delete())
    else:
        names = ", ".join(table.name for table in Base.metadata.sorted_tables)
        with test_engine.begin() as connection:
            connection.execute(text(f"TRUNCATE {names} CASCADE"))


def csrf_headers(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies.get("csrf_token", "")}
