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
import re
import subprocess
import socket
import stat
import sys
import tempfile
import time


MAX_INPUT = 256 * 1024
MAX_OUTPUT = 8 * 1024 * 1024
API_VERSION = "external-harness-v2"
_UNSET = object()
MUTATIONS = {"start", "observe", "revise", "check", "retire-check", "assess", "submit", "verify", "compare", "cancel", "cleanup", "finish", "checkpoint", "knowledge-publish", "report-publish"}
# Generated Core syntax, not another set of Domain/policy rules.
CATALOG = json.loads(Path(__file__).with_name("operation_catalog.json").read_text(encoding="utf-8"))
OPTIONS = CATALOG["options"]
REQUIRED = {key: set(value) for key, value in CATALOG["required"].items()}

# Executed by the operator-selected Python, before importing any Harness code.
# This is runtime identification, not a claim that installed packages are trusted.
_PYTHON_IDENTITY_CODE = """
import hashlib, json, pathlib, sys
def _runtime_hash(path):
    with open(path, 'rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()
_venv_config = pathlib.Path(sys.prefix) / 'pyvenv.cfg'
_runtime_identity = {
    'implementation': sys.implementation.name,
    'version': list(sys.version_info[:3]),
    'cache_tag': sys.implementation.cache_tag,
    'executable_sha256': _runtime_hash(sys.executable),
    'prefix': str(pathlib.Path(sys.prefix).resolve()),
    'base_prefix': str(pathlib.Path(sys.base_prefix).resolve()),
    'venv_config_sha256': _runtime_hash(_venv_config) if _venv_config.is_file() else None,
}
"""
_CHECKED_BOOTSTRAP = _PYTHON_IDENTITY_CODE + """
_source = sys.argv.pop(1)
_expected = json.loads(sys.argv.pop(1))
if _runtime_identity != _expected:
    print(json.dumps({'api_version':'external-harness-v2','ok':False,'source':'runtime-bootstrap',
        'error':{'code':'PYTHON_RUNTIME_CHANGED','message':'Configured Python identity changed; no Core operation was invoked.'}}))
    raise SystemExit(2)
sys.path.insert(0, _source)
"""
_PINNED_BOOTSTRAP = _CHECKED_BOOTSTRAP + """
from harness_external.__main__ import main
raise SystemExit(main())
"""
_EXPLORER_BOOTSTRAP = _CHECKED_BOOTSTRAP + """
from change_lens.__main__ import main
raise SystemExit(main())
"""


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


def response_error_code(value):
    error = value.get("error") if isinstance(value, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, str) else None


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


def compact_call_result(operation, result):
    """Drop repeated implementation manifests, never criteria or uncertainty."""
    if not result.get("ok"):
        return result
    require(operation in {"start", "finish"}, "COMPACT_OPERATION", "Compact call projection supports start/finish.")
    common = {key: copy.deepcopy(result[key]) for key in (
        "api_version", "ok", "assurance", "ready", "run_id", "task_id", "project_workspace", "caller_request", "binding_file", "transport", "verification_scope", "knowledge_publication", "run_report") if key in result}
    common.update(view="caller-compact", meaning="read_projection_not_a_complete_context_transfer")
    if operation == "start":
        contract = result["contract"]
        common.update(goal=contract["original_goal"], constraints=copy.deepcopy(result.get("constraints", [])),
                      contract_projection={key: copy.deepcopy(value) for key, value in contract.items()
                                           if key not in {"original_goal", "verifier", "source_domain"}},
                      **{key: copy.deepcopy(result[key]) for key in ("context", "policy", "phase", "limits", "questions", "domain_preparation", "acceptance", "workspace_constraints") if key in result},
                      details={"operation": "context", "run_id": result["run_id"], "contract_hash": contract["contract_hash"],
                               "omitted": ["contract.verifier", "contract.source_domain", "interpretation"]})
    else:
        record = result["record"]
        resolution = copy.deepcopy(result["resolution"])
        scope = resolution.get("measurement_scope", {})
        if "observations" in scope:
            scope["observation_count"] = len(scope.pop("observations"))
        common.update(completion={key: copy.deepcopy(record[key]) for key in (
            "outcome", "outcome_meaning", "lifecycle", "candidate_hash", "contract_hash", "record_hash",
            "gates", "assessment", "limitations", "unresolved_work", "unresolved_verification_jobs") if key in record},
            resolution=resolution, details={"operation": "context", "run_id": result["run_id"], "record_hash": record["record_hash"]})
    return common




def read_json(source):
    if source == "-":
        raw = sys.stdin.buffer.read(MAX_INPUT + 1)
    elif source.startswith("json:"):
        raw = source[5:].encode("utf-8")
    else:
        require(not source.lstrip().startswith(("{", "[")), "JSON_SOURCE", "Use a file path, '-', or the json: prefix.")
        raw = read_regular_file(Path(source), MAX_INPUT)
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


def validate_python_runtime(value):
    require(isinstance(value, dict) and set(value) == {"schema_version", "executable", "identity"}
            and value["schema_version"] == "caller-python-runtime-v1", "CONFIG_RUNTIME", "Invalid configured Python binding.")
    absolute_path(value["executable"])
    identity = value["identity"]
    require(isinstance(identity, dict) and set(identity) == {"implementation", "version", "cache_tag", "executable_sha256", "prefix", "base_prefix", "venv_config_sha256"},
            "CONFIG_RUNTIME", "Invalid Python identity fields.")
    require(isinstance(identity["version"], list) and len(identity["version"]) == 3
            and all(type(v) is int and 0 <= v <= 999 for v in identity["version"])
            and all(isinstance(identity[k], str) and 0 < len(identity[k]) <= 4096 for k in ("implementation", "cache_tag", "prefix", "base_prefix")),
            "CONFIG_RUNTIME", "Invalid Python version or implementation.")
    require(isinstance(identity["executable_sha256"], str) and re.fullmatch(r"[a-f0-9]{64}", identity["executable_sha256"])
            and (identity["venv_config_sha256"] is None or isinstance(identity["venv_config_sha256"], str)
                 and re.fullmatch(r"[a-f0-9]{64}", identity["venv_config_sha256"])), "CONFIG_RUNTIME", "Invalid Python file digest.")
    return copy.deepcopy(value)


def pin_python(repo, selected=None):
    default = Path(repo) / ".venv" / "bin" / "python"
    # Selection happens once during explicit configuration, never after a lost
    # runtime or request. Preserve a venv entry path instead of resolving it away.
    executable = absolute_path(selected or (str(default) if default.is_file() else sys.executable))
    require(executable.is_file() and os.access(executable, os.X_OK), "PYTHON_RUNTIME_UNAVAILABLE", "Selected Python executable is unavailable.")
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            process = subprocess.run([str(executable), "-I", "-c", _PYTHON_IDENTITY_CODE + "\nprint(json.dumps(_runtime_identity))"],
                                     stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, timeout=10)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ClientError("PYTHON_RUNTIME_UNAVAILABLE", "Selected Python probe failed; no fallback was attempted.") from error
        stdout.seek(0)
        raw = stdout.read(16385)
    require(process.returncode == 0 and len(raw) <= 16384, "PYTHON_RUNTIME_UNAVAILABLE", "Use a working Python 3.11+ executable.")
    try:
        identity = decode(raw.decode("utf-8"))
    except (ValueError, UnicodeError) as error:
        raise ClientError("PYTHON_RUNTIME_UNAVAILABLE", "Selected executable did not report a Python identity.") from error
    result = validate_python_runtime({"schema_version": "caller-python-runtime-v1", "executable": str(executable), "identity": identity})
    require(tuple(identity["version"]) >= (3, 11, 0), "PYTHON_RUNTIME_UNSUPPORTED", "Harness requires Python 3.11 or newer.")
    return result


