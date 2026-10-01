"""JSON CLI surface for external agent tools; no model or UI dependencies."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
import sqlite3

from harness.measurement_v5 import MeasurementProtocolError, decode
from harness.common import canonical_bytes
from . import API_VERSION, ASSURANCE
from .errors import HarnessError
from .service import Harness
from . import diagnostics, queries


JSON_HELP = "JSON file path, '-' for stdin, or 'json:{...}' for inline JSON"


def load_json(path: str):
    if path == "-":
        data = getattr(sys.stdin, "buffer", sys.stdin).read(256 * 1024 + 1)
        if isinstance(data, str):
            data = data.encode("utf-8")
    elif path.startswith("json:"):
        data = path[5:].encode("utf-8")
    else:
        if path.lstrip().startswith(("{", "[")):
            raise HarnessError("JSON_INPUT_SOURCE", "Inline JSON requires a json: prefix; alternatively use '-' for stdin or a file path")
        try:
            with Path(path).open("rb") as stream:
                data = stream.read(256 * 1024 + 1)
        except OSError as error:
            raise HarnessError("JSON_FILE_ERROR", "Cannot read JSON file; use a file path, '-' for stdin, or json: followed by JSON") from error
    if len(data) > 256 * 1024:
        raise HarnessError("INPUT_TOO_LARGE", "JSON input exceeds 256 KiB")
    try:
        return decode(data.decode("utf-8"))
    except (ValueError, UnicodeError) as error:
        raise HarnessError("JSON_INVALID", "Input must be UTF-8 JSON with unique keys and finite numbers") from error


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="External Develop Harness — local advisory reports, never Ready attestations")
    parser.add_argument("--state-dir", help="independent persistent store; must be outside the workspace")
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start")
    start.add_argument("--domain", default="develop")
    start.add_argument("--goal", required=True)
    start.add_argument("--workspace", required=True)
    start.add_argument("--parameters", help=JSON_HELP + "; inputs/artifacts/expectations/test_commands/profile")
    start.add_argument("--mode", choices=("strict", "exploratory"), default="strict")
    start.add_argument("--required-check", action="append", default=[], help="initial explicit gate check ID; exploratory mode only")
    start.add_argument("--deferred-check", action="append", default=[], help="explicitly required check to be defined later; exploratory mode only")
    start.add_argument("--provenance", help=JSON_HELP + "; declared_author and optional unverified approval_reference")
    start.add_argument("--constraints", help=JSON_HELP + "; string array of original constraints")
    start.add_argument("--budget", help=JSON_HELP + "; max_actions/max_verifications/timeout_seconds")
    doctor = commands.add_parser("doctor")
    doctor.add_argument("--sandbox", action="store_true", help="run a controlled command through the strict Sandbox")
    listing = commands.add_parser("list-runs")
    listing.add_argument("--offset", type=int, default=0)
    listing.add_argument("--limit", type=int, default=20)
    for name in ("observe", "revise", "check", "retire-check", "assess", "submit", "verify", "cancel", "status", "resume", "records", "cleanup", "finish"):
        cmd = commands.add_parser(name)
        cmd.add_argument("--run-id", required=True)
        if name in {"observe", "revise", "check", "retire-check", "assess"}:
            cmd.add_argument("--data", required=True, help=JSON_HELP)
        if name == "status":
            cmd.add_argument("--job-id")
            cmd.add_argument("--check-workspace", action="store_true")
            cmd.add_argument("--view", choices=("summary", "full"), default="summary", help="v2 defaults to bounded summary; request full history explicitly")
        elif name == "cancel":
            cmd.add_argument("--job-id", required=True)
        elif name == "records":
            cmd.add_argument("--kind", choices=(*queries.COLLECTIONS, "jobs", "measurements"), required=True)
            cmd.add_argument("--job-id")
            cmd.add_argument("--offset", type=int, default=0)
            cmd.add_argument("--limit", type=int, default=20)
        elif name == "cleanup":
            cmd.add_argument("--apply", action="store_true", help="quarantine abandoned captures; default is preview")
            cmd.add_argument("--min-age-seconds", type=int, default=3600)
        elif name == "finish":
            cmd.add_argument("--outcome", choices=("completed", "partial", "abandoned"), required=True)
            cmd.add_argument("--summary", default="")
            cmd.add_argument("--assessment", help=JSON_HELP + "; final caller assessment")
        if name not in {"status", "resume", "records", "cleanup"}:
            cmd.add_argument("--request-id", required=True, help="stable idempotency key; do not reuse for a different action")
    start.add_argument("--request-id", required=True)
    args = parser.parse_args(argv)
    try:
        if sum(getattr(args, key, None) == "-" for key in ("data", "parameters", "budget", "constraints", "assessment", "provenance")) > 1:
            raise HarnessError("JSON_STDIN_REUSED", "Only one JSON input may consume stdin per invocation")
        api = Harness(args.state_dir)
        if args.command == "doctor":
            from .service import response
            result = response(**diagnostics.doctor(api.store, sandbox=args.sandbox))
        elif args.command == "list-runs":
            result = api.list_runs(offset=args.offset, limit=args.limit)
        elif args.command == "resume":
            result = api.resume(args.run_id)
        elif args.command == "records":
            result = api.records(args.run_id, args.kind, offset=args.offset, limit=args.limit, job_id=args.job_id)
        elif args.command == "cleanup":
            result = api.cleanup(args.run_id, apply=args.apply, min_age_seconds=args.min_age_seconds)
        elif args.command == "cancel":
            result = api.cancel(args.run_id, args.job_id, args.request_id)
        elif args.command == "retire-check":
            result = api.retire_check(args.run_id, load_json(args.data), args.request_id)
        elif args.command == "start":
            result = api.start(domain_id=args.domain, goal=args.goal, workspace=args.workspace,
                               parameters=load_json(args.parameters) if args.parameters else {}, budget=load_json(args.budget) if args.budget else None,
                               request_id=args.request_id, mode=args.mode, required_checks=args.required_check,
                               deferred_checks=args.deferred_check, provenance=load_json(args.provenance) if args.provenance else None,
                               constraints=load_json(args.constraints) if args.constraints else None)
        elif args.command == "observe":
            result = api.observe(args.run_id, load_json(args.data), args.request_id)
        elif args.command == "revise":
            result = api.revise(args.run_id, load_json(args.data), args.request_id)
        elif args.command == "check":
            result = api.register_check(args.run_id, load_json(args.data), args.request_id)
        elif args.command == "assess":
            result = api.assess(args.run_id, load_json(args.data), args.request_id)
        elif args.command == "submit":
            result = api.submit(args.run_id, args.request_id)
        elif args.command == "verify":
            result = api.verify(args.run_id, args.request_id)
        elif args.command == "status":
            result = api.status(args.run_id, args.job_id, check_workspace=args.check_workspace, view=args.view)
        else:
            result = api.finish(args.run_id, args.request_id, outcome=args.outcome, summary=args.summary,
                                assessment=load_json(args.assessment) if args.assessment else None)
        print(canonical_bytes({"ok": True, **result}).decode())
        return 2 if args.command == "doctor" and not result["healthy"] else 0
    except (HarnessError, MeasurementProtocolError, OSError, ValueError, sqlite3.Error) as error:
        print(canonical_bytes({"ok": False, "api_version": API_VERSION, "assurance": ASSURANCE, "ready": False,
                               "error": {"code": getattr(error, "code", "HARNESS_ERROR"), "message": str(error)[:2000]}}).decode())
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
