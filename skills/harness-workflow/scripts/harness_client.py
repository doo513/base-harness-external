#!/usr/bin/env python3
"""Thin, model-free caller for the existing Harness CLI (Python 3.11+)."""
from __future__ import annotations

import argparse
import base64
import copy
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


MAX_INPUT = 256 * 1024
MAX_OUTPUT = 8 * 1024 * 1024
API_VERSION = "external-harness-v2"
MUTATIONS = {"start", "observe", "revise", "check", "retire-check", "assess", "submit", "verify", "cancel", "finish", "checkpoint"}
# Only CLI syntax lives here. Domain payloads and policy semantics belong to Harness.
OPTIONS = {
    "start": {"domain": "str", "goal": "str", "workspace": "str", "mode": "str",
              "parameters": "json", "budget": "json", "constraints": "json", "provenance": "json", "analysis_selection": "json",
              "required_check": "repeat", "deferred_check": "repeat", "policy_id": "str",
              "predecessor_run_id": "str", "policy_change_reason": "str", "continuation_reason": "str", "capture_baseline": "bool"},
    "invoke": {"request": "json"},
    "context": {"run_id": "str", "after": "str", "page": "str", "limit": "int"},
    "checkpoint": {"run_id": "str", "compare_baseline": "bool", "wait_seconds": "number"},
    "wait": {"run_id": "str", "job_id": "str", "wait_seconds": "number"},
    "doctor": {"sandbox": "bool"},
    "list-runs": {"offset": "int", "limit": "int"},
    **{name: {"run_id": "str", "data": "json"} for name in ("observe", "revise", "check", "retire-check", "assess")},
    **{name: {"run_id": "str"} for name in ("submit", "verify", "resume")},
    "resume": {"run_id": "str", "view": "str", "analysis_selection": "json"},
    "verify": {"run_id": "str", "compare_baseline": "bool", "expected_candidate_hash": "str"},
    "cancel": {"run_id": "str", "job_id": "str"},
    "status": {"run_id": "str", "job_id": "str", "check_workspace": "bool", "view": "str"},
    "records": {"run_id": "str", "kind": "str", "job_id": "str", "offset": "int", "limit": "int", "view": "str", "detail": "json", "outcomes": "json", "subject_role": "str"},
    "cleanup": {"run_id": "str", "apply": "bool", "min_age_seconds": "int"},
    "finish": {"run_id": "str", "outcome": "str", "summary": "str", "assessment": "json"},
}
REQUIRED = {
    "invoke": {"request"}, "wait": {"run_id", "job_id"},
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


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def restore_item(item, texts):
    """Validate a Core context entry before emitting a smaller delivery format."""
    require(isinstance(item, dict) and {"data", "digest", "text_refs"} <= item.keys() and isinstance(item["text_refs"], list),
            "CONTEXT_PROTOCOL", "Invalid context entry")
    value = copy.deepcopy(item["data"])
    for ref in item["text_refs"]:
        require(isinstance(ref, dict) and isinstance(ref.get("path"), list), "CONTEXT_PROTOCOL", "Invalid text reference")
        key, path = ref.get("text_ref"), ref["path"]
        require(isinstance(key, str) and key in texts and isinstance(texts[key], str) and digest(texts[key]) == key,
                "CONTEXT_CORRUPT", "Context text digest mismatch")
        if not path:
            require(value is None, "CONTEXT_CORRUPT", "Context reference did not name a placeholder")
            value = texts[key]
        else:
            target = value
            for part in path[:-1]:
                require(isinstance(target, dict) and type(part) is str and part in target or
                        isinstance(target, list) and type(part) is int and 0 <= part < len(target),
                        "CONTEXT_PROTOCOL", "Invalid context path")
                target = target[part]
            part = path[-1]
            require(isinstance(target, dict) and type(part) is str and part in target or
                    isinstance(target, list) and type(part) is int and 0 <= part < len(target),
                    "CONTEXT_PROTOCOL", "Invalid context path")
            require(target[part] is None, "CONTEXT_CORRUPT", "Context reference did not name a placeholder")
            target[part] = texts[key]
    require(digest(value) == item.get("digest"), "CONTEXT_CORRUPT", "Context entry digest mismatch")
    return value


def pack_context(document):
    """Reuse long strings across pages; short aliases are transport, not Core IDs."""
    aliases, texts, references = {}, {}, []
    def walk(value, path):
        if isinstance(value, str) and len(value) >= 80:
            if value not in aliases:
                name = "t" + str(len(aliases))
                aliases[value], texts[name] = name, value
            references.append({"path": path, "text_ref": aliases[value]})
            return None
        if isinstance(value, dict):
            return {key: walk(body, path + [key]) for key, body in value.items()}
        if isinstance(value, list):
            return [walk(body, path + [index]) for index, body in enumerate(value)]
        return value
    return {"data": walk(document, []), "texts": texts, "text_refs": references, "data_hash": digest(document)}


def expand_context(packet):
    """Optional machine decoder; the delivered data must equal all Core entries."""
    texts = packet["texts"]
    translated = {digest(value): value for value in texts.values()}
    item = {"data": packet["data"], "digest": packet["data_hash"], "text_refs": [
        {"path": ref["path"], "text_ref": digest(texts[ref["text_ref"]])} for ref in packet["text_refs"]]}
    return restore_item(item, translated)


ATTENTION_OUTCOMES = ["failed", "error", "skipped", "xfail", "xpass", "not_selected", "not_run", "incomplete"]


def attention_report(current, job, cases, *, limit=20):
    """Project observed exceptions, never infer root causes or goal satisfaction."""
    result = (job or {}).get("result") or {}
    linked = {}
    requirements = (result.get("requirement_observations") or {}).get("requirements", [])
    for requirement in requirements:
        for case in requirement["scenarios"]:
            linked.setdefault((case["check_id"], case["case_id"]), []).append(requirement["requirement_id"])
    items = []
    for record in cases.get("items", []):
        # Baseline records remain accessible but cannot become Candidate issues.
        if record.get("subject_role") != "candidate":
            continue
        case = record["case"]
        errors = []
        for index, phase in enumerate(case["phases"]):
            if isinstance(phase, dict) and isinstance(phase.get("message"), str) and phase["message"]:
                message = phase["message"]
                errors.append({"phase": phase.get("phase"), "outcome": phase.get("outcome"), "excerpt": message[:600],
                    "truncated": len(message) > 600, "detail": {"kind": "cases", "detail": {
                        "record_id": record["observation_id"], "record_digest": digest(record),
                        "path": ["case", "phases", index, "message"], "sha256": digest(message)}}})
            elif phase:
                errors.append({"phase": phase.get("phase") if isinstance(phase, dict) else index,
                    "detail": {"kind": "cases", "detail": {"record_id": record["observation_id"], "record_digest": digest(record),
                        "path": ["case", "phases", index], "sha256": digest(phase)}}})
        items.append({"kind": "case", "status": case["outcome"], "check_id": record["check_id"],
                      "case_id": case["case_id"], "requirement_ids": linked.get((record["check_id"], case["case_id"]), []),
                      "observation_id": record["observation_id"], "executed": case["executed"],
                      "finished": case["finished"], "errors": errors})
    seen = {(item.get("check_id"), item.get("case_id")) for item in items}
    for requirement in requirements:
        for case in requirement["scenarios"]:
            identity = (case["check_id"], case["case_id"])
            if case["status"] != "passed" and identity not in seen:
                seen.add(identity)
                items.append({"kind": "declared_scenario", "status": case["status"], "check_id": case["check_id"],
                              "case_id": case["case_id"], "requirement_ids": linked[identity],
                              "executed": case["executed"], "observation_id": case["observation_id"]})
    scope = result.get("measurement_scope") or {}
    for check in scope.get("observations", []):
        if check.get("comparison_status") != "passed":
            items.append({"kind": "check", **{key: check.get(key) for key in
                ("check_id", "comparison_status", "execution_status", "observation_id", "case_summary")}})
    if job and job.get("error"):
        items.append({"kind": "job_error", "error": job["error"]})
    returned = items[:limit]
    complete = bool(job and job["status"] not in {"queued", "running"}) and cases.get("next_offset") is None and len(returned) == len(items)
    payload = {"api_version": current["api_version"], "ok": True, "schema_version": "harness-client-attention-v1",
        "assurance": current["assurance"], "ready": current["ready"],
        "run_id": current.get("run_id") or current.get("run", {}).get("run_id"),
        **{key: current[key] for key in ("run", "intent_ref", "policy_ref", "candidate", "gates", "assessment", "limits", "usage", "details") if key in current},
        "measurement": current.get("measurement"),
        "attention": returned, "attention_page_complete": complete, "listed_attention_count": len(returned),
        "total_known_attention_count": len(items), "omitted_attention_count": len(items) - len(returned),
        "case_query": {"job_id": (job or {}).get("job_id"), "outcomes": ATTENTION_OUTCOMES,
                       "total": cases.get("total", 0), "next_offset": cases.get("next_offset")},
        "selected_job": {key: job.get(key) for key in ("job_id", "status", "candidate_hash", "check_set_hash", "completed_checks", "total_checks")} if job else None,
        "requirement_count": len(requirements),
        "known_gaps": [{"requirement_id": item["requirement_id"], "known_gap_ids": item["known_gap_ids"]} for item in requirements if item["known_gap_ids"]]}
    if job:
        payload["selected_job"]["result_status"] = result.get("status")
        measured = current.get("measurement")
        payload["selected_job"]["is_current_verification"] = measured.get("job_id") == job["job_id"] if measured is not None else None
        whole = digest(job)
        payload["job_detail"] = {"kind": "jobs", "detail": {"record_id": job["job_id"], "record_digest": whole, "path": [], "sha256": whole}}
        payload["selected_job"]["measurement_scope"] = {key: value for key, value in scope.items() if key != "observations"}
    if "wait_status" in current:
        payload["wait_status"] = current["wait_status"]
    if "current_workspace_matches_submitted_files" in current:
        payload["current_workspace_matches_submitted_files"] = current["current_workspace_matches_submitted_files"]
    payload["not_queried"] = [key for key in ("gates", "assessment", "limits", "usage") if key not in current]
    if "run" in payload:
        payload["run"] = {key: value for key, value in payload["run"].items() if key != "goal"}
    if payload["measurement"] is not None:
        measurement = payload["measurement"]
        payload["measurement"] = {key: value for key, value in measurement.items() if key not in {"measurement_scope", "baseline_comparison"}}
        payload["measurement"]["measurement_scope"] = {key: value for key, value in measurement.get("measurement_scope", {}).items() if key != "observations"}
    payload["context_operation"] = {"operation": "context", "arguments": {}}
    return payload


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
    required = {"schema_version", "harness_repo", "state_dir", "state_lifetime"}
    require(isinstance(config, dict) and required <= config.keys() <= required | {"policy_root"},
            "CONFIG_INVALID", "Config requires schema_version, harness_repo, state_dir and state_lifetime.")
    require(type(config["schema_version"]) is int and config["schema_version"] == 1, "CONFIG_VERSION", "Unsupported config schema.")
    repo, state = absolute_path(config["harness_repo"]), absolute_path(config["state_dir"])
    require((repo / "scripts" / "harness-tool").is_file(), "HARNESS_NOT_FOUND", "Select a Harness source checkout containing scripts/harness-tool.")
    require(config["state_lifetime"] in ("persistent", "ephemeral"), "CONFIG_INVALID", "Invalid state_lifetime.")
    temporary_roots = {Path(tempfile.gettempdir()).resolve(), Path("/tmp"), Path("/var/tmp")}
    require(config["state_lifetime"] == "ephemeral" or not any(state.resolve().is_relative_to(root) for root in temporary_roots),
            "EPHEMERAL_STATE", "Choose durable state storage; --ephemeral is only for disposable tests.")
    policy = {}
    if "policy_root" in config:
        root = absolute_path(config["policy_root"])
        require(root.is_dir(), "POLICY_ROOT_MISSING", "Configured operator policy directory must exist.")
        policy["policy_root"] = str(root)
    return {**config, "harness_repo": str(repo), "state_dir": str(state), **policy}


def configure(path, repo, state, ephemeral=False, policy_root=None):
    config = validate_config({"schema_version": 1, "harness_repo": repo, "state_dir": state,
                              "state_lifetime": "ephemeral" if ephemeral else "persistent",
                              **({"policy_root": policy_root} if policy_root is not None else {})})
    path.parent.mkdir(parents=True, exist_ok=True)
    # Installation never replaces an existing caller's configuration.
    with path.open("x", encoding="utf-8") as stream:
        json.dump(config, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
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
        elif kind == "number":
            validate_wait(value)
            result.append(flag + "=" + str(value))
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
        effective = arguments["request"]["operation"] if operation == "invoke" else operation
        if effective not in {"start", "doctor"}:
            require((Path(self.config["state_dir"]) / "runs.sqlite3").is_file(), "STATE_NOT_FOUND",
                    "Configured state is missing; check storage/configuration. Do not silently start a replacement Run.")
        # No shell, no model calls, no worker loop, no automatic retry/cancellation.
        with tempfile.TemporaryDirectory(prefix="harness-client-json-") as transport, tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            argv = ["bash", str(Path(self.config["harness_repo"]) / "scripts" / "harness-tool"),
                    "--state-dir", self.config["state_dir"],
                    *(["--policy-root", self.config["policy_root"]] if self.config.get("policy_root") else []),
                    *cli_arguments(operation, arguments, transport)]
            if request_id is not None:
                argv.append("--request-id=" + request_id)
            try:
                completed = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, timeout=self.timeout)
            except subprocess.TimeoutExpired as error:
                raise ClientError("CALL_OUTCOME_UNKNOWN", "CLI response timed out. A mutation may have committed and a verification worker may still run. Query status or replay the IDENTICAL request ID and payload; do not issue a new request automatically.") from error
            stdout.seek(0)
            raw = stdout.read(MAX_OUTPUT + 1)
            if operation == "invoke" and not raw and completed.returncode == 2:
                stderr.seek(0)
                diagnostic = stderr.read(8192)
                if b"invalid choice: 'invoke'" in diagnostic:
                    raise ClientError("CORE_UPGRADE_REQUIRED", "Selected Core lacks guarded invoke. Use a matching Core version; no legacy fallback was sent.")
            require(len(raw) <= MAX_OUTPUT, "RESPONSE_TOO_LARGE", "Use status summary or smaller records pages; mutation outcome may be unknown.")
            try:
                response = decode(raw.decode("utf-8"))
            except (ValueError, UnicodeError, ClientError) as error:
                raise ClientError("TRANSPORT_RESPONSE", "Harness did not return valid JSON. Check launcher/runtime; do not assume a mutating request failed to commit.") from error
        require(isinstance(response, dict) and response.get("api_version") == API_VERSION and type(response.get("ok")) is bool,
                "API_MISMATCH", "Expected the external-harness-v2 JSON API; check the configured checkout.")
        require(completed.returncode in (0, 2), "TRANSPORT_EXIT", "Unexpected CLI exit; mutation outcome may be unknown.")
        return response

    @contextmanager
    def serialized(self, key):
        """Transport mutex only: Core remains the authoritative concurrency guard."""
        require(os.name == "posix", "CALLER_LOCK_UNAVAILABLE", "This bash caller requires POSIX file locks (use WSL on Windows).")
        require(isinstance(key, str) and bool(key), "REQUEST_INVALID", "A request scope is required.")
        import fcntl
        root = Path(self.config["state_dir"]) / ".caller-locks"
        require(not any(p.is_symlink() for p in (root, *root.parents)), "CALLER_LOCK_PATH", "Caller lock directory must not use symlinks.")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        filename = root / (hashlib.sha256(key.encode()).hexdigest() + ".lock")
        descriptor = os.open(filename, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(descriptor, "rb+") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ClientError("CALLER_BUSY", "Another helper request owns this Run. No request was sent; retry the same saved request later.") from error
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def call(self, request):
        require(isinstance(request, dict), "REQUEST_INVALID", "Request must be an object.")
        cli_arguments(request.get("operation"), request.get("arguments"))
        if request["operation"] in MUTATIONS:
            if request["operation"] != "start":
                require((Path(self.config["state_dir"]) / "runs.sqlite3").is_file(), "STATE_NOT_FOUND",
                        "Configured state is missing; do not initialize a replacement store.")
            key = request["arguments"].get("run_id", "start")
            with self.serialized(key):
                return self._call(request)
        return self._call(request)

    def normalize_request(self, request):
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
            require(isinstance(context, dict) and {"workspace", "domain"} <= context.keys()
                    and context.keys() <= {"workspace", "domain", "intent_ref", "policy_ref"}
                    and isinstance(context["domain"], str), "CONTEXT_REQUIRED", "Run operations require context: {workspace: absolute path, domain: ID}.")
            workspace = str(absolute_path(context["workspace"]))
            request = {**request, "context": {**context, "workspace": workspace}}
        else:
            require("context" not in request, "REQUEST_INVALID", "Context is only for an existing Run.")
        return copy.deepcopy({**request, "arguments": arguments})

    def _call(self, request):
        request = self.normalize_request(request)
        result = self.execute("invoke", {"request": request})
        if not result["ok"] and result.get("error", {}).get("code") in {"RUN_CONTEXT_MISMATCH", "BINDING_IDENTITY_CHANGED"}:
            raise ClientError(result["error"]["code"], result["error"]["message"])
        return result

    def call_operation(self, operation, arguments, *, binding=None, action_id=None, output=None):
        """Save one explicit intent, then send it. No automatic ID change/retry."""
        request = self.normalize_request(self.make_request(operation, arguments, binding=binding, action_id=action_id)["request"])
        require(operation not in MUTATIONS or output is not None, "REQUEST_FILE_REQUIRED", "Mutations need --output to save their exact request before dispatch.")
        reference = {}
        if output is not None:
            path = Path(output).absolute()
            try:
                write_new_json(path, request)
            except FileExistsError:
                require(not path.is_symlink() and read_json(str(path)) == request,
                        "SAVED_REQUEST_CONFLICT", "Saved request differs; use its exact envelope for retransmission or a new file for a new intent.")
            reference = {"request_file": str(path), "request_id": request.get("request_id")}
        try:
            result = self.call(request)
        except (ClientError, OSError, ValueError) as error:
            error.saved_request = reference
            raise
        return {**result, **({"caller_request": reference} if reference else {})}

    def bind(self, run_id, workspace, domain, output):
        resumed = self.call({"operation": "resume", "arguments": {"run_id": run_id},
                             "context": {"workspace": workspace, "domain": domain}})
        if not resumed["ok"]:
            return resumed
        binding = {"schema_version": "harness-caller-binding-v1", "config": self.config,
                   "run_id": run_id, "workspace": resumed["run"]["workspace"], "domain": domain,
                   "intent_ref": resumed["intent"]["ref"], "policy_ref": resumed["policy"]["ref"]}
        write_new_json(output, binding)
        return {"ok": True, "binding_file": str(output), "binding": binding,
                "note": "A pinned reference, not a Run database; Harness is always revalidated."}

    def binding_context(self, binding):
        require(isinstance(binding, dict) and set(binding) == {"schema_version", "config", "run_id", "workspace", "domain", "intent_ref", "policy_ref"}
                and binding["schema_version"] == "harness-caller-binding-v1", "BINDING_INVALID", "Invalid caller binding.")
        require(binding["config"] == self.config, "BINDING_CONFIG_MISMATCH", "Binding belongs to another Harness/store configuration.")
        return {"workspace": str(absolute_path(binding["workspace"])), "domain": binding["domain"],
                "intent_ref": binding["intent_ref"], "policy_ref": binding["policy_ref"]}

    def validate_binding(self, binding):
        context = self.binding_context(binding)
        result = self._call({"operation": "resume", "arguments": {"run_id": binding["run_id"]},
                             "context": context})
        if result["ok"]:
            require(result["intent"]["ref"] == binding["intent_ref"] and result["policy"]["ref"] == binding["policy_ref"],
                    "BINDING_IDENTITY_CHANGED", "Run intent or policy differs from the selected binding.")
        return result

    def make_request(self, operation, arguments, *, action_id=None, binding=None):
        require(isinstance(arguments, dict), "ARGUMENTS_INVALID", "Arguments must be an object.")
        value = {"operation": operation, "arguments": dict(arguments)}
        if binding is not None:
            context = self.binding_context(binding)
            require("run_id" not in arguments, "BINDING_ARGUMENT_CONFLICT", "The binding supplies run_id.")
            value["arguments"]["run_id"] = binding["run_id"]
            value["context"] = context
        cli_arguments(operation, value["arguments"])
        if operation in MUTATIONS:
            value["request_id"] = action_key(binding, action_id)
        else:
            require(action_id is None, "REQUEST_ID_REQUIRED", "Read operations do not need an action ID.")
        return {"ok": True, "request": value}

    def checkpoint(self, binding, action_id, *, compare_baseline=False, wait_seconds=0):
        require(isinstance(binding, dict), "BINDING_INVALID", "Checkpoint requires an explicit Run binding.")
        require(type(compare_baseline) is bool, "ARGUMENTS_INVALID", "compare_baseline must be boolean.")
        validate_wait(wait_seconds)
        request = self.make_request("checkpoint", {"compare_baseline": compare_baseline, "wait_seconds": wait_seconds},
                                    action_id=action_id, binding=binding)["request"]
        return self.call(request)

    def wait(self, binding, job_id, *, wait_seconds=30, validated=False):
        validate_wait(wait_seconds)
        request = self.make_request("wait", {"job_id": job_id, "wait_seconds": wait_seconds}, binding=binding)["request"]
        return self.call(request)

    def context(self, binding, *, after=None, page=None, limit=20, complete=False, max_pages=64):
        args = {"limit": limit}
        if after is not None:
            args["after"] = after
        if page is not None:
            args["page"] = page
        if not complete:
            return self.call(self.make_request("context", args, binding=binding)["request"])
        require(page is None and type(max_pages) is int and 1 <= max_pages <= 1000,
                "CONTEXT_ARGUMENTS", "Complete transfer starts without --page and requires a bounded max_pages.")
        document, seen_pages, offset, raw_bytes, snapshot, delivery = {}, set(), 0, 0, None, None
        for number in range(max_pages):
            response = self.call(self.make_request("context", args, binding=binding)["request"])
            require(response.get("ok"), response.get("error", {}).get("code", "CONTEXT_FAILED"), response.get("error", {}).get("message", "Context failed"))
            require(response.get("schema_version") == "harness-context-v1" and response.get("run_id") == binding["run_id"]
                    and response.get("offset") == offset and isinstance(response.get("items"), list)
                    and isinstance(response.get("texts"), dict)
                    and {"snapshot", "delivery", "critical", "next_cursor", "reset_reason"} <= response.keys(),
                    "CONTEXT_PROTOCOL", "Invalid context page identity or offset")
            if number == 0:
                snapshot, delivery = response["snapshot"], response["delivery"]
            require(response["snapshot"] == snapshot and response["delivery"] == delivery,
                    "CONTEXT_CHANGED", "Context pages changed snapshot; discard this transfer")
            raw_bytes += len(canonical(response))
            require(raw_bytes <= MAX_OUTPUT, "CONTEXT_TOO_LARGE", "Transfer exceeds the helper bound; use explicit pages or a delta")
            for item in response["items"]:
                key = item["key"]
                require(isinstance(key, str) and key not in document, "CONTEXT_PROTOCOL", "Duplicate context key")
                document[key] = restore_item(item, response["texts"])
            offset += len(response["items"])
            token = response.get("next_page")
            if token is None:
                packed = pack_context(document)
                result = {key: response[key] for key in ("api_version", "ok", "assurance", "ready", "run_id", "delivery",
                                                        "snapshot", "critical", "next_cursor", "reset_reason")}
                if response.get("snapshot_critical") is not None:
                    result["snapshot_critical"] = response["snapshot_critical"]
                result.update(schema_version="harness-client-context-v1", **packed, page_count=number + 1)
                require(len(canonical(result)) <= MAX_OUTPUT, "CONTEXT_TOO_LARGE", "Assembled context exceeds the helper bound")
                return result
            require(isinstance(token, str) and token not in seen_pages and bool(response["items"]),
                    "CONTEXT_PROTOCOL", "Context paging did not progress")
            seen_pages.add(token)
            args = {"page": token, "limit": limit}
        raise ClientError("CONTEXT_PAGE_LIMIT", "Context transfer reached max_pages; no complete cursor was delivered")

    def resolve_detail(self, binding, arguments, *, max_chunks=1024):
        """Decode only an explicitly selected detail, not every record reference."""
        require(set(arguments) == {"kind", "detail"} and isinstance(arguments.get("detail"), dict), "DETAIL_ARGUMENTS", "Select only kind and detail explicitly")
        detail = copy.deepcopy(arguments["detail"])
        require(detail.get("offset", 0) == 0, "DETAIL_ARGUMENTS", "Complete detail resolution starts at offset zero")
        detail.setdefault("limit", 16384)
        value, total, expected = bytearray(), None, {key: detail[key] for key in ("record_id", "record_digest", "path", "sha256")}
        for _ in range(max_chunks):
            response = self.call(self.make_request("records", {"kind": arguments["kind"], "detail": {**detail, "offset": len(value)}}, binding=binding)["request"])
            require(response.get("ok"), response.get("error", {}).get("code", "DETAIL_FAILED"), response.get("error", {}).get("message", "Detail failed"))
            require(response.get("schema_version") == "record-detail-chunk-v1" and response.get("encoding") == "base64-canonical-json"
                    and response.get("run_id") == binding["run_id"] and response.get("kind") == arguments["kind"]
                    and all(response.get(key) == body for key, body in expected.items()) and response.get("offset") == len(value)
                    and type(response.get("total_bytes")) is int and 0 <= response["total_bytes"] <= MAX_OUTPUT,
                    "DETAIL_PROTOCOL", "Detail chunk changed identity or exceeded bounds")
            total = response["total_bytes"] if total is None else total
            require(total == response["total_bytes"], "DETAIL_PROTOCOL", "Detail total changed")
            chunk = base64.b64decode(response["data"], validate=True)
            value.extend(chunk)
            require(len(value) <= total, "DETAIL_PROTOCOL", "Detail chunk exceeds total")
            if response["next_offset"] is None:
                require(len(value) == total and hashlib.sha256(value).hexdigest() == expected["sha256"], "DETAIL_CORRUPT", "Detail digest or length mismatch")
                decoded = decode(value.decode("utf-8"))
                require(canonical(decoded) == bytes(value), "DETAIL_CORRUPT", "Detail is not canonical JSON")
                return {key: response[key] for key in ("api_version", "ok", "assurance", "ready", "run_id", "kind", "record_id", "record_digest", "path", "sha256")} | {
                    "schema_version": "harness-client-detail-v1", "value": decoded, "total_bytes": total}
            require(bool(chunk) and response["next_offset"] == len(value), "DETAIL_PROTOCOL", "Detail paging did not progress")
        raise ClientError("DETAIL_CHUNK_LIMIT", "Detail transfer reached max_chunks; no decoded value was delivered")

    def attention(self, response, binding, *, limit=20):
        require(type(limit) is int and 1 <= limit <= 100, "ATTENTION_ARGUMENTS", "Attention limit must be 1..100")
        if not response.get("ok"):
            return response
        run_id = response.get("run_id") or response.get("run", {}).get("run_id")
        require(run_id == binding["run_id"], "RUN_CONTEXT_MISMATCH", "Response belongs to another Run")
        require(response.get("view") in {"summary", "compact"}, "ATTENTION_ARGUMENTS", "Attention uses a summary or compact read response")
        job = response.get("job")
        if job is None:
            wanted = response.get("run", {}).get("active_job") or response.get("measurement", {}).get("job_id")
            job = next((item for item in response.get("recent_jobs", []) if item["job_id"] == wanted), None)
        if job and "requirement_count" in (job.get("result") or {}):
            full = self.call(self.make_request("status", {"job_id": job["job_id"], "view": "summary"}, binding=binding)["request"])
            require(full.get("ok"), full.get("error", {}).get("code", "ATTENTION_FAILED"), full.get("error", {}).get("message", "Job lookup failed"))
            job = full["job"]
        cases = {"items": [], "total": 0, "next_offset": None}
        result = (job or {}).get("result") or {}
        scopes = (result.get("measurement_scope") or {}).get("observations", [])
        case_checks = [item["case_summary"] for item in scopes if item.get("case_summary") is not None]
        nonpassing = any(count for summary in case_checks for state, count in summary.get("outcomes", {}).items() if state != "passed")
        if job and case_checks and (nonpassing or result.get("status") != "passed"):
            cases = self.call(self.make_request("records", {"kind": "cases", "job_id": job["job_id"],
                "outcomes": ATTENTION_OUTCOMES, "subject_role": "candidate", "limit": limit}, binding=binding)["request"])
            require(cases.get("ok"), cases.get("error", {}).get("code", "ATTENTION_FAILED"), cases.get("error", {}).get("message", "Case lookup failed"))
        return attention_report(response, job, cases, limit=limit)

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


def validate_wait(value):
    require(type(value) in (int, float) and 0 <= value <= 3600, "WAIT_INVALID", "Wait must be 0..3600 seconds.")


def action_key(binding, value):
    require(isinstance(value, str) and 0 < len(value) <= 128 and "\x00" not in value,
            "ACTION_ID_REQUIRED", "Give this intentional action a stable ID; reuse it only for retransmission.")
    require(binding is None or isinstance(binding, dict) and isinstance(binding.get("run_id"), str),
            "BINDING_INVALID", "A valid explicit binding is required.")
    scope = binding["run_id"] if binding is not None else "start"
    return "caller-" + hashlib.sha256((scope + "\x00" + value).encode()).hexdigest()


def write_new_json(path, value):
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


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
        setup.add_argument("--policy-root", help="operator-configured acceptance registry, not an editable task proposal")
        call = commands.add_parser("call", help="one request, no automatic action loop")
        source = call.add_mutually_exclusive_group(required=True)
        source.add_argument("--request", help="replay a saved JSON request: path, '-' stdin, or json:{...}")
        source.add_argument("--operation", choices=tuple(OPTIONS), help="construct and send one existing operation")
        call.add_argument("--binding", type=Path)
        call.add_argument("--arguments", default="json:{}", help="JSON arguments; binding supplies run_id")
        call.add_argument("--action-id", help="stable explicit intent; mutations only")
        call.add_argument("--output", type=Path, help="save the exact request before dispatch; required for a direct mutation")
        call.add_argument("--resolve-detail", action="store_true", help="decode only the selected records detail, starting at byte zero")
        call.add_argument("--format", choices=("raw", "attention"), default="raw", help="attention is a bounded view for status/resume responses")
        call.add_argument("--attention-limit", type=int, default=20)
        find = commands.add_parser("find", help="list matching Runs without choosing/resuming one")
        find.add_argument("--workspace", required=True)
        find.add_argument("--domain", required=True)
        find.add_argument("--goal", help="optional exact original goal filter")
        find.add_argument("--max-pages", type=int, default=10)
        bind = commands.add_parser("bind", help="save explicit Run/context references, never choose the newest Run")
        bind.add_argument("--run-id", required=True)
        bind.add_argument("--workspace", required=True)
        bind.add_argument("--domain", required=True)
        bind.add_argument("--output", type=Path, required=True)
        prepare = commands.add_parser("request", help="serialize a request once; resend the saved envelope after response loss")
        prepare.add_argument("--binding", type=Path)
        prepare.add_argument("--operation", required=True, choices=tuple(OPTIONS))
        prepare.add_argument("--arguments", default="json:{}", help="JSON arguments; the binding supplies Run context")
        prepare.add_argument("--action-id")
        prepare.add_argument("--output", type=Path, required=True)
        checkpoint = commands.add_parser("checkpoint", help="submit + verify, with no assessment, repair loop or finish")
        checkpoint.add_argument("--binding", type=Path, required=True)
        checkpoint.add_argument("--action-id", required=True)
        checkpoint.add_argument("--compare-baseline", action="store_true")
        checkpoint.add_argument("--wait-seconds", type=float, default=30)
        wait = commands.add_parser("wait", help="poll one existing job; timeout means pending, never failed")
        wait.add_argument("--binding", type=Path, required=True)
        wait.add_argument("--job-id", required=True)
        wait.add_argument("--wait-seconds", type=float, default=30)
        context = commands.add_parser("context", help="first restore without a cursor; then request explicit deltas")
        context.add_argument("--binding", type=Path, required=True)
        context.add_argument("--after")
        context.add_argument("--page")
        context.add_argument("--limit", type=int, default=20)
        context.add_argument("--complete", action="store_true", help="validate and coalesce all pages with short, lossless text references")
        context.add_argument("--max-pages", type=int, default=64)
        commands.add_parser("preflight", help="measure actual strict Sandbox availability; no task run is created")
        args = parser.parse_args(argv)
        if args.command == "configure":
            result = configure(args.config, args.harness_repo, args.state_dir, args.ephemeral, args.policy_root)
        else:
            client = Client(read_json(str(args.config)), timeout=args.call_timeout)
            if args.command == "call":
                binding = read_json(str(args.binding)) if args.binding else None
                if args.request is not None:
                    require(args.action_id is None and args.output is None and args.arguments == "json:{}",
                            "CLI_ARGUMENTS", "Saved request replay cannot also construct an intent")
                    saved = client.normalize_request(read_json(args.request))
                    operation, arguments = saved["operation"], saved["arguments"]
                    if binding is not None:
                        require(saved.get("context") == client.binding_context(binding) and arguments.get("run_id") == binding["run_id"],
                                "RUN_CONTEXT_MISMATCH", "Saved request and binding differ")
                    result = None
                else:
                    operation, arguments = args.operation, read_json(args.arguments)
                    saved = None
                    result = None
                require(not args.resolve_detail or args.format == "raw" and operation == "records" and binding is not None
                        and args.action_id is None and args.output is None,
                        "CLI_ARGUMENTS", "Detail resolution requires records, a binding and raw format")
                require(args.format == "raw" or operation in {"status", "resume", "checkpoint", "wait"} and binding is not None,
                        "CLI_ARGUMENTS", "Attention requires an explicit bound read/checkpoint/wait")
                if args.resolve_detail:
                    detail_arguments = {key: value for key, value in arguments.items() if key != "run_id"}
                    result = client.resolve_detail(binding, detail_arguments)
                elif saved is not None:
                    result = client.call(saved)
                else:
                    result = client.call_operation(operation, arguments, binding=binding, action_id=args.action_id, output=args.output)
                if args.format == "attention":
                    reference = result.get("caller_request")
                    try:
                        result = client.attention(result, binding, limit=args.attention_limit)
                    except (ClientError, OSError, ValueError) as error:
                        error.saved_request = reference
                        raise
                    if reference:
                        result["caller_request"] = reference
            elif args.command == "find":
                result = client.find(args.workspace, args.domain, args.goal, args.max_pages)
            elif args.command == "bind":
                result = client.bind(args.run_id, args.workspace, args.domain, args.output)
            elif args.command == "request":
                result = client.make_request(args.operation, read_json(args.arguments), action_id=args.action_id,
                                             binding=read_json(str(args.binding)) if args.binding else None)
                if result["ok"]:
                    write_new_json(args.output, result["request"])
                    result = {"ok": True, "request_file": str(args.output), "request_id": result["request"].get("request_id")}
            elif args.command == "checkpoint":
                result = client.checkpoint(read_json(str(args.binding)), args.action_id,
                                           compare_baseline=args.compare_baseline, wait_seconds=args.wait_seconds)
            elif args.command == "wait":
                result = client.wait(read_json(str(args.binding)), args.job_id, wait_seconds=args.wait_seconds)
            elif args.command == "context":
                result = client.context(read_json(str(args.binding)), after=args.after, page=args.page, limit=args.limit,
                                        complete=args.complete, max_pages=args.max_pages)
            else:
                result = client.call({"operation": "doctor", "arguments": {"sandbox": True}})
        print(json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":")))
        return 0 if result.get("ok") and result.get("healthy", True) else 2
    except (ClientError, OSError, ValueError, RecursionError) as error:
        print(json.dumps({"ok": False, "source": "harness-client", "error": {
            "code": getattr(error, "code", "CLIENT_INPUT_OR_IO"), "message": str(error)[:2000]},
            **({"caller_request": error.saved_request} if getattr(error, "saved_request", None) else {})}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