def validate_config(config):
    required = {"schema_version", "harness_repo", "state_dir", "state_lifetime"}
    require(isinstance(config, dict) and required <= config.keys() <= required | {"policy_root", "server_socket", "python_runtime", "task_storage"},
            "CONFIG_INVALID", "Config requires schema_version, harness_repo, state_dir and state_lifetime.")
    require(type(config["schema_version"]) is int and config["schema_version"] == 1, "CONFIG_VERSION", "Unsupported config schema.")
    repo, state = absolute_path(config["harness_repo"]), absolute_path(config["state_dir"])
    endpoint = {}
    if "server_socket" in config:
        selected = absolute_path(config["server_socket"])
        require(os.name == "posix" and hasattr(socket, "AF_UNIX") and len(os.fsencode(selected)) <= 103,
                "TRANSPORT_PATH", "Use a POSIX local socket path of at most 103 bytes.")
        endpoint["server_socket"] = str(selected)
    else:
        require((repo / "scripts" / "harness-tool").is_file(), "HARNESS_NOT_FOUND", "Select a Harness source checkout containing scripts/harness-tool.")
    require(config["state_lifetime"] in ("persistent", "ephemeral"), "CONFIG_INVALID", "Invalid state_lifetime.")
    require(config.get("task_storage", "external") in ("external", "project"), "CONFIG_INVALID", "task_storage is project or external (legacy).")
    temporary_roots = {Path(tempfile.gettempdir()).resolve(), Path("/tmp"), Path("/var/tmp")}
    require(config["state_lifetime"] == "ephemeral" or not any(state.resolve().is_relative_to(root) for root in temporary_roots),
            "EPHEMERAL_STATE", "Choose durable state storage; --ephemeral is only for disposable tests.")
    policy = {}
    if "policy_root" in config:
        root = absolute_path(config["policy_root"])
        require(bool(endpoint) or root.is_dir(), "POLICY_ROOT_MISSING", "Configured operator policy directory must exist.")
        policy["policy_root"] = str(root)
    if "python_runtime" in config:
        validate_python_runtime(config["python_runtime"])
    return {**config, "harness_repo": str(repo), "state_dir": str(state), **policy, **endpoint}


def configure(path, repo, state, ephemeral=False, policy_root=None, server_socket=None, python_executable=None, task_storage="project"):
    config = validate_config({"schema_version": 1, "harness_repo": repo, "state_dir": state,
                              "state_lifetime": "ephemeral" if ephemeral else "persistent", "task_storage": task_storage,
                              **({"policy_root": policy_root} if policy_root is not None else {}),
                              **({"server_socket": server_socket} if server_socket is not None else {})})
    require(not server_socket or python_executable is None, "CONFIG_RUNTIME", "The external server owns its interpreter; do not select a local Python for socket transport.")
    if not server_socket:
        config["python_runtime"] = pin_python(config["harness_repo"], python_executable)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        require(path.parent.stat().st_uid == os.getuid(), "CONFIG_PERMISSIONS", "Config directory must be owned by the current user")
        path.parent.chmod(0o700)
    # Installation never replaces an existing caller's configuration.
    write_new_json(path, config, indent=2)
    return {"ok": True, "config_path": str(path.absolute()), "config": config}


