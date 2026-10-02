import json

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.services.catalog import ensure_fixture_connector
from app.services.sources.pipeline import run_ingestion
from app.services.storage import ArtifactStorage


def main() -> None:
    """Ingest the bundled sanitized fixture catalog through the discovery pipeline."""
    with SessionLocal() as db:
        connector = ensure_fixture_connector(db)
        db.commit()
        result = run_ingestion(
            db, ArtifactStorage(get_settings()), connector_id=connector.id, actor="seed"
        )
    summary = {
        key: result.stats.get(key, 0)
        for key in ("created", "updated", "unchanged", "closed", "stale", "rejected")
    }
    print(json.dumps({"status": result.status, **summary}, sort_keys=True))


if __name__ == "__main__":
    main()
