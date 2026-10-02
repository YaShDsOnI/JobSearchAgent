"""Ingestion worker: `python -m app.worker --once` processes queued source_ingest tasks."""

import argparse
import logging
import socket
from uuid import uuid4

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.session import SessionLocal
from app.services.sources.worker import run_next_ingestion_task
from app.services.storage import ArtifactStorage

logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description="Process queued source ingestion tasks.")
    parser.add_argument("--once", action="store_true", help="Process at most one task")
    parser.add_argument("--max-tasks", type=int, default=100)
    args = parser.parse_args()
    settings = get_settings()
    configure_logging(settings.log_level)
    worker_id = f"{socket.gethostname()}:{uuid4().hex[:8]}"
    storage = ArtifactStorage(settings)
    limit = 1 if args.once else max(args.max_tasks, 1)
    processed = 0
    with SessionLocal() as db:
        while processed < limit:
            task = run_next_ingestion_task(db, storage, worker_id=worker_id)
            if task is None:
                break
            processed += 1
            logger.info("Processed ingestion task %s state=%s", task.id, task.state)
    logger.info("Ingestion worker finished; processed=%s", processed)


if __name__ == "__main__":
    main()