def describe(operation=None):
    """Expose existing caller syntax without configuration, I/O or a Core call."""
    if operation is None:
        return {"ok": True, "operations": sorted(set(OPTIONS) - {"invoke"}),
                "detail": "describe --operation NAME", "domain_fields_are_opaque": True}
    require(operation in OPTIONS and operation != "invoke", "OPERATION_INVALID", "Select a public operation.")
    options = OPTIONS[operation]
    argv = ["python3", "<harness_client.py>", "--config", "<client.json>", "call", "--operation", operation,
            "--arguments", "json:<operation arguments only>"]
    if "run_id" in options:
        argv.extend(["--binding", "<binding.json>"])
    if operation in MUTATIONS:
        argv.extend(["--action-id", "<stable intent label>", "--output", "<saved-request.json>"])
    if operation == "start":
        argv.extend(["--binding-output", "<new-binding.json>", "--format", "compact"])
    return {"ok": True, "operation": operation, "mutates": operation in MUTATIONS, "call_argv_template": argv,
            "response_formats": ["raw", *(["compact"] if operation in {"start", "finish", "status", "resume"} else []),
                                 *(["attention"] if operation in {"status", "resume", "checkpoint", "wait"} else []),
                                 *(["brief"] if operation in {"checkpoint", "wait"} else [])],
            "input_schema": copy.deepcopy(CATALOG["operations"][operation]),
            "core_input_schema": copy.deepcopy(CATALOG["core_operations"][operation]),
            "source": "bundled_core_catalog", "catalog_hash": CATALOG["sha256"],
            "application_capabilities": CATALOG.get("application_capabilities", {}),
            "delivery": copy.deepcopy(CATALOG.get("delivery")),
            "field_input": "--text /field=value; --text-file /field=FILE; --json /field=JSON; --null /field; --from-record /field=RECEIPT[#/reference]",
            **({"optional_finish_knowledge": CATALOG["finish_knowledge_schema"]} if operation == "finish" else {}),
            "runtime_check": "describe --operation " + operation + " --runtime",
            "binding_supplies": ["run_id", "context"] if "run_id" in options else [],
            "request_rule": "explicit stable action-id and saved output request" if operation in MUTATIONS else "no action-id",
            "domain_fields_are_opaque": True}


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
    def __init__(self, config, *, timeout=None, trace_file=None):
        self.config = validate_config(config)
        require(timeout is None or 0 < timeout <= 3600, "TIMEOUT_INVALID", "Client timeout must be positive and at most 3600 seconds.")
        self.timeout = timeout
        self.trace_file = Path(os.path.abspath(trace_file)) if trace_file is not None else None
        self.cost_events = []
        self.trace_warnings = []

    def database_path(self, request):
        root = Path(self.config["state_dir"])
        if "project_workspace" in request:
            require("task_id" in request, "PROJECT_WORKSPACE", "A project route requires a task ID")
            project = absolute_path(request["project_workspace"])
            temporary_roots = {Path(tempfile.gettempdir()).resolve(), Path("/tmp"), Path("/var/tmp")}
            require(self.config["state_lifetime"] == "ephemeral" or not any(project.resolve().is_relative_to(p) for p in temporary_roots),
                    "EPHEMERAL_STATE", "Project-local records need a durable project; temporary projects require an explicit ephemeral test config.")
            root = project / ".harness"
        if "task_id" in request:
            task_id = request["task_id"]
            require(isinstance(task_id, str) and re.fullmatch(r"task_[a-f0-9]{32}", task_id),
                    "TASK_ID_INVALID", "Use the exact task ID returned by Harness.")
            root = root / "tasks" / task_id
        return root / "runs.sqlite3"

    def explore(self, arguments):
        """Host-local advisory tool: no Core transport, Run write or gate result."""
        require(isinstance(arguments, list) and len(arguments) <= 128 and all(isinstance(v, str) and len(v) <= 8192 and "\x00" not in v for v in arguments),
                "EXPLORATION_ARGUMENTS", "Use bounded Change Lens CLI arguments.")
        source = Path(self.config["harness_repo"]) / "src"
        require((source / "change_lens/__main__.py").is_file(), "EXPLORATION_UNAVAILABLE",
                "Install the optional explorer locally. An external server does not expose arbitrary workspace exploration.")
        pinned = self.config.get("python_runtime")
        if pinned is not None:
            executable = Path(pinned["executable"])
            require(executable.is_file() and os.access(executable, os.X_OK), "PYTHON_RUNTIME_UNAVAILABLE", "Pinned local Python is unavailable.")
            launcher = [str(executable), "-I", "-c", _EXPLORER_BOOTSTRAP, str(source), canonical(pinned["identity"]).decode("utf-8")]
        else:
            # This process is the Host exploration runtime, not a fallback Core.
            launcher = [sys.executable, "-I", "-c", "import sys; sys.path.insert(0,sys.argv.pop(1)); from change_lens.__main__ import main; raise SystemExit(main())", str(source)]
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            try:
                process = subprocess.run([*launcher, *(arguments or ["--help"])], stdin=subprocess.DEVNULL,
                                         stdout=stdout, stderr=stderr, timeout=self.timeout or 120)
            except subprocess.TimeoutExpired as error:
                raise ClientError("EXPLORATION_TIMEOUT", "Local exploration timed out; no Harness verification was started.") from error
            stdout.seek(0)
            stderr.seek(0)
            out, err = stdout.read(MAX_OUTPUT + 1), stderr.read(16385)
        require(len(out) <= MAX_OUTPUT and len(err) <= 16384, "EXPLORATION_OUTPUT_TOO_LARGE", "Use the bounded context view or a smaller file limit.")
        raw = out if process.returncode == 0 or out else err
        try:
            body = decode(raw.decode("utf-8"))
        except (ValueError, UnicodeError):
            body = {"text": raw.decode("utf-8", errors="replace")}
        return {"ok": process.returncode == 0, "tool": "change_lens", "gate_eligible": False,
                "data": body, "exit_code": process.returncode,
                "meaning": "host_exploration_not_verifier_evidence"}

    def execute(self, operation, arguments, request_id=None):
        if self.trace_file is None:
            return self._execute(operation, arguments, request_id)
        started = time.monotonic()
        envelope = arguments.get("request", {}) if operation == "invoke" else {"operation": operation, "arguments": arguments, "request_id": request_id}
        result, failure = None, None
        try:
            result = self._execute(operation, arguments, request_id)
            return result
        except Exception as error:
            failure = getattr(error, "code", type(error).__name__)
            raise
        finally:
            request_arguments = envelope.get("arguments", {})
            run = (result or {}).get("run")
            job = (result or {}).get("job")
            event = {"schema_version": "harness-client-cost-v1", "kind": "transport", "operation": envelope.get("operation"),
                     "request_id": envelope.get("request_id"),
                     "run_id": (result or {}).get("run_id") or (run.get("run_id") if isinstance(run, dict) else None) or request_arguments.get("run_id"),
                     "job_id": (result or {}).get("job_id") or (job.get("job_id") if isinstance(job, dict) else None),
                     "transport": "socket" if self.config.get("server_socket") else "cli",
                     "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
                     "request_bytes": len(canonical(envelope)), "response_bytes": len(canonical(result)) if result is not None else None,
                     "ok": result.get("ok") if result is not None else None,
                     "error_code": failure or response_error_code(result), "model_tokens": None}
            self.cost_events.append(event)
            if self.trace_file is not None:
                workspace = request_arguments.get("workspace") or envelope.get("context", {}).get("workspace")
                warning = "TRACE_INSIDE_WORKSPACE" if workspace and self.trace_file.is_relative_to(Path(os.path.abspath(workspace))) else append_cost(self.trace_file, event)
                if warning and warning not in self.trace_warnings:
                    self.trace_warnings.append(warning)

    def _execute(self, operation, arguments, request_id=None):
        if self.config.get("server_socket"):
            require(operation == "invoke" and request_id is None, "TRANSPORT_OPERATION", "Socket transport accepts guarded invoke envelopes only.")
            return self._socket_execute(arguments["request"])
        effective = arguments["request"]["operation"] if operation == "invoke" else operation
        envelope = arguments["request"] if operation == "invoke" else {}
        if effective not in {"start", "doctor", "describe", "list-tasks", "knowledge-find", "knowledge-read", "report-list", "report-read"}:
            require(self.database_path(envelope).is_file(), "STATE_NOT_FOUND",
                    "Configured state is missing; check storage/configuration. Do not silently start a replacement Run.")
        # No shell, no model calls, no worker loop, no automatic retry/cancellation.
        with tempfile.TemporaryDirectory(prefix="harness-client-json-") as transport, tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            pinned = self.config.get("python_runtime")
            if pinned is not None:
                executable = Path(pinned["executable"])
                require(executable.is_file() and os.access(executable, os.X_OK), "PYTHON_RUNTIME_UNAVAILABLE", "Pinned Python disappeared; no Core request or fallback was sent.")
                launcher = [str(executable), "-I", "-c", _PINNED_BOOTSTRAP, str(Path(self.config["harness_repo"]) / "src"), canonical(pinned["identity"]).decode("utf-8")]
            else:
                legacy_python = Path(self.config["harness_repo"]) / ".venv" / "bin" / "python"
                require(legacy_python.is_file() and os.access(legacy_python, os.X_OK), "PYTHON_RUNTIME_NOT_CONFIGURED",
                        "Legacy config requires its checkout .venv. Configure an explicit Python; no PATH fallback was attempted.")
                launcher = ["bash", str(Path(self.config["harness_repo"]) / "scripts" / "harness-tool")]
            argv = [*launcher, "--state-dir", self.config["state_dir"],
                    *(["--policy-root", self.config["policy_root"]] if self.config.get("policy_root") else []),
                    *cli_arguments(operation, arguments, transport)]
            if request_id is not None:
                argv.append("--request-id=" + request_id)
            try:
                environment = dict(os.environ)
                if pinned is None:
                    environment["BASE_HARNESS_PYTHON"] = str(legacy_python)
                completed = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, timeout=self.timeout, env=environment)
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
        if pinned is not None and response.get("source") == "runtime-bootstrap" and response_error_code(response) == "PYTHON_RUNTIME_CHANGED":
            raise ClientError("PYTHON_RUNTIME_CHANGED", "Configured Python changed; no Core operation was invoked. Restore it or explicitly configure a new runtime.")
        require(completed.returncode in (0, 2), "TRANSPORT_EXIT", "Unexpected CLI exit; mutation outcome may be unknown.")
        return response

    def _socket_execute(self, request):
        expected = {"state_dir": self.config["state_dir"], "policy_root": self.config.get("policy_root")}
        packet = canonical({"protocol": "harness-local-transport-v1", "configuration": expected, "request": request}) + b"\n"
        require(len(packet) <= MAX_INPUT, "INPUT_TOO_LARGE", "Socket request exceeds 256 KiB.")
        sent = False
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
                channel.settimeout(self.timeout)
                channel.connect(self.config["server_socket"])
                sent = True
                channel.sendall(packet)
                with channel.makefile("rb") as stream:
                    raw = stream.readline(MAX_OUTPUT + 1)
        except OSError as error:
            raise ClientError("CALL_OUTCOME_UNKNOWN" if sent else "TRANSPORT_UNAVAILABLE",
                              "Server response was lost; query the same Run or replay the IDENTICAL saved request. No automatic retry or local fallback occurred." if sent else
                              "Configured Harness server is unavailable. Start it outside the restricted Host; no local fallback was attempted.") from error
        require(raw.endswith(b"\n") and len(raw) <= MAX_OUTPUT, "CALL_OUTCOME_UNKNOWN", "Missing or oversized server response; preserve the saved request.")
        try:
            response = decode(raw.decode("utf-8"))
        except (ValueError, UnicodeError) as error:
            raise ClientError("CALL_OUTCOME_UNKNOWN", "Server response was not valid JSON; preserve the saved request.") from error
        require(isinstance(response, dict) and response.get("api_version") == API_VERSION and type(response.get("ok")) is bool,
                "API_MISMATCH", "Expected the external-harness-v2 API.")
        if response["ok"]:
            require(response.get("transport", {}).get("configuration") == expected,
                    "TRANSPORT_CONFIGURATION_MISMATCH", "Server response belongs to a different store or policy registry.")
        return response

    @contextmanager
    def serialized(self, key):
        """Transport mutex only: Core remains the authoritative concurrency guard."""
        require(os.name == "posix", "CALLER_LOCK_UNAVAILABLE", "This bash caller requires POSIX file locks (use WSL on Windows).")
        require(isinstance(key, str) and bool(key), "REQUEST_INVALID", "A request scope is required.")
        import fcntl
        root = Path(self.config["state_dir"]) / ".caller-locks"
        if self.config.get("server_socket"):
            # The remote store need not be visible inside the Host sandbox.
            # This is only a per-user transport mutex, never a Run database.
            keyspace = hashlib.sha256(self.config["server_socket"].encode("utf-8")).hexdigest()
            root = Path(tempfile.gettempdir()) / ("harness-client-" + str(os.getuid())) / keyspace
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
            if request["operation"] != "start" and not self.config.get("server_socket"):
                require(self.database_path(request).is_file(), "STATE_NOT_FOUND",
                        "Configured state is missing; do not initialize a replacement store.")
            key = request["arguments"].get("run_id", request.get("task_id", "start"))
            with self.serialized(key):
                return self._call(request)
        return self._call(request)

    def normalize_request(self, request):
        require(isinstance(request, dict) and set(request) <= {"operation", "arguments", "request_id", "context", "task_id", "project_workspace", "knowledge", "input_bindings"}
                and {"operation", "arguments"} <= request.keys(), "REQUEST_INVALID", "Use operation, arguments, optional request_id and context.")
        operation, arguments = request["operation"], request["arguments"]
        cli_arguments(operation, arguments)
        if "input_bindings" in request:
            validate_input_bindings(operation, request["input_bindings"])
        if "task_id" in request:
            self.database_path(request)
        if "project_workspace" in request:
            require("task_id" in request, "PROJECT_WORKSPACE", "Project storage requires its task ID")
            request = {**request, "project_workspace": str(absolute_path(request["project_workspace"]))}
        require("knowledge" not in request or operation == "finish" and isinstance(request["knowledge"], dict),
                "KNOWLEDGE_ARGUMENT", "A selected knowledge object is optional on finish only.")
        request_id = request.get("request_id")
        require((isinstance(request_id, str) and 0 < len(request_id) <= 128 and "\x00" not in request_id)
                if operation in MUTATIONS else request_id is None, "REQUEST_ID_REQUIRED",
                "Mutations need an explicit stable request_id; reads do not accept one. Replay identical requests with the same ID.")
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
        if result.get("ok") and "task_id" in request:
            require(result.get("task_id") == request["task_id"], "TASK_STORE_MISMATCH",
                    "Response is not bound to the selected task store. Upgrade/check the runtime; do not fall back to a legacy store.")
            require(result.get("project_workspace") == request.get("project_workspace"), "TASK_STORE_MISMATCH",
                    "Response belongs to a different project storage route; no fallback was attempted.")
        return result

    def call_operation(self, operation, arguments, *, binding=None, action_id=None, output=None, binding_output=None, binding_path=None,
                       task_id=None, project_workspace=None, knowledge=None, input_bindings=None):
        """Save one explicit intent, then send it. No automatic ID change/retry."""
        request = self.normalize_request(self.make_request(operation, arguments, binding=binding, action_id=action_id,
                                                          task_id=task_id, project_workspace=project_workspace, knowledge=knowledge, input_bindings=input_bindings)["request"])
        if operation in MUTATIONS and output is None:
            anchor = binding_path or binding_output
            require(anchor is not None, "REQUEST_FILE_REQUIRED", "Supply --output or a binding file location to save the exact request.")
            parent = Path(anchor).absolute().parent / ".harness-requests"
            require(not any(p.is_symlink() for p in (parent, *parent.parents)), "REQUEST_FILE_PATH", "Request directory must not use symlinks")
            parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            # Content is deliberately NOT in the key: changing an existing action
            # must conflict, not silently create another request file.
            key = digest({"config": self.config, "operation": operation, "request_id": request["request_id"]})
            output = parent / (key + ".json")
        require(binding_output is None or operation == "start", "BINDING_OPERATION", "Only start can save a new binding from its receipt.")
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
            if binding_output is not None and result.get("ok"):
                result = {**result, "binding_file": self.bind_started(result, binding_output)}
        except (ClientError, OSError, ValueError) as error:
            error.saved_request = reference
            raise
        return {**result, **({"caller_request": reference} if reference else {})}

    def bind_started(self, result, output):
        context = result.get("context")
        require(result.get("ok") and isinstance(context, dict)
                and set(context) == {"workspace", "domain", "intent_ref", "policy_ref"},
                "CORE_UPGRADE_REQUIRED", "Start receipt lacks binding references. Preserve/replay the saved request; do not start another Run.")
        binding = {"schema_version": "harness-caller-binding-v1", "config": copy.deepcopy(self.config),
                   "run_id": result["run_id"], **copy.deepcopy(context)}
        if "task_id" in result:
            binding.update(schema_version="harness-caller-binding-v2", task_id=result["task_id"])
        if "project_workspace" in result:
            binding.update(schema_version="harness-caller-binding-v3", project_workspace=result["project_workspace"])
        self.binding_context(binding)
        path = Path(output).absolute()
        try:
            write_new_json(path, binding)
        except FileExistsError:
            require(not path.is_symlink() and read_json(str(path)) == binding,
                    "BINDING_CONFLICT", "Binding file belongs to different references; preserve the saved start request and select a new binding path.")
        return str(path)

    def bind(self, run_id, workspace, domain, output, *, task_id=None, project_workspace=None):
        resumed = self.call({"operation": "resume", "arguments": {"run_id": run_id},
                             "context": {"workspace": workspace, "domain": domain},
                             **({"task_id": task_id} if task_id is not None else {}),
                             **({"project_workspace": project_workspace} if project_workspace is not None else {})})
        if not resumed["ok"]:
            return resumed
        binding = {"schema_version": "harness-caller-binding-v1", "config": self.config,
                   "run_id": run_id, "workspace": resumed["run"]["workspace"], "domain": domain,
                   "intent_ref": resumed["intent"]["ref"], "policy_ref": resumed["policy"]["ref"]}
        if task_id is not None:
            binding.update(schema_version="harness-caller-binding-v2", task_id=task_id)
        if project_workspace is not None:
            binding.update(schema_version="harness-caller-binding-v3", project_workspace=resumed["project_workspace"])
        try:
            write_new_json(output, binding)
        except FileExistsError:
            require(not Path(output).is_symlink() and read_json(str(output)) == binding,
                    "BINDING_CONFLICT", "Existing binding has different references; it was not overwritten.")
        return {"ok": True, "binding_file": str(output), "binding": binding,
                "note": "A pinned reference, not a Run database; Harness is always revalidated."}

    def binding_context(self, binding):
        require(isinstance(binding, dict), "BINDING_INVALID", "Invalid caller binding.")
        project = binding.get("schema_version") == "harness-caller-binding-v3"
        scoped = project or binding.get("schema_version") == "harness-caller-binding-v2"
        require(set(binding) == {"schema_version", "config", "run_id", "workspace", "domain", "intent_ref", "policy_ref"} | ({"task_id"} if scoped else set()) | ({"project_workspace"} if project else set())
                and binding["schema_version"] in {"harness-caller-binding-v1", "harness-caller-binding-v2", "harness-caller-binding-v3"},
                "BINDING_INVALID", "Invalid caller binding.")
        require(not project or binding["project_workspace"] == str(absolute_path(binding["workspace"])),
                "BINDING_INVALID", "Project route and Run workspace differ")
        if scoped:
            self.database_path(binding)
        require(binding["config"] == self.config, "BINDING_CONFIG_MISMATCH", "Binding belongs to another Harness/store configuration.")
        return {"workspace": str(absolute_path(binding["workspace"])), "domain": binding["domain"],
                "intent_ref": binding["intent_ref"], "policy_ref": binding["policy_ref"]}

    def validate_binding(self, binding):
        context = self.binding_context(binding)
        result = self._call({"operation": "resume", "arguments": {"run_id": binding["run_id"]},
                             "context": context, **({"task_id": binding["task_id"]} if "task_id" in binding else {}),
                             **({"project_workspace": binding["project_workspace"]} if "project_workspace" in binding else {})})
        if result["ok"]:
            require(result["intent"]["ref"] == binding["intent_ref"] and result["policy"]["ref"] == binding["policy_ref"],
                    "BINDING_IDENTITY_CHANGED", "Run intent or policy differs from the selected binding.")
        return result

    def make_request(self, operation, arguments, *, action_id=None, binding=None, task_id=None, project_workspace=None, knowledge=None, input_bindings=None):
        require(isinstance(arguments, dict), "ARGUMENTS_INVALID", "Arguments must be an object.")
        value = {"operation": operation, "arguments": dict(arguments)}
        if binding is not None:
            context = self.binding_context(binding)
            require("run_id" not in arguments, "BINDING_ARGUMENT_CONFLICT", "The binding supplies run_id.")
            if operation == "start":
                require(arguments.get("predecessor_run_id") == binding["run_id"], "TASK_CONTINUATION",
                        "Starting through an old binding requires its explicit predecessor Run.")
            else:
                value["arguments"]["run_id"] = binding["run_id"]
                value["context"] = context
            require(task_id is None or task_id == binding.get("task_id"), "TASK_STORE_MISMATCH", "Binding and task selector differ.")
            task_id = binding.get("task_id")
            require(project_workspace is None or project_workspace == binding.get("project_workspace"),
                    "TASK_STORE_MISMATCH", "Binding and project store selector differ")
            project_workspace = binding.get("project_workspace")
        cli_arguments(operation, value["arguments"])
        if operation in MUTATIONS:
            start_scope = None
            if operation == "start" and binding is None:
                # Start has no Run ID yet. Scope the label to explicit mechanical
                # context, NOT the payload: changing a goal must still conflict.
                # Exclude socket/runtime paths so reconnecting does not invent an intent.
                start_scope = "start-scope-v1:" + digest({"state_dir": self.config["state_dir"],
                    "workspace": str(absolute_path(arguments["workspace"])), "domain": arguments["domain"]})
            value["request_id"] = action_key(binding, action_id, start_scope=start_scope)
        else:
            require(action_id is None, "REQUEST_ID_REQUIRED", "Read operations do not need an action ID.")
        if task_id is None and binding is None and operation == "start" and not arguments.get("predecessor_run_id"):
            task_id = "task_" + digest({"request_id": value["request_id"],
                "workspace": str(absolute_path(arguments["workspace"])), "domain": arguments["domain"]})[:32]
            if self.config.get("task_storage", "external") == "project":
                project_workspace = str(absolute_path(arguments["workspace"]))
        if project_workspace is not None:
            require(task_id is not None, "PROJECT_WORKSPACE", "A project route requires a task ID")
            value["project_workspace"] = str(absolute_path(project_workspace))
        if task_id is not None:
            value["task_id"] = task_id
            self.database_path(value)
        if knowledge is not None:
            require(operation == "finish" and isinstance(knowledge, dict), "KNOWLEDGE_ARGUMENT", "Selected knowledge is an object on finish.")
            value["knowledge"] = copy.deepcopy(knowledge)
        if input_bindings is not None:
            require(binding is not None, "BINDING_REQUIRED", "Record reuse requires an explicit Run binding")
            validate_input_bindings(operation, input_bindings)
            value["input_bindings"] = copy.deepcopy(input_bindings)
        return {"ok": True, "request": value}

    def checkpoint(self, binding, action_id, *, compare_baseline=False, wait_seconds=0, view=None,
                   attention_limit=None, payload=_UNSET, note=_UNSET, binding_path=None, output=None):
        require(isinstance(binding, dict), "BINDING_INVALID", "Checkpoint requires an explicit Run binding.")
        require(type(compare_baseline) is bool, "ARGUMENTS_INVALID", "compare_baseline must be boolean.")
        validate_wait(wait_seconds)
        arguments = {"compare_baseline": compare_baseline, "wait_seconds": wait_seconds}
        if view is not None:
            arguments["view"] = view
        if attention_limit is not None:
            arguments["attention_limit"] = attention_limit
        if payload is not _UNSET:
            arguments["payload"] = copy.deepcopy(payload)
        if note is not _UNSET:
            arguments["note"] = note
        if binding_path is not None or output is not None:
            return self.call_operation("checkpoint", arguments, binding=binding, action_id=action_id,
                                       binding_path=binding_path, output=output)
        request = self.make_request("checkpoint", arguments, action_id=action_id, binding=binding)["request"]
        return self.call(request)

    def wait(self, binding, job_id, *, wait_seconds=30, validated=False, view=None, attention_limit=None):
        validate_wait(wait_seconds)
        arguments = {"job_id": job_id, "wait_seconds": wait_seconds}
        if view is not None:
            arguments["view"] = view
        if attention_limit is not None:
            arguments["attention_limit"] = attention_limit
        request = self.make_request("wait", arguments, binding=binding)["request"]
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
        if response.get("view") == "attention":
            require(response.get("schema_version") == "harness-attention-v1", "API_MISMATCH", "Unknown Core attention view")
            require(response.get("attention_limit") == limit, "ATTENTION_ARGUMENTS",
                    "This response used a different attention_limit; use the matching limit or request a new Core view")
            return response
        require(response.get("view") in {"summary", "compact"}, "ATTENTION_ARGUMENTS", "Attention uses a summary or compact read response")
        arguments = {"view": "attention", "attention_limit": limit}
        if response.get("job"):
            arguments["job_id"] = response["job"]["job_id"]
        if "current_workspace_matches_submitted_files" in response:
            arguments["check_workspace"] = True
        result = self.call(self.make_request("status", arguments, binding=binding)["request"])
        require(result.get("ok"), result.get("error", {}).get("code", "ATTENTION_FAILED"), result.get("error", {}).get("message", "Core attention lookup failed"))
        if "wait_status" in response:
            result["wait_status"] = response["wait_status"]
        return result

    def find(self, workspace, domain, goal=None, max_pages=10, *, task_id=None, project_workspace=None):
        workspace = str(absolute_path(workspace))
        require(type(max_pages) is int and 1 <= max_pages <= 100, "PAGE_LIMIT", "max_pages must be 1..100.")
        offset, matches = 0, []
        for _ in range(max_pages):
            page = self.call({"operation": "list-runs", "arguments": {"offset": offset, "limit": 100},
                              **({"task_id": task_id} if task_id is not None else {}),
                              **({"project_workspace": project_workspace} if project_workspace is not None else {})})
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


