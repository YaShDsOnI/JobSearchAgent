"""Scheduler tick: `python -m app.scheduler --once` enqueues due connector runs.

It only enqueues durable `source_ingest` tasks; `python -m app.worker` executes them. Run it
from cron/systemd timers (e.g. every minute) or with `--loop`. No separate scheduler service
is required: the task table is the durable queue and ticks are idempotent per slot.
"""

import argparse
import json
import logging
import time
from dataclasses import asdict

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.session import SessionLocal
from app.services.sources.scheduler import schedule_due_runs

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.scheduler")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="Run a single tick and exit")
    mode.add_argument("--loop", action="store_true", help="Tick repeatedly")
    parser.add_argument("--tick-seconds", type=int, default=60)
    args = parser.parse_args(argv)
    configure_logging(get_settings().log_level)
    while True:
        with SessionLocal() as db:
            decisions = schedule_due_runs(db)
        print(json.dumps([asdict(item) for item in decisions]), flush=True)
        if args.once:
            return 0
        time.sleep(max(args.tick_seconds, 10))


if __name__ == "__main__":
    raise SystemExit(main())
