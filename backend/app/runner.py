"""Isolated browser runner for the sandbox application adapter (Phase 6).

    python -m app.runner --loop      # until SIGTERM/SIGINT
    python -m app.runner --once      # at most one task

Claims only `application_fill` and `application_submit` tasks; the ingestion worker never
runs them, and this process never runs ingestion. The only destination is the local
sandbox ATS, and every kill switch is re-checked immediately before the submit click.
"""

import argparse
import logging
import socket
from uuid import uuid4

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.shutdown import StopSignal
from app.db.session import SessionLocal
from app.services.storage import ArtifactStorage
from app.services.submission.service import run_next_submission_task

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.runner")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--loop", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level)
    # Imported here so the API process never loads a browser library.
    from app.services.submission.browser import PlaywrightRunner

    stop = StopSignal().install()
    storage = ArtifactStorage(settings)
    worker_id = f"runner:{socket.gethostname()}:{uuid4().hex[:8]}"
    processed = 0
    while not stop.stopped:
        with SessionLocal() as db:
            task = run_next_submission_task(
                db, storage, worker_id=worker_id, runner_factory=PlaywrightRunner,
                settings=settings,
            )
            outcome = None if task is None else (task.id, task.kind)
        if outcome is not None:
            processed += 1
            logger.info("Processed %s task %s", outcome[1], outcome[0])
            if args.once:
                break
            continue
        if args.once or stop.wait(max(args.poll_seconds, 0.5)):
            break
    logger.info("Runner finished; processed=%s", processed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