def action_key(binding, value, *, start_scope=None):
    require(isinstance(value, str) and 0 < len(value) <= 128 and "\x00" not in value,
            "ACTION_ID_REQUIRED", "Give this intentional action a stable ID; reuse it only for retransmission.")
    require(binding is None or isinstance(binding, dict) and isinstance(binding.get("run_id"), str),
            "BINDING_INVALID", "A valid explicit binding is required.")
    require(start_scope is None or binding is None and isinstance(start_scope, str) and bool(start_scope),
            "ACTION_SCOPE_INVALID", "Start scope cannot override a Run binding")
    scope = binding["run_id"] if binding is not None else start_scope or "start"
    return "caller-" + hashlib.sha256((scope + "\x00" + value).encode()).hexdigest()


def fsync_parent(path):
    if os.name != "posix":
        return
    descriptor = os.open(Path(path).parent, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def read_regular_file(path, maximum):
    path = Path(path)
    require(not any(item.is_symlink() for item in (path, *path.parents)), "FILE_PATH", "Input path contains a symbolic link")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and before.st_size <= maximum,
                "FILE_TYPE", "Input must be a bounded single-link regular file")
        body = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
    require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) ==
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns),
            "FILE_CHANGED", "Input file changed while reading")
    require(len(body) <= maximum, "INPUT_TOO_LARGE", "Input file exceeds its byte limit")
    return body


