"""Ingestion worker: processes queued source_ingest tasks.

- `python -m app.worker --once` processes at most one task.
- `python -m app.worker` processes up to `--max-tasks` and exits when the queue is empty.
- `python -m app.worker --loop` runs until SIGTERM/SIGINT, polling an empty queue every
  `--poll-seconds`. Each task gets a fresh database session.
"""

import argparse
import logging
import socket
from uuid import uuid4

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.shutdown import StopSignal
from app.db.session import SessionLocal
from app.services.sources.worker import run_next_ingestion_task
from app.services.storage import ArtifactStorage

logger = logging.getLogger(__name__)

MIN_POLL_SECONDS = 1


def run_worker(
    storage: ArtifactStorage,
    *,
    worker_id: str,
    stop: StopSignal,
    limit: int | None,
    loop: bool,
    poll_seconds: float,
) -> int:
    """Process tasks until the limit is reached, the queue is empty (unless looping), or a
    stop is requested. Returns the number of tasks processed."""
    processed = 0
    while not stop.stopped and (limit is None or processed < limit):
        with SessionLocal() as db:
            task = run_next_ingestion_task(db, storage, worker_id=worker_id)
            outcome = None if task is None else (task.id, task.state)
        if outcome is None:
            if not loop or stop.wait(max(poll_seconds, MIN_POLL_SECONDS)):
                break
            continue
        processed += 1
        logger.info("Processed ingestion task %s state=%s", *outcome)
    return processed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Process queued source ingestion tasks.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="Process at most one task")
    mode.add_argument("--loop", action="store_true", help="Run until SIGTERM/SIGINT")
    parser.add_argument("--max-tasks", type=int, default=100)
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level)
    worker_id = f"{socket.gethostname()}:{uuid4().hex[:8]}"
    stop = StopSignal().install()
    if args.loop:
        limit: int | None = None
    else:
        limit = 1 if args.once else max(args.max_tasks, 1)
    processed = run_worker(
        ArtifactStorage(settings),
        worker_id=worker_id,
        stop=stop,
        limit=limit,
        loop=args.loop,
        poll_seconds=args.poll_seconds,
    )
    logger.info("Ingestion worker finished; processed=%s", processed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
