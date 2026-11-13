"""Scheduler tick: `python -m app.scheduler --once` enqueues due connector runs.

It only enqueues durable `source_ingest` tasks; `python -m app.worker` executes them. Run it
from cron/systemd timers (e.g. every minute) or with `--loop`, which runs until SIGTERM or
SIGINT. Ticks are idempotent per slot: the task table is the durable queue.

After each tick, health alerts are sent when `ALERT_WEBHOOK_REF` and `ALERT_WEBHOOK_HOST`
are set (see `app/services/sources/alerts.py`). An alerting failure never stops scheduling.
"""

import argparse
import json
import logging
from dataclasses import asdict

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.shutdown import StopSignal
from app.db.session import SessionLocal
from app.services.sources.alerts import send_health_alerts
from app.services.sources.scheduler import schedule_due_runs

logger = logging.getLogger(__name__)

MIN_TICK_SECONDS = 10


def run_tick() -> None:
    with SessionLocal() as db:
        decisions = schedule_due_runs(db)
    print(json.dumps([asdict(item) for item in decisions]), flush=True)
    if not get_settings().alerting_enabled:
        return
    try:
        with SessionLocal() as db:
            outcomes = send_health_alerts(db)
    except Exception as exc:  # noqa: BLE001 - alerting must never stop scheduling.
        logger.error(
            "Health alert evaluation failed (%s)",
            type(exc).__name__,
            extra={"error_type": "alert_evaluation_failed"},
        )
        return
    if outcomes:
        print(json.dumps({"alerts": [asdict(item) for item in outcomes]}), flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.scheduler")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="Run a single tick and exit")
    mode.add_argument("--loop", action="store_true", help="Tick until SIGTERM/SIGINT")
    parser.add_argument("--tick-seconds", type=int, default=60)
    args = parser.parse_args(argv)
    configure_logging(get_settings().log_level)
    stop = StopSignal().install()
    while not stop.stopped:
        run_tick()
        if args.once or stop.wait(max(args.tick_seconds, MIN_TICK_SECONDS)):
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