def write_new_json(path, value, *, indent=None):
    path = Path(path)
    require(not any(item.is_symlink() for item in (path, *path.parents)), "OUTPUT_PATH", "Output path contains a symbolic link")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, allow_nan=False,
                  **({"indent": indent} if indent is not None else {"separators": (",", ":")}))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    fsync_parent(path)


def append_cost(path, event):
    """Optional diagnostics cannot change a committed operation's outcome."""
    try:
        path = Path(path)
        require(not any(item.is_symlink() for item in (path, *path.parents)), "TRACE_PATH", "Trace path contains a symlink")
        descriptor = os.open(path, os.O_CREAT | os.O_APPEND | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(descriptor, "ab+") as stream:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            stream.seek(0)
            first = stream.readline(4097)
            require(not first or len(first) <= 4096 and decode(first.decode("utf-8")).get("schema_version") == "harness-client-cost-v1",
                    "TRACE_FORMAT", "Existing file is not a caller cost trace")
            encoded = canonical(event) + b"\n"
            require(len(encoded) <= 4096, "TRACE_LIMIT", "Trace row exceeds limit")
            stream.write(encoded)
            stream.flush()
        return None
    except (OSError, ValueError, ClientError, ImportError, AttributeError):
        return "TRACE_WRITE_FAILED"


def with_cost(result, client, trace_file, started, command):
    if trace_file is None:
        return result
    events = client.cost_events if client is not None else []
    elapsed = round((time.monotonic() - started) * 1000, 3)
    transport = round(sum(item["elapsed_ms"] for item in events), 3)
    metric = {"schema_version": "harness-client-cost-v1", "kind": "helper", "command": command,
              "run_ids": sorted({item["run_id"] for item in events if item.get("run_id")}),
              "ok": result.get("ok"), "error_code": response_error_code(result),
              "elapsed_ms": elapsed, "transport_ms": transport, "outside_transport_ms": round(max(0, elapsed - transport), 3),
              "transport_calls": len(events), "result_bytes_before_telemetry": len(canonical(result)),
              "core_response_bytes": sum(item["response_bytes"] or 0 for item in events),
              "unknown_response_count": sum(item["response_bytes"] is None for item in events),
              "model_tokens": None, "meaning": "local_timings_and_bytes_not_model_billing"}
    warnings = list(client.trace_warnings) if client is not None else []
    if "TRACE_INSIDE_WORKSPACE" not in warnings:
        warning = append_cost(trace_file, metric)
        if warning:
            warnings.append(warning)
    return {**result, "client_cost": metric, **({"telemetry_warnings": sorted(set(warnings))} if warnings else {})}


def source_arguments(operation, arguments, *, goal_file=None, source_files=()):
    """Read explicitly selected originals once, before saving the exact request."""
    require(not (goal_file or source_files) or operation == "start", "SOURCE_OPERATION", "Original source files belong to start")
    result = copy.deepcopy(arguments)
    def original(filename, maximum):
        path = absolute_path(str(filename))
        data = read_regular_file(path, maximum)
        return data.decode("utf-8")
    if goal_file:
        require("goal" not in result, "SOURCE_CONFLICT", "Use goal or --goal-file, not both")
        result["goal"] = original(goal_file, 64000)
    if source_files:
        require("sources" not in result, "SOURCE_CONFLICT", "Use sources or --source, not both")
        sources = []
        for entry in source_files:
            key, separator, filename = entry.partition("=")
            require(bool(separator and key and filename), "SOURCE_ARGUMENT", "Use --source ID=/absolute/file")
            sources.append({"id": key, "label": Path(filename).name, "text": original(filename, 64000)})
        result["sources"] = sources
    return result


def pointer(raw):
    """JSON Pointer field syntax, not a natural-language parser or filesystem path."""
    require(isinstance(raw, str) and raw.startswith("/") and len(raw) <= 4096, "FIELD_PATH", "Use /field or /object/field")
    parts = raw[1:].split("/")
    require(len(parts) <= 64 and all(not re.search(r"~(?![01])", part) for part in parts), "FIELD_PATH", "Invalid JSON Pointer")
    return [part.replace("~1", "/").replace("~0", "~") for part in parts]


def select_field(value, parts):
    for part in parts:
        if isinstance(value, list):
            require(isinstance(part, str) and re.fullmatch(r"0|[1-9][0-9]*", part) and int(part) < len(value), "FIELD_PATH", "Unknown array index")
            value = value[int(part)]
        else:
            require(isinstance(value, dict) and part in value, "FIELD_PATH", "Unknown object field")
            value = value[part]
    return value


def insert_field(arguments, parts, value):
    """Only add an absent field; no last-wins, implicit array indexes or merging."""
    current, actual = arguments, []
    for index, part in enumerate(parts):
        last = index == len(parts) - 1
        require(isinstance(current, dict), "FIELD_PATH", "Use --json for complete arrays; field paths address objects")
        actual.append(part)
        if last:
            require(part not in current, "FIELD_CONFLICT", "Field already supplied: /" + "/".join(parts))
            current[part] = copy.deepcopy(value)
        else:
            if part not in current:
                current[part] = {}
            current = current[part]
    return actual


def validate_input_bindings(operation, bindings):
    prefixes = CATALOG.get("delivery", {}).get("targets", {}).get(operation, [])
    require(prefixes and isinstance(bindings, list) and 1 <= len(bindings) <= 32, "DELIVERY_TARGET", "Record reuse is for content fields of supported operations")
    seen = []
    for item in bindings:
        require(isinstance(item, dict) and set(item) == {"target", "source"}, "DELIVERY_REFERENCE", "Use target and source")
        target = item["target"]
        require(isinstance(target, list) and 1 <= len(target) <= 64 and all(type(key) in (str, int) for key in target)
                and any(target[:len(p)] == p for p in prefixes), "DELIVERY_TARGET", "Record reuse cannot supply routing, authority, citations or outcomes")
        require(not any(target[:len(old)] == old or old[:len(target)] == target for old in seen), "FIELD_CONFLICT", "Record binding targets overlap")
        seen.append(target)
        reference = item["source"]
        require(isinstance(reference, dict) and set(reference) == set(CATALOG["delivery"]["reference_fields"]), "DELIVERY_REFERENCE", "Use an exact Harness record reference")
        require(isinstance(reference["path"], list) and len(reference["path"]) <= 64 and
                all(type(key) in (str, int) for key in reference["path"]), "DELIVERY_REFERENCE", "Invalid reference field path")


def field_arguments(client, operation, arguments, *, binding=None, texts=(), text_files=(), json_fields=(), nulls=(), from_records=()):
    """Serialize explicit values; selected prior fields are fetched and pinned."""
    require(isinstance(arguments, dict), "ARGUMENTS_INVALID", "Arguments must be an object")
    result, bindings = copy.deepcopy(arguments), []
    def pair(raw):
        name, separator, value = raw.partition("=")
        require(bool(separator), "FIELD_ARGUMENT", "Use /field=value (an empty value is allowed)")
        return pointer(name), value
    for mode, entries in (("text", texts), ("file", text_files), ("json", json_fields)):
        for entry in entries:
            keys, value = pair(entry)
            if mode == "file":
                raw = read_regular_file(Path(value), MAX_INPUT)
                value = raw.decode("utf-8")  # preserve CRLF and all other text bytes
            elif mode == "json":
                value = decode(value)
            insert_field(result, keys, value)
    for name in nulls:
        insert_field(result, pointer(name), None)
    for entry in from_records:
        require(binding is not None, "BINDING_REQUIRED", "Record reuse requires --binding")
        keys, location = pair(entry)
        filename, fragment, selector = location.partition("#")
        reference = read_json(filename)
        if fragment:
            reference = select_field(reference, pointer(selector))
        require(isinstance(reference, dict), "DELIVERY_REFERENCE", "Selected field must be a record reference")
        reference = copy.deepcopy(reference)
        # A records projection's source addresses the whole record.
        if set(reference) == {"run_id", "kind", "record_id", "record_digest"}:
            reference.update(path=[], sha256=reference["record_digest"])
        item = {"target": keys, "source": reference}
        validate_input_bindings(operation, [*bindings, item])
        require(reference["run_id"] == binding["run_id"], "DELIVERY_RUN_MISMATCH", "Reference belongs to another Run")
        details = {key: value for key, value in reference.items() if key not in {"run_id", "kind"}}
        response = client.resolve_detail(binding, {"kind": reference["kind"], "detail": details})
        insert_field(result, keys, response["value"])
        bindings.append(item)
    if operation == "observe" and isinstance(result.get("data"), dict) and "payload" in result["data"]:
        result["data"].setdefault("note", "")
    require(len(canonical(result)) <= MAX_INPUT, "INPUT_TOO_LARGE", "Combined fields exceed the request size limit")
    return result, bindings or None


def save_response(path, result):
    """A receipt projection, never a ledger; failure cannot undo the operation."""
    try:
        try:
            write_new_json(path, result)
        except FileExistsError:
            require(not path.is_symlink() and read_json(str(path)) == result, "RECEIPT_CONFLICT", "Existing response file differs; it was not overwritten")
        return {**result, "response_output": {"status": "saved", "path": str(path.absolute())}}
    except (ClientError, OSError, ValueError) as error:
        return {**result, "response_output": {"status": "failed", "path": str(path.absolute()),
                "error": {"code": getattr(error, "code", "RECEIPT_IO"), "message": str(error)[:1000]},
                "meaning": "operation_result_unchanged_replay_saved_request_to_recover_receipt"}}


def main(argv=None):
    started, client, args = time.monotonic(), None, None
    try:
        parser = Parser(description=__doc__)
        parser.add_argument("--config", type=Path, default=default_config_path(), help="one persistent caller config, independent of the workspace")
        parser.add_argument("--call-timeout", type=float, help="optional foreground CLI timeout; not the verification job budget")
        parser.add_argument("--trace-file", type=Path, help="optional caller timing/byte log outside the workspace; never model token estimates")
        commands = parser.add_subparsers(dest="command", required=True)
        schema = commands.add_parser("describe", help="show local request fields without loading config or calling Harness")
        schema.add_argument("--operation", choices=tuple(name for name in OPTIONS if name != "invoke"))
        schema.add_argument("--domain", help="query the configured Harness for Domain-owned parameter schemas")
        schema.add_argument("--adapter", help="limit Domain discovery to one registered adapter")
        schema.add_argument("--runtime", action="store_true", help="query the configured Core's actual operation schema")
        schema.add_argument("--helper", choices=("find", "bind", "context", "checkpoint", "wait", "explore"), help="show exact helper arguments")
        setup = commands.add_parser("configure", help="create a NEW config; never overwrite")
        setup.add_argument("--harness-repo", required=True)
        setup.add_argument("--state-dir", required=True)
        setup.add_argument("--ephemeral", action="store_true", help="explicitly disposable tests only")
        setup.add_argument("--policy-root", help="operator-configured acceptance registry, not an editable task proposal")
        setup.add_argument("--server-socket", help="explicit external Harness socket; never falls back to spawning local Core")
        setup.add_argument("--python-executable", help="absolute Python executable to pin for local calls; default selects checkout venv or this configuration process")
        setup.add_argument("--task-storage", choices=("project", "external"), default="project", help="new tasks use project/.harness; external retains the historical layout")
        call = commands.add_parser("call", help="one request, no automatic action loop")
        source = call.add_mutually_exclusive_group(required=True)
        source.add_argument("--request", help="replay a saved JSON request: path, '-' stdin, or json:{...}")
        source.add_argument("--operation", choices=tuple(OPTIONS), help="construct and send one existing operation")
        call.add_argument("--binding", type=Path)
        call.add_argument("--task-id", help="explicit discovered task selector; new start derives one automatically")
        call.add_argument("--project-workspace", help="explicit project store route with --task-id; bindings carry it automatically")
        call.add_argument("--knowledge", help="finish only: selected reusable note as JSON file, stdin or json:{...}")
        call.add_argument("--arguments", default="json:{}", help="JSON arguments; binding supplies run_id")
        call.add_argument("--text", action="append", default=[], help="/field=literal text; repeat instead of writing a JSON object")
        call.add_argument("--text-file", action="append", default=[], help="/field=UTF-8 file; preserves text exactly")
        call.add_argument("--json", action="append", default=[], help="/field=JSON value for explicit arrays, objects, numbers or booleans")
        call.add_argument("--null", action="append", default=[], help="/field explicitly unresolved; does not satisfy execution requirements")
        call.add_argument("--from-record", action="append", default=[], help="/field=receipt.json[#/reference]; copy one hash-checked same-Run value")
        call.add_argument("--response-output", type=Path, help="save the returned JSON for later reference; never overwrite a different receipt")
        call.add_argument("--goal-file", type=Path, help="start only: read the original goal as UTF-8, without summarizing it")
        call.add_argument("--source", action="append", default=[], help="start only: ID=/absolute/specification; repeat for original documents")
        call.add_argument("--action-id", help="stable explicit intent; mutations only")
        call.add_argument("--output", type=Path, help="save exact request; defaults beside --binding/--binding-output for mutations")
        call.add_argument("--binding-output", type=Path, help="start only: save binding from the same Core receipt, without another resume")
        call.add_argument("--resolve-detail", action="store_true", help="decode only the selected records detail, starting at byte zero")
        call.add_argument("--format", choices=("raw", "attention", "compact", "brief"), default="raw", help="brief for checkpoint/wait; attention for bound reads; compact start/finish or status/resume")
        call.add_argument("--attention-limit", type=int, default=20)
        find = commands.add_parser("find", help="list matching Runs without choosing/resuming one")
        find.add_argument("--workspace", required=True)
        find.add_argument("--domain", required=True)
        find.add_argument("--goal", help="optional exact original goal filter")
        find.add_argument("--max-pages", type=int, default=10)
        find.add_argument("--task-id", help="search one selected task; omission searches the legacy store")
        find.add_argument("--project-workspace", help="project storage location returned by list-tasks")
        bind = commands.add_parser("bind", help="save explicit Run/context references, never choose the newest Run")
        bind.add_argument("--run-id", required=True)
        bind.add_argument("--workspace", required=True)
        bind.add_argument("--domain", required=True)
        bind.add_argument("--output", type=Path, required=True)
        bind.add_argument("--task-id", help="exact task ID from list-tasks/start; omitted for legacy Runs")
        bind.add_argument("--project-workspace", help="explicit project store location; no automatic fallback to the shared store")
        prepare = commands.add_parser("request", help="serialize a request once; resend the saved envelope after response loss")
        prepare.add_argument("--binding", type=Path)
        prepare.add_argument("--operation", required=True, choices=tuple(OPTIONS))
        prepare.add_argument("--arguments", default="json:{}", help="JSON arguments; the binding supplies Run context")
        prepare.add_argument("--action-id")
        prepare.add_argument("--task-id")
        prepare.add_argument("--project-workspace")
        prepare.add_argument("--output", type=Path, required=True)
        checkpoint = commands.add_parser("checkpoint", help="submit + verify, with no assessment, repair loop or finish")
        checkpoint.add_argument("--binding", type=Path, required=True)
        checkpoint.add_argument("--action-id", required=True)
        checkpoint.add_argument("--compare-baseline", action="store_true")
        checkpoint.add_argument("--wait-seconds", type=float, default=30)
        checkpoint.add_argument("--view", choices=("brief", "attention", "summary"), default="brief")
        checkpoint.add_argument("--attention-limit", type=int, default=20)
        checkpoint.add_argument("--payload", help="optional fields/extensions/parents JSON; omitted means no caller note")
        checkpoint.add_argument("--note", help="optional caller note, never verification evidence")
        checkpoint.add_argument("--output", type=Path, help="saved exact request; defaults beside the binding")
        checkpoint.add_argument("--response-output", type=Path, help="optional receipt with exact caller record reference")
        wait = commands.add_parser("wait", help="poll one existing job; timeout means pending, never failed")
        wait.add_argument("--binding", type=Path, required=True)
        wait.add_argument("--job-id", required=True)
        wait.add_argument("--wait-seconds", type=float, default=30)
        wait.add_argument("--view", choices=("brief", "attention", "summary"), default="brief")
        wait.add_argument("--attention-limit", type=int, default=20)
        context = commands.add_parser("context", help="first restore without a cursor; then request explicit deltas")
        context.add_argument("--binding", type=Path, required=True)
        context.add_argument("--after")
        context.add_argument("--page")
        context.add_argument("--limit", type=int, default=20)
        context.add_argument("--complete", action="store_true", help="validate and coalesce all pages with short, lossless text references")
        context.add_argument("--max-pages", type=int, default=64)
        explore = commands.add_parser("explore", help="local Change Lens discovery only; no Core call or verification")
        explore.add_argument("arguments", nargs=argparse.REMAINDER, help="inspect|impact|context PATH; for example context PATH --changed src/a.py --limit 8 --format json")
        commands.add_parser("preflight", help="measure actual strict Sandbox availability; no task run is created")
        args = parser.parse_args(argv)
        if args.command == "describe" and args.helper:
            require(not (args.domain or args.operation or args.runtime or args.adapter), "CLI_ARGUMENTS", "Choose one discovery target")
            target = commands.choices[args.helper]
            result = {"ok": True, "helper": args.helper, "help": target.format_help(),
                      "required": [a.option_strings[0] for a in target._actions if a.required]}
        elif args.command == "describe" and not args.domain and not args.runtime:
            require(not args.adapter, "CLI_ARGUMENTS", "--adapter requires --domain")
            result = describe(args.operation)
        elif args.command == "configure":
            result = configure(args.config, args.harness_repo, args.state_dir, args.ephemeral, args.policy_root, args.server_socket, args.python_executable, args.task_storage)
        else:
            require(args.config.is_file(), "CONFIG_MISSING",
                    "Selected config does not exist: " + str(args.config) +
                    ". Use --config PATH or BASE_HARNESS_CLIENT_CONFIG for the installed configuration; do not select another store from a workspace binding.")
            client = Client(read_json(str(args.config)), timeout=args.call_timeout, trace_file=args.trace_file)
            if args.command == "describe":
                require((args.operation is None) != bool(args.runtime), "CLI_ARGUMENTS", "--runtime requires --operation; Domain discovery uses --domain")
                require(not args.runtime or not (args.domain or args.adapter), "CLI_ARGUMENTS", "Choose operation or Domain discovery")
                fields = {"operation": args.operation} if args.runtime else {"domain_id": args.domain, **({"adapter_id": args.adapter} if args.adapter else {})}
                result = client.call({"operation": "describe", "arguments": fields})
                if args.runtime and result.get("ok"):
                    result["matches_bundled_schema"] = result["input_schema"] == CATALOG["core_operations"][args.operation]
            elif args.command == "call":
                binding = read_json(str(args.binding)) if args.binding else None
                if args.request is not None:
                    require(args.action_id is None and args.output is None and args.arguments == "json:{}" and not args.goal_file and not args.source
                            and args.task_id is None and args.project_workspace is None and args.knowledge is None and not (args.text or args.text_file or args.json or args.null or args.from_record),
                            "CLI_ARGUMENTS", "Saved request replay cannot also construct an intent")
                    saved = client.normalize_request(read_json(args.request))
                    operation, arguments = saved["operation"], saved["arguments"]
                    if binding is not None:
                        require(saved.get("context") == client.binding_context(binding) and arguments.get("run_id") == binding["run_id"]
                                and saved.get("task_id") == binding.get("task_id") and saved.get("project_workspace") == binding.get("project_workspace"),
                                "RUN_CONTEXT_MISMATCH", "Saved request and binding differ")
                    result = None
                else:
                    operation = args.operation
                    arguments, input_bindings = field_arguments(client, operation, read_json(args.arguments), binding=binding,
                        texts=args.text, text_files=args.text_file, json_fields=args.json, nulls=args.null, from_records=args.from_record)
                    arguments = source_arguments(operation, arguments, goal_file=args.goal_file, source_files=args.source)
                    saved = None
                    result = None
                require(not args.resolve_detail or args.format == "raw" and operation == "records" and binding is not None
                        and args.action_id is None and args.output is None,
                        "CLI_ARGUMENTS", "Detail resolution requires records, a binding and raw format")
                require(args.format != "attention" or operation in {"status", "resume", "checkpoint", "wait"} and binding is not None,
                        "CLI_ARGUMENTS", "Attention requires an explicit bound read/checkpoint/wait")
                require(args.format != "compact" or operation in {"start", "finish", "status", "resume"}, "CLI_ARGUMENTS", "Compact supports start/finish/status/resume")
                require(args.format != "brief" or operation in {"checkpoint", "wait"} and binding is not None,
                        "CLI_ARGUMENTS", "Brief requires a bound checkpoint/wait")
                if args.format == "brief" and saved is not None:
                    require(arguments.get("view") == "brief", "CLI_ARGUMENTS", "Replay the saved request as-is; request a brief wait separately if its original view was different.")
                if args.format == "brief" and saved is None:
                    require(arguments.get("view", "brief") == "brief", "CLI_ARGUMENTS", "--format brief conflicts with the explicit view")
                    arguments.update(view="brief", attention_limit=args.attention_limit)
                if args.format == "compact" and operation in {"status", "resume"}:
                    require(arguments.get("view", "compact") == "compact", "CLI_ARGUMENTS", "--format compact conflicts with the explicit view")
                    arguments["view"] = "compact"
                if args.format == "attention" and operation in {"status", "resume", "checkpoint", "wait"} and saved is None:
                    require(arguments.get("view", "attention") == "attention", "CLI_ARGUMENTS", "--format attention conflicts with the explicit view")
                    require("attention_limit" not in arguments or arguments["attention_limit"] == args.attention_limit,
                            "CLI_ARGUMENTS", "Conflicting attention limits")
                    arguments.update(view="attention", attention_limit=args.attention_limit)
                require(args.binding_output is None or operation == "start", "BINDING_OPERATION", "Only start can save a new binding")
                if args.resolve_detail:
                    detail_arguments = {key: value for key, value in arguments.items() if key != "run_id"}
                    result = client.resolve_detail(binding, detail_arguments)
                elif saved is not None:
                    result = client.call(saved)
                    if args.binding_output is not None and result.get("ok"):
                        result = {**result, "binding_file": client.bind_started(result, args.binding_output)}
                else:
                    result = client.call_operation(operation, arguments, binding=binding, action_id=args.action_id, output=args.output,
                                                   binding_output=args.binding_output, binding_path=args.binding, task_id=args.task_id, project_workspace=args.project_workspace,
                                                   knowledge=read_json(args.knowledge) if args.knowledge is not None else None,
                                                   input_bindings=input_bindings)
                if args.response_output is not None:
                    result = save_response(args.response_output, result)
                if args.format == "compact" and operation in {"start", "finish"}:
                    result = compact_call_result(operation, result)
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
                result = client.find(args.workspace, args.domain, args.goal, args.max_pages, task_id=args.task_id, project_workspace=args.project_workspace)
            elif args.command == "bind":
                result = client.bind(args.run_id, args.workspace, args.domain, args.output, task_id=args.task_id, project_workspace=args.project_workspace)
            elif args.command == "request":
                result = client.make_request(args.operation, read_json(args.arguments), action_id=args.action_id,
                                             binding=read_json(str(args.binding)) if args.binding else None, task_id=args.task_id, project_workspace=args.project_workspace)
                if result["ok"]:
                    write_new_json(args.output, result["request"])
                    result = {"ok": True, "request_file": str(args.output), "request_id": result["request"].get("request_id")}
            elif args.command == "checkpoint":
                result = client.checkpoint(read_json(str(args.binding)), args.action_id,
                                           compare_baseline=args.compare_baseline, wait_seconds=args.wait_seconds,
                                           view=args.view, attention_limit=args.attention_limit,
                                           payload=read_json(args.payload) if args.payload is not None else _UNSET,
                                           note=args.note if args.note is not None else _UNSET,
                                           binding_path=args.binding, output=args.output)
                if args.response_output is not None:
                    result = save_response(args.response_output, result)
            elif args.command == "wait":
                result = client.wait(read_json(str(args.binding)), args.job_id, wait_seconds=args.wait_seconds,
                                     view=args.view, attention_limit=args.attention_limit)
            elif args.command == "context":
                result = client.context(read_json(str(args.binding)), after=args.after, page=args.page, limit=args.limit,
                                        complete=args.complete, max_pages=args.max_pages)
            elif args.command == "explore":
                result = client.explore(args.arguments)
            else:
                result = client.call({"operation": "doctor", "arguments": {"sandbox": True}})
        result = with_cost(result, client, args.trace_file, started, args.command)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":")))
        return 0 if result.get("ok") and result.get("healthy", True) else 2
    except (ClientError, OSError, ValueError, RecursionError) as error:
        result = {"ok": False, "source": "harness-client", "error": {
            "code": getattr(error, "code", "CLIENT_INPUT_OR_IO"), "message": str(error)[:2000]},
            **({"caller_request": error.saved_request} if getattr(error, "saved_request", None) else {})}
        result = with_cost(result, client, getattr(args, "trace_file", None), started, getattr(args, "command", "invalid"))
        print(json.dumps(result, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
