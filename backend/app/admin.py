"""Operational CLI. Requires the same environment (database and artifact key) as the API.

    python -m app.admin promote-operator --email ops@example.org --reason "ticket OPS-12"
    python -m app.admin demote-operator --email ops@example.org --reason "role change"
    python -m app.admin list-operators
    python -m app.admin reconcile-artifacts [--apply] [--grace-minutes 60]
    python -m app.admin set-schedule --connector NAME (--interval-minutes N | --disable)
    python -m app.admin health-report   # exit code 3 when any source needs attention
    python -m app.admin send-health-alerts   # exit code 4 when a delivery failed
    python -m app.admin register-ashby --name N --board B --employer E --basis official_api \
        --evidence-ref REF --reviewed-by WHO --terms-version V [--approve] [--owner-email X]
    python -m app.admin set-connector-enabled --connector N (--enable | --disable) --reason R
"""

import argparse
import getpass
import json
import sys
from dataclasses import asdict
from datetime import timedelta

from sqlalchemy import select

from app.core.config import get_settings
from app.db.models import SourceConnector
from app.db.session import SessionLocal
from app.services.artifact_lifecycle import reconcile_artifacts
from app.services.operators import RoleChangeError, list_operators, set_user_role
from app.services.sources.alerts import send_health_alerts
from app.services.sources.connectors import (
    ConnectorRegistrationError,
    register_ashby_connector,
    set_connector_enabled,
)
from app.services.sources.health import connector_health_report
from app.services.sources.scheduler import ScheduleError, set_connector_schedule
from app.services.storage import ArtifactStorage


def _actor() -> str:
    try:
        return f"cli:{getpass.getuser()}"[:64]
    except (KeyError, OSError):
        return "cli:unknown"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.admin")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("promote-operator", "demote-operator"):
        command = commands.add_parser(name)
        command.add_argument("--email", required=True)
        command.add_argument("--reason", required=True)
    commands.add_parser("list-operators")
    reconcile = commands.add_parser("reconcile-artifacts")
    reconcile.add_argument("--apply", action="store_true", help="Delete what is found")
    reconcile.add_argument("--grace-minutes", type=int, default=60)
    schedule = commands.add_parser("set-schedule")
    schedule.add_argument("--connector", required=True, help="Connector name")
    interval = schedule.add_mutually_exclusive_group(required=True)
    interval.add_argument("--interval-minutes", type=int)
    interval.add_argument("--disable", action="store_true")
    commands.add_parser("health-report")
    commands.add_parser("send-health-alerts")
    ashby = commands.add_parser("register-ashby")
    for flag in ("--name", "--board", "--employer", "--basis", "--evidence-ref",
                 "--reviewed-by", "--terms-version"):
        ashby.add_argument(flag, required=True)
    ashby.add_argument("--owner-email")
    ashby.add_argument("--no-compensation", action="store_true")
    ashby.add_argument("--approve", action="store_true", help="Record policy approval")
    toggle = commands.add_parser("set-connector-enabled")
    toggle.add_argument("--connector", required=True)
    state = toggle.add_mutually_exclusive_group(required=True)
    state.add_argument("--enable", action="store_true")
    state.add_argument("--disable", action="store_true")
    toggle.add_argument("--reason", required=True)
    args = parser.parse_args(argv)

    with SessionLocal() as db:
        if args.command in {"promote-operator", "demote-operator"}:
            role = "operator" if args.command == "promote-operator" else "candidate"
            try:
                user = set_user_role(
                    db, email=args.email, role=role, actor=_actor(), reason=args.reason
                )
            except RoleChangeError as exc:
                db.rollback()
                print(f"error: {exc}", file=sys.stderr)
                return 1
            db.commit()
            print(json.dumps({"email": user.email, "role": user.role}))
        elif args.command == "list-operators":
            print(json.dumps([user.email for user in list_operators(db)]))
        elif args.command == "set-schedule":
            connector = db.scalar(
                select(SourceConnector).where(SourceConnector.name == args.connector)
            )
            if connector is None:
                print("error: connector not found", file=sys.stderr)
                return 1
            try:
                set_connector_schedule(
                    db,
                    connector,
                    interval_seconds=None if args.disable else args.interval_minutes * 60,
                    actor=_actor(),
                    user_id=None,
                )
            except ScheduleError as exc:
                db.rollback()
                print(f"error: {exc}", file=sys.stderr)
                return 1
            db.commit()
            print(
                json.dumps(
                    {
                        "connector": connector.name,
                        "interval_seconds": connector.schedule_interval_seconds,
                    }
                )
            )
        elif args.command == "register-ashby":
            try:
                connector = register_ashby_connector(
                    db,
                    name=args.name,
                    board_name=args.board,
                    employer_name=args.employer,
                    basis=args.basis,
                    evidence_ref=args.evidence_ref,
                    reviewed_by=args.reviewed_by,
                    terms_version=args.terms_version,
                    include_compensation=not args.no_compensation,
                    owner_email=args.owner_email,
                    approve=args.approve,
                    actor=_actor(),
                )
            except ConnectorRegistrationError as exc:
                db.rollback()
                print(f"error: {exc}", file=sys.stderr)
                return 1
            db.commit()
            print(json.dumps({"connector_id": connector.id, "name": connector.name,
                              "enabled": connector.enabled,
                              "policy_status": connector.policy_status}))
        elif args.command == "set-connector-enabled":
            connector = db.scalar(
                select(SourceConnector).where(SourceConnector.name == args.connector)
            )
            if connector is None:
                print("error: connector not found", file=sys.stderr)
                return 1
            try:
                set_connector_enabled(
                    db, connector, enabled=args.enable, actor=_actor(), reason=args.reason
                )
            except ConnectorRegistrationError as exc:
                db.rollback()
                print(f"error: {exc}", file=sys.stderr)
                return 1
            db.commit()
            print(json.dumps({"connector": connector.name, "enabled": connector.enabled}))
        elif args.command == "health-report":
            connectors = db.scalars(select(SourceConnector).order_by(SourceConnector.name)).all()
            health = connector_health_report(db, connectors)
            print(json.dumps(health, default=str, sort_keys=True))
            if any(item["alert"] for item in health):
                return 3
        elif args.command == "send-health-alerts":
            if not get_settings().alerting_enabled:
                print("error: ALERT_WEBHOOK_REF/ALERT_WEBHOOK_HOST not set", file=sys.stderr)
                return 1
            outcomes = send_health_alerts(db)
            print(json.dumps([asdict(item) for item in outcomes], sort_keys=True))
            if any(not item.delivered for item in outcomes):
                return 4
        else:
            report = reconcile_artifacts(
                db,
                ArtifactStorage(get_settings()),
                apply=args.apply,
                grace=timedelta(minutes=args.grace_minutes),
                actor=_actor(),
            )
            print(json.dumps(report.summary(), sort_keys=True))
            if report.missing_files:
                return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
