#!/usr/bin/env python3
"""Thin, model-free caller for the existing Harness CLI (Python 3.11+)."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


MAX_INPUT = 256 * 1024
MAX_OUTPUT = 8 * 1024 * 1024
API_VERSION = "external-harness-v2"
MUTATIONS = {"start", "observe", "revise", "check", "retire-check", "assess", "submit", "verify", "cancel", "finish"}
# Only CLI syntax lives here. Domain payloads and policy semantics belong to Harness.
OPTIONS = {
    "start": {"domain": "str", "goal": "str", "workspace": "str", "mode": "str",
              "parameters": "json", "budget": "json", "constraints": "json", "provenance": "json",
              "required_check": "repeat", "deferred_check": "repeat"},
    "doctor": {"sandbox": "bool"},
    "list-runs": {"offset": "int", "limit": "int"},
    **{name: {"run_id": "str", "data": "json"} for name in ("observe", "revise", "check", "retire-check", "assess")},
    **{name: {"run_id": "str"} for name in ("submit", "verify", "resume")},
    "cancel": {"run_id": "str", "job_id": "str"},
    "status": {"run_id": "str", "job_id": "str", "check_workspace": "bool", "view": "str"},
    "records": {"run_id": "str", "kind": "str", "job_id": "str", "offset": "int", "limit": "int"},
    "cleanup": {"run_id": "str", "apply": "bool", "min_age_seconds": "int"},
    "finish": {"run_id": "str", "outcome": "str", "summary": "str", "assessment": "json"},
}
REQUIRED = {
    "start": {"domain", "goal", "workspace", "mode", "provenance"},
    **{name: {"run_id", "data"} for name in ("observe", "revise", "check", "retire-check", "assess")},
    "cancel": {"run_id", "job_id"}, "records": {"run_id", "kind"}, "finish": {"run_id", "outcome"},
}


class ClientError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def require(condition, code, message):
    if not condition:
        raise ClientError(code, message)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "JSON_INVALID", f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def reject_constant(value):
    raise ClientError("JSON_INVALID", f"Non-finite JSON number: {value}")


def decode(raw):
    return json.loads(raw, object_pairs_hook=unique_object, parse_constant=reject_constant)


def read_json(source):
    if source == "-":
        raw = sys.stdin.buffer.read(MAX_INPUT + 1)
    elif source.startswith("json:"):
        raw = source[5:].encode("utf-8")
    else:
        require(not source.lstrip().startswith(("{", "[")), "JSON_SOURCE", "Use a file path, '-', or the json: prefix.")
        with Path(source).open("rb") as stream:
            raw = stream.read(MAX_INPUT + 1)
    require(len(raw) <= MAX_INPUT, "INPUT_TOO_LARGE", "JSON input exceeds 256 KiB.")
    return decode(raw.decode("utf-8"))


def absolute_path(value):
    require(isinstance(value, str) and bool(value) and "\x00" not in value, "PATH_INVALID", "Provide a nonempty local path.")
    path = Path(value).expanduser()
    require(path.is_absolute(), "PATH_INVALID", "Use an absolute path in the Harness environment (WSL paths when using WSL).")
    # Do not resolve symlinks away: Core must still reject linked input/state paths.
    return Path(os.path.abspath(path))


def default_config_path():
    configured = os.environ.get("BASE_HARNESS_CLIENT_CONFIG")
    if configured:
        return Path(configured).expanduser()
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return root / "base-harness-external" / "client.json"


def validate_config(config):
    require(isinstance(config, dict) and set(config) == {"schema_version", "harness_repo", "state_dir", "state_lifetime"},
            "CONFIG_INVALID", "Config requires schema_version, harness_repo, state_dir and state_lifetime.")
    require(type(config["schema_version"]) is int and config["schema_version"] == 1, "CONFIG_VERSION", "Unsupported config schema.")
    repo, state = absolute_path(config["harness_repo"]), absolute_path(config["state_dir"])
    require((repo / "scripts" / "harness-tool").is_file(), "HARNESS_NOT_FOUND", "Select a Harness source checkout containing scripts/harness-tool.")
    require(config["state_lifetime"] in ("persistent", "ephemeral"), "CONFIG_INVALID", "Invalid state_lifetime.")
    temporary_roots = {Path(tempfile.gettempdir()).resolve(), Path("/tmp"), Path("/var/tmp")}
    require(config["state_lifetime"] == "ephemeral" or not any(state.resolve().is_relative_to(root) for root in temporary_roots),
            "EPHEMERAL_STATE", "Choose durable state storage; --ephemeral is only for disposable tests.")
    return {**config, "harness_repo": str(repo), "state_dir": str(state)}


def configure(path, repo, state, ephemeral=False):
    config = validate_config({"schema_version": 1, "harness_repo": repo, "state_dir": state,
                              "state_lifetime": "ephemeral" if ephemeral else "persistent"})
    path.parent.mkdir(parents=True, exist_ok=True)
    # Installation never replaces an existing caller's configuration.
    with path.open("x", encoding="utf-8") as stream:
        json.dump(config, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return {"ok": True, "config_path": str(path.absolute()), "config": config}


def cli_arguments(operation, arguments, json_directory=None):
    require(isinstance(operation, str) and operation in OPTIONS, "OPERATION_INVALID", "Unsupported Harness operation.")
    options = OPTIONS[operation]
    required = REQUIRED.get(operation, {"run_id"} if "run_id" in options else set())
    require(isinstance(arguments, dict) and required <= arguments.keys() and arguments.keys() <= options.keys(),
            "ARGUMENTS_INVALID", f"{operation}: allowed fields {sorted(options)}; required {sorted(required)}.")
    result = [operation]
    for key, value in arguments.items():
        kind, flag = options[key], "--" + key.replace("_", "-")
        if kind == "json":
            require(isinstance(value, (dict, list)), "ARGUMENTS_INVALID", f"{key} must be a JSON object/array, not a file path.")
            encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            require(len(encoded.encode("utf-8")) <= MAX_INPUT, "INPUT_TOO_LARGE", f"{key} exceeds the Harness JSON bound.")
            if json_directory is None:
                result.append(flag + "=json:" + encoded)
            else:
                # Linux limits a single argv element well below the JSON API bound.
                # Core reads these private transport files synchronously; they are
                # not a second state store or candidate input.
                path = Path(json_directory) / (key + ".json")
                path.write_text(encoded, encoding="utf-8")
                result.append(flag + "=" + str(path))
        elif kind == "bool":
            require(type(value) is bool, "ARGUMENTS_INVALID", f"{key} must be boolean.")
            if value:
                result.append(flag)
        elif kind == "repeat":
            require(isinstance(value, list) and all(isinstance(item, str) for item in value), "ARGUMENTS_INVALID", f"{key} must be a string array.")
            result.extend(flag + "=" + item for item in value)
        else:
            require(type(value) is (int if kind == "int" else str), "ARGUMENTS_INVALID", f"{key} must be {kind}.")
            result.append(flag + "=" + str(value))
    return result


class Client:
    def __init__(self, config, *, timeout=None):
        self.config = validate_config(config)
        require(timeout is None or 0 < timeout <= 3600, "TIMEOUT_INVALID", "Client timeout must be positive and at most 3600 seconds.")
        self.timeout = timeout

    def execute(self, operation, arguments, request_id=None):
        if operation not in {"start", "doctor"}:
            require((Path(self.config["state_dir"]) / "runs.sqlite3").is_file(), "STATE_NOT_FOUND",
                    "Configured state is missing; check storage/configuration. Do not silently start a replacement Run.")
        # No shell, no model calls, no worker loop, no automatic retry/cancellation.
        with tempfile.TemporaryDirectory(prefix="harness-client-json-") as transport, tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            argv = ["bash", str(Path(self.config["harness_repo"]) / "scripts" / "harness-tool"),
                    "--state-dir", self.config["state_dir"], *cli_arguments(operation, arguments, transport)]
            if request_id is not None:
                argv.append("--request-id=" + request_id)
            try:
                completed = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, timeout=self.timeout)
            except subprocess.TimeoutExpired as error:
                raise ClientError("CALL_OUTCOME_UNKNOWN", "CLI response timed out. A mutation may have committed and a verification worker may still run. Query status or replay the IDENTICAL request ID and payload; do not issue a new request automatically.") from error
            stdout.seek(0)
            raw = stdout.read(MAX_OUTPUT + 1)
            require(len(raw) <= MAX_OUTPUT, "RESPONSE_TOO_LARGE", "Use status summary or smaller records pages; mutation outcome may be unknown.")
            try:
                response = decode(raw.decode("utf-8"))
            except (ValueError, UnicodeError, ClientError) as error:
                raise ClientError("TRANSPORT_RESPONSE", "Harness did not return valid JSON. Check launcher/runtime; do not assume a mutating request failed to commit.") from error
        require(isinstance(response, dict) and response.get("api_version") == API_VERSION and type(response.get("ok")) is bool,
                "API_MISMATCH", "Expected the external-harness-v2 JSON API; check the configured checkout.")
        require(completed.returncode in (0, 2), "TRANSPORT_EXIT", "Unexpected CLI exit; mutation outcome may be unknown.")
        return response

    def call(self, request):
        require(isinstance(request, dict) and set(request) <= {"operation", "arguments", "request_id", "context"}
                and {"operation", "arguments"} <= request.keys(), "REQUEST_INVALID", "Use operation, arguments, optional request_id and context.")
        operation, arguments = request["operation"], request["arguments"]
        cli_arguments(operation, arguments)
        request_id = request.get("request_id")
        require((isinstance(request_id, str) and 0 < len(request_id) <= 128 and "\x00" not in request_id)
                if operation in MUTATIONS else request_id is None, "REQUEST_ID_REQUIRED",
                "Mutations need an explicit stable request_id; reads/cleanup do not accept one. Replay identical requests with the same ID.")
        if operation == "start":
            arguments = {**arguments, "workspace": str(absolute_path(arguments["workspace"]))}
        if "run_id" in OPTIONS[operation]:
            context = request.get("context")
            require(isinstance(context, dict) and set(context) == {"workspace", "domain"}
                    and isinstance(context["domain"], str), "CONTEXT_REQUIRED", "Run operations require context: {workspace: absolute path, domain: ID}.")
            workspace = str(absolute_path(context["workspace"]))
            resumed = self.execute("resume", {"run_id": arguments["run_id"]})
            if not resumed["ok"]:
                return resumed
            identity = resumed.get("run", {})
            require(identity.get("workspace") == workspace and identity.get("domain_id") == context["domain"],
                    "RUN_CONTEXT_MISMATCH", "Run belongs to another workspace/domain. Select the correct Run explicitly.")
            if operation == "resume":
                return resumed
        else:
            require("context" not in request, "REQUEST_INVALID", "Context is only for an existing Run.")
        return self.execute(operation, arguments, request_id)

    def find(self, workspace, domain, goal=None, max_pages=10):
        workspace = str(absolute_path(workspace))
        require(type(max_pages) is int and 1 <= max_pages <= 100, "PAGE_LIMIT", "max_pages must be 1..100.")
        offset, matches = 0, []
        for _ in range(max_pages):
            page = self.execute("list-runs", {"offset": offset, "limit": 100})
            if not page["ok"]:
                return page
            matches.extend(item for item in page["items"] if item["workspace"] == workspace
                           and item["domain_id"] == domain and (goal is None or item["goal"] == goal))
            offset = page["next_offset"]
            if offset is None:
                break
        return {"ok": True, "source": "harness-client", "matches": matches, "truncated": offset is not None,
                "next_offset": offset, "selected_run_id": None,
                "note": "Discovery only: inspect original goal and lifecycle, then explicitly resume a Run. Never infer the newest Run is the right task."}


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ClientError("CLI_ARGUMENTS", message)


def main(argv=None):
    try:
        parser = Parser(description=__doc__)
        parser.add_argument("--config", type=Path, default=default_config_path(), help="one persistent caller config, independent of the workspace")
        parser.add_argument("--call-timeout", type=float, help="optional foreground CLI timeout; not the verification job budget")
        commands = parser.add_subparsers(dest="command", required=True)
        setup = commands.add_parser("configure", help="create a NEW config; never overwrite")
        setup.add_argument("--harness-repo", required=True)
        setup.add_argument("--state-dir", required=True)
        setup.add_argument("--ephemeral", action="store_true", help="explicitly disposable tests only")
        call = commands.add_parser("call", help="one request, no automatic action loop")
        call.add_argument("--request", required=True, help="JSON file path, '-' stdin, or json:{...}")
        find = commands.add_parser("find", help="list matching Runs without choosing/resuming one")
        find.add_argument("--workspace", required=True)
        find.add_argument("--domain", required=True)
        find.add_argument("--goal", help="optional exact original goal filter")
        find.add_argument("--max-pages", type=int, default=10)
        args = parser.parse_args(argv)
        if args.command == "configure":
            result = configure(args.config, args.harness_repo, args.state_dir, args.ephemeral)
        else:
            client = Client(read_json(str(args.config)), timeout=args.call_timeout)
            result = client.call(read_json(args.request)) if args.command == "call" else client.find(args.workspace, args.domain, args.goal, args.max_pages)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0 if result.get("ok") and result.get("healthy", True) else 2
    except (ClientError, OSError, ValueError, RecursionError) as error:
        print(json.dumps({"ok": False, "source": "harness-client", "error": {
            "code": getattr(error, "code", "CLIENT_INPUT_OR_IO"), "message": str(error)[:2000]}}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
