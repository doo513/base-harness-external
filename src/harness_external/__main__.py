"""JSON CLI surface for external agent tools; no model or UI dependencies."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
import sqlite3

from harness.json_codec import MeasurementProtocolError, parse_message, render_message
from . import API_VERSION, ASSURANCE
from .errors import HarnessError
from .artifact_io import read_bytes


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
            data = read_bytes(Path(path), 256 * 1024)
        except (OSError, HarnessError) as error:
            if isinstance(error, HarnessError) and error.code == "ARTIFACT_LIMIT":
                raise HarnessError("INPUT_TOO_LARGE", "JSON input exceeds 256 KiB") from error
            raise HarnessError("JSON_FILE_ERROR", "Cannot read JSON file; use a file path, '-' for stdin, or json: followed by JSON") from error
    if len(data) > 256 * 1024:
        raise HarnessError("INPUT_TOO_LARGE", "JSON input exceeds 256 KiB")
    try:
        return parse_message(data)
    except (ValueError, UnicodeError) as error:
        raise HarnessError("JSON_INVALID", "Input must be UTF-8 JSON with unique keys and finite numbers") from error


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="External Develop Harness — local advisory reports, never Ready attestations")
    parser.add_argument("--state-dir", help="independent persistent store; must be outside the workspace")
    parser.add_argument("--policy-root", help="operator-configured acceptance policy registry, outside the workspace")
    parser.add_argument("--task-id", help="explicit task store selector; omission preserves the legacy store")
    commands = parser.add_subparsers(dest="command", required=True)
    describe = commands.add_parser("describe", help="read Domain and registered adapter input schemas without starting a Run")
    describe.add_argument("--domain-id", default="develop")
    describe.add_argument("--adapter-id")
    describe.add_argument("--operation")
    server = commands.add_parser("serve", help="serve existing Harness requests outside a restricted Host; no MCP or model runtime")
    server.add_argument("--socket", required=True, help="Unix socket in a private runtime directory, outside workspaces")
    server.add_argument("--workspace-root", action="append", required=True, help="explicit allowed workspace root; repeat for multiple roots")
    server.add_argument("--client-uid", action="append", type=int, help="allowed local client UID; repeat when the server uses a protected service account")
    invoked = commands.add_parser("invoke", help="one guarded caller envelope; no separate resume preflight")
    invoked.add_argument("--request", required=True, help=JSON_HELP)
    start = commands.add_parser("start")
    start.add_argument("--domain", default="develop")
    start.add_argument("--goal", required=True)
    start.add_argument("--sources", help=JSON_HELP + "; original source documents: [{id,label,text}]")
    start.add_argument("--workspace", required=True)
    start.add_argument("--parameters", help=JSON_HELP + "; inputs/artifacts/expectations/execution_checks/profile; optional requirements/coverage")
    start.add_argument("--analysis-selection", help=JSON_HELP + "; optional Domain advice view, separate from contract parameters")
    start.add_argument("--mode", choices=("strict", "exploratory", "acceptance"), help="default: configured acceptance when present, otherwise legacy strict")
    start.add_argument("--policy-id", help="registered acceptance profile; cannot override an operator Domain default")
    start.add_argument("--predecessor-run-id", help="terminal Run replaced after an explicit policy change")
    start.add_argument("--policy-change-reason", help="why a new policy/Run is needed; preserves predecessor history")
    start.add_argument("--continuation-reason", help="explicit runtime transition from a terminal predecessor; does not inherit passing evidence")
    start.add_argument("--capture-baseline", action="store_true", help="capture the declared pre-edit input scope for optional paired verification")
    start.add_argument("--required-check", action="append", default=[], help="initial explicit gate check ID; exploratory mode only")
    start.add_argument("--deferred-check", action="append", default=[], help="explicitly required check to be defined later; exploratory mode only")
    start.add_argument("--provenance", help=JSON_HELP + "; declared_author and optional unverified approval_reference")
    start.add_argument("--constraints", help=JSON_HELP + "; string array of original constraints")
    start.add_argument("--workspace-constraints", help=JSON_HELP + "; explicit unchanged_files with original source_refs")
    start.add_argument("--budget", help=JSON_HELP + "; max_actions/max_verifications/timeout_seconds")
    doctor = commands.add_parser("doctor")
    doctor.add_argument("--sandbox", action="store_true", help="run a controlled command through the strict Sandbox")
    listing = commands.add_parser("list-runs")
    listing.add_argument("--offset", type=int, default=0)
    listing.add_argument("--limit", type=int, default=20)
    tasks = commands.add_parser("list-tasks", help="bounded task connection metadata; never chooses a Run")
    tasks.add_argument("--workspace")
    tasks.add_argument("--domain")
    tasks.add_argument("--offset", type=int, default=0)
    tasks.add_argument("--limit", type=int, default=20)
    publish = commands.add_parser("knowledge-publish", help="publish selected advice from an explicitly closed Run")
    publish.add_argument("--run-id", required=True)
    publish.add_argument("--data", required=True, help=JSON_HELP)
    publish.add_argument("--request-id", required=True)
    search = commands.add_parser("knowledge-find", help="find short advisory document references")
    search.add_argument("--query", default="")
    search.add_argument("--domain")
    search.add_argument("--offset", type=int, default=0)
    search.add_argument("--limit", type=int, default=10)
    read = commands.add_parser("knowledge-read", help="read one selected advisory document")
    read.add_argument("--entry-id", required=True)
    for name in ("observe", "revise", "check", "retire-check", "assess", "submit", "verify", "compare", "cancel", "status", "resume", "records", "cleanup", "finish", "context"):
        cmd = commands.add_parser(name)
        cmd.add_argument("--run-id", required=True)
        if name in {"observe", "revise", "check", "retire-check", "assess"}:
            cmd.add_argument("--data", required=True, help=JSON_HELP)
        if name == "status":
            cmd.add_argument("--job-id")
            cmd.add_argument("--check-workspace", action="store_true")
            cmd.add_argument("--view", choices=("summary", "compact", "full", "attention"), default="summary", help="attention joins current issues and evidence; compact for polling; full includes history")
            cmd.add_argument("--attention-limit", type=int, default=20)
        elif name == "verify":
            cmd.add_argument("--compare-baseline", action="store_true", help="measure the pinned original and current Candidate with identical checks")
            cmd.add_argument("--expected-candidate-hash", help="reject a concurrent or superseding submission")
        elif name == "compare":
            cmd.add_argument("--reference", choices=("baseline",), default="baseline")
            cmd.add_argument("--expected-candidate-hash", required=True)
            cmd.add_argument("--parameters", required=True, help=JSON_HELP + "; Domain-owned comparison selection")
        elif name == "cancel":
            cmd.add_argument("--job-id", required=True)
        elif name == "records":
            cmd.add_argument("--kind", choices=("checks", "interpretations", "activity", "assessments", "tasks", "notes", "needs", "jobs", "measurements", "cases"), required=True)
            cmd.add_argument("--job-id")
            cmd.add_argument("--offset", type=int, default=0)
            cmd.add_argument("--limit", type=int, default=20)
            cmd.add_argument("--view", choices=("full", "references"), default="full", help="references is a bounded lossless projection")
            cmd.add_argument("--detail", help=JSON_HELP + "; digest-bound detail reference and optional byte offset/limit")
            cmd.add_argument("--outcomes", help=JSON_HELP + "; cases only: observed outcome strings to include")
            cmd.add_argument("--subject-role", choices=("candidate", "baseline"), help="cases only: filter the measured subject")
        elif name == "resume":
            cmd.add_argument("--view", choices=("summary", "compact", "attention"), default="summary")
            cmd.add_argument("--attention-limit", type=int, default=20)
            cmd.add_argument("--analysis-selection", help=JSON_HELP + "; select stored Domain advice without revising the Run")
        elif name == "cleanup":
            cmd.add_argument("--apply", action="store_true", help="quarantine abandoned captures; default is preview")
            cmd.add_argument("--min-age-seconds", type=int, default=3600)
        elif name == "finish":
            cmd.add_argument("--outcome", choices=("completed", "partial", "abandoned"), required=True)
            cmd.add_argument("--summary", default="")
            cmd.add_argument("--assessment", help=JSON_HELP + "; final caller assessment")
        elif name == "context":
            cmd.add_argument("--after", help="opaque cursor from the last complete context transfer")
            cmd.add_argument("--page", help="opaque continuation for the same immutable snapshot")
            cmd.add_argument("--limit", type=int, default=20)
        if name not in {"status", "resume", "records", "context"}:
            cmd.add_argument("--request-id", required=True, help="stable idempotency key; do not reuse for a different action")
    start.add_argument("--request-id", required=True)
    args = parser.parse_args(argv)
    try:
        if sum(getattr(args, key, None) == "-" for key in ("data", "parameters", "budget", "constraints", "workspace_constraints", "assessment", "provenance", "detail", "analysis_selection", "outcomes", "sources")) > 1:
            raise HarnessError("JSON_STDIN_REUSED", "Only one JSON input may consume stdin per invocation")
        from .service import Harness
        api = Harness(args.state_dir, policy_root=args.policy_root)
        if args.task_id and args.command == "serve":
            raise HarnessError("TASK_ARGUMENT", "The server routes tasks per request, not by changing its configured root")
        from .application_schema import OPTIONS as APP_OPTIONS
        if args.command != "invoke" and (args.task_id or args.command in APP_OPTIONS):
            from .dispatch import invoke
            from .operation_schema import OPTIONS
            from .task_storage import TaskStores
            arguments = {}
            for key, kind in OPTIONS[args.command].items():
                value = getattr(args, key, None)
                if value is not None:
                    arguments[key] = load_json(value) if kind == "json" else value
            envelope = {"operation": args.command, "arguments": arguments}
            if args.task_id:
                envelope["task_id"] = args.task_id
            if getattr(args, "request_id", None):
                envelope["request_id"] = args.request_id
            if "run_id" in arguments:
                selected = TaskStores(api).selected(args.task_id) if args.task_id else api
                with selected.store.transaction(write=False) as connection:
                    current = selected.store.run(connection, args.run_id)
                    envelope["context"] = {"workspace": current["workspace"], "domain": current["contract"]["domain_id"]}
            result = invoke(api, envelope)
            print(render_message({"ok": True, **result}).decode("utf-8"))
            return 0
        if args.command == "serve":
            from .local_transport import serve
            serve(api, args.socket, args.workspace_root, peer_uids=set(args.client_uid) if args.client_uid else None)
            return 0
        if args.command == "describe":
            result = api.describe(args.domain_id, args.adapter_id, operation=args.operation)
        elif args.command == "doctor":
            from . import diagnostics
            from .service import response
            result = response(**diagnostics.doctor(api.store, sandbox=args.sandbox))
        elif args.command == "invoke":
            from .dispatch import invoke
            request = load_json(args.request)
            if args.task_id:
                if request.get("task_id", args.task_id) != args.task_id:
                    raise HarnessError("TASK_ARGUMENT", "CLI and request task selectors differ")
                request["task_id"] = args.task_id
            result = invoke(api, request)
        elif args.command == "context":
            result = api.context(args.run_id, after=args.after, page=args.page, limit=args.limit)
        elif args.command == "list-runs":
            result = api.list_runs(offset=args.offset, limit=args.limit)
        elif args.command == "resume":
            result = api.resume(args.run_id, view=args.view, analysis_selection=load_json(args.analysis_selection) if args.analysis_selection else None,
                                attention_limit=args.attention_limit)
        elif args.command == "records":
            result = api.records(args.run_id, args.kind, offset=args.offset, limit=args.limit, job_id=args.job_id,
                                 view=args.view, detail=load_json(args.detail) if args.detail else None,
                                 outcomes=load_json(args.outcomes) if args.outcomes else None, subject_role=args.subject_role)
        elif args.command == "cleanup":
            result = api.cleanup(args.run_id, args.request_id, apply=args.apply, min_age_seconds=args.min_age_seconds)
        elif args.command == "cancel":
            result = api.cancel(args.run_id, args.job_id, args.request_id)
        elif args.command == "retire-check":
            result = api.retire_check(args.run_id, load_json(args.data), args.request_id)
        elif args.command == "start":
            result = api.start(domain_id=args.domain, goal=args.goal, workspace=args.workspace,
                               parameters=load_json(args.parameters) if args.parameters else {}, budget=load_json(args.budget) if args.budget else None,
                               request_id=args.request_id, mode=args.mode, required_checks=args.required_check,
                               deferred_checks=args.deferred_check, provenance=load_json(args.provenance) if args.provenance else None,
                               constraints=load_json(args.constraints) if args.constraints else None,
                               policy_id=args.policy_id, predecessor_run_id=args.predecessor_run_id, policy_change_reason=args.policy_change_reason,
                               capture_baseline=args.capture_baseline, continuation_reason=args.continuation_reason,
                               analysis_selection=load_json(args.analysis_selection) if args.analysis_selection else None,
                               workspace_constraints=load_json(args.workspace_constraints) if args.workspace_constraints else None,
                               sources=load_json(args.sources) if args.sources else None)
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
            result = api.verify(args.run_id, args.request_id, compare_baseline=args.compare_baseline,
                                expected_candidate_hash=args.expected_candidate_hash)
        elif args.command == "compare":
            result = api.compare(args.run_id, args.request_id, reference=args.reference,
                                 expected_candidate_hash=args.expected_candidate_hash, parameters=load_json(args.parameters))
        elif args.command == "status":
            result = api.status(args.run_id, args.job_id, check_workspace=args.check_workspace, view=args.view, attention_limit=args.attention_limit)
        else:
            result = api.finish(args.run_id, args.request_id, outcome=args.outcome, summary=args.summary,
                                assessment=load_json(args.assessment) if args.assessment else None)
        print(render_message({"ok": True, **result}).decode("utf-8"))
        return 2 if args.command == "doctor" and not result["healthy"] else 0
    except (HarnessError, MeasurementProtocolError, OSError, ValueError, RecursionError, sqlite3.Error) as error:
        print(render_message({"ok": False, "api_version": API_VERSION, "assurance": ASSURANCE, "ready": False,
                               "error": error.as_dict() if isinstance(error, HarnessError) else
                                        {"code": getattr(error, "code", "HARNESS_ERROR"), "message": str(error)[:2000]}}).decode())
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
