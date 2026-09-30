"""JSON CLI surface for external agent tools; no model or UI dependencies."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
import sqlite3

from harness.measurement_v5 import MeasurementProtocolError, decode
from harness.common import canonical_bytes
from . import API_VERSION, ASSURANCE
from .domain import HarnessError
from .service import Harness


def load_json(path: str) -> dict:
    with Path(path).open("rb") as stream:
        data = stream.read(256 * 1024 + 1)
    if len(data) > 256 * 1024:
        raise HarnessError("INPUT_TOO_LARGE", "JSON input exceeds 256 KiB")
    return decode(data.decode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="External Develop Harness — local advisory reports, never Ready attestations")
    parser.add_argument("--state-dir", help="independent persistent store; must be outside the workspace")
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start")
    start.add_argument("--domain", default="develop")
    start.add_argument("--goal", required=True)
    start.add_argument("--workspace", required=True)
    start.add_argument("--parameters", required=True, help="JSON file: inputs/artifacts/expectations/test_commands/profile")
    start.add_argument("--budget", help="optional JSON file with max_actions/max_verifications/timeout_seconds")
    for name in ("observe", "submit", "verify", "status", "finish"):
        cmd = commands.add_parser(name)
        cmd.add_argument("--run-id", required=True)
        if name == "observe":
            cmd.add_argument("--data", required=True, help="JSON file with an untrusted note and optional references")
        if name == "status":
            cmd.add_argument("--job-id")
            cmd.add_argument("--check-workspace", action="store_true")
        elif name == "finish":
            cmd.add_argument("--outcome", choices=("completed", "partial", "abandoned"), required=True)
            cmd.add_argument("--summary", default="")
        if name != "status":
            cmd.add_argument("--request-id", required=True, help="stable idempotency key; do not reuse for a different action")
    start.add_argument("--request-id", required=True)
    args = parser.parse_args(argv)
    try:
        api = Harness(args.state_dir)
        if args.command == "start":
            result = api.start(domain_id=args.domain, goal=args.goal, workspace=args.workspace,
                               parameters=load_json(args.parameters), budget=load_json(args.budget) if args.budget else None, request_id=args.request_id)
        elif args.command == "observe":
            result = api.observe(args.run_id, load_json(args.data), args.request_id)
        elif args.command == "submit":
            result = api.submit(args.run_id, args.request_id)
        elif args.command == "verify":
            result = api.verify(args.run_id, args.request_id)
        elif args.command == "status":
            result = api.status(args.run_id, args.job_id, check_workspace=args.check_workspace)
        else:
            result = api.finish(args.run_id, args.request_id, outcome=args.outcome, summary=args.summary)
        print(canonical_bytes({"ok": True, **result}).decode())
        return 0
    except (HarnessError, MeasurementProtocolError, OSError, ValueError, sqlite3.Error) as error:
        print(canonical_bytes({"ok": False, "api_version": API_VERSION, "assurance": ASSURANCE, "ready": False,
                               "error": {"code": getattr(error, "code", "HARNESS_ERROR"), "message": str(error)[:2000]}}).decode())
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
