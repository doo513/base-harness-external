"""Registered adapter dispatch and framework-neutral, strict Sandbox transport."""
import copy
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import tempfile
import threading
import time
import uuid

from harness.common import canonical_bytes, canonical_hash
from .errors import HarnessError, fields, integer, relative_path, require
from .adapter_ports import AdapterUnavailable
from .adapter_registry import builtin_adapter_registry, validate_binding

MAX_REPORT_BYTES = 8 * 1024 * 1024
MAX_CASES = 30000
OUTCOMES = {"passed", "failed", "skipped", "error", "xfail", "xpass", "not_selected", "not_run", "incomplete"}


def command(check, registry=None):
    registry = registry if registry is not None else builtin_adapter_registry()
    execution = registry.resolve(check["adapter"]["id"]).command(copy.deepcopy(check))
    fields(execution, {"kind", "argv", "cwd", "timeout_seconds", "expectedExitCode"},
           {"kind", "argv", "cwd", "timeout_seconds", "expectedExitCode"})
    require(execution["kind"] == "command" and type(execution["expectedExitCode"]) is int and execution["expectedExitCode"] == 0,
            "ADAPTER_COMMAND", "Adapter must describe a command with the supported exit condition")
    argv = execution["argv"]
    require(isinstance(argv, list) and 1 <= len(argv) <= 64 and all(isinstance(v, str) and len(v) <= 8192 and "\x00" not in v for v in argv)
            and bool(argv[0].strip()) and len(canonical_bytes(argv)) <= 64000, "ADAPTER_COMMAND", "Invalid materialized argv")
    relative_path(execution["cwd"], directory=True)
    integer(execution["timeout_seconds"], 1, 120, "timeout_seconds")
    require(execution["timeout_seconds"] <= check.get("timeout_seconds", 30), "ADAPTER_COMMAND", "Adapter cannot increase the approved timeout")
    return copy.deepcopy(execution)


def bind(checks, state_root, pinned=None, registry=None):
    registry = registry if registry is not None else builtin_adapter_registry()
    result = copy.deepcopy(pinned or {})
    registry.validate_bindings(result)
    for check in checks:
        selection = check["spec"]["parameters"].get("adapter")
        if not selection:
            continue
        adapter = registry.resolve(selection["id"])
        identity = registry.identity(selection["id"])
        previous = result.get(check["check_id"])
        require(previous is None or previous["id"] == selection["id"], "ADAPTER_SELECTION_CHANGED", "Check adapter selection changed; use a new Run")
        runtime = None
        reason = None
        try:
            runtime = adapter.runtime(state_root, copy.deepcopy(previous["runtime"]) if previous and previous["status"] == "available" else None)
            require(isinstance(runtime, dict) and len(canonical_bytes(runtime)) <= 65536, "ADAPTER_RUNTIME_INVALID", "Runtime identity must be a bounded object")
            require(previous is None or previous["status"] == "unavailable" or previous["runtime"] == runtime,
                    "ADAPTER_RUNTIME_CHANGED", "Pinned adapter runtime changed")
        except AdapterUnavailable as error:
            require(previous is None or previous["status"] == "unavailable", "ADAPTER_RUNTIME_CHANGED", "Pinned adapter runtime became unavailable")
            reason = str(error)[:2000]
        require(registry.identity(selection["id"]) == identity, "ADAPTER_IMPLEMENTATION_CHANGED", "Adapter changed during runtime preparation")
        body = {"schema_version": "adapter-binding-v1", "id": selection["id"], "implementation": identity,
                "status": "unavailable" if reason is not None else "available", "runtime": runtime, "reason": reason}
        result[check["check_id"]] = {**body, "binding_hash": canonical_hash(body)}
    return result


def _case(case):
    require(isinstance(case, dict) and isinstance(case.get("case_id"), str) and 0 < len(case["case_id"].encode()) <= 4096
            and isinstance(case.get("outcome"), str) and case["outcome"] in OUTCOMES and isinstance(case.get("phases"), list)
            and all(type(case.get(key)) is bool for key in ("discovered", "started", "executed", "finished"))
            and (case.get("selected") is None or type(case["selected"]) is bool)
            and len(canonical_bytes(case)) <= MAX_REPORT_BYTES, "CASE_REPORT_PROTOCOL", "Adapter returned an invalid normalized case")
    require(case["outcome"] != "passed" or case["executed"] and case["finished"] and case["selected"] is True,
            "CASE_REPORT_PROTOCOL", "A passing case must have finished execution")
    return copy.deepcopy(case)


def execute(check, payload, state_root, abort, remaining, binding, on_case, registry=None):
    from .worker import clean_environment, stop_child
    deadline = time.monotonic() + remaining
    registry = registry if registry is not None else builtin_adapter_registry()
    validate_binding(binding)
    registry.validate_bindings({"current": binding})
    require(binding["id"] == check["adapter"]["id"], "ADAPTER_SELECTION_CHANGED", "Check and adapter binding differ")
    adapter = registry.resolve(binding["id"])
    execution = command(check, registry)
    artifact_path = relative_path(adapter.report_path)
    artifact = Path(payload) / artifact_path
    require(not artifact.exists() and not artifact.is_symlink(), "ADAPTER_RESOURCE_CONFLICT", "Snapshot collides with the adapter observation artifact")
    if binding.get("status") == "unavailable":
        return {"status": "not_run", "reason": "ADAPTER_UNAVAILABLE: " + binding["reason"]}, None
    require(adapter.runtime(state_root, copy.deepcopy(binding["runtime"])) == binding["runtime"],
            "ADAPTER_RUNTIME_CHANGED", "Adapter runtime differs from its pinned identity")
    token = uuid.uuid4().hex
    observer = adapter.observer(token, copy.deepcopy(binding["runtime"]))
    bridge = Path(__file__).resolve().parents[2] / "runtime/script/external-harness-sandbox.ts"
    bun = shutil.which(os.environ.get("BUN", "bun"))
    if not bun or not bridge.is_file():
        return {"status": "not_run", "reason": "SANDBOX_ADAPTER_UNAVAILABLE"}, None
    require(not abort.is_set() and remaining > 0, "VERIFICATION_CANCELLED", "Verification was cancelled")
    emitted = {}
    def emit(case):
        case = _case(case)
        key = case["case_id"]
        if key in emitted:
            require(emitted[key] == case, "CASE_REPORT_PROTOCOL", "Adapter changed a checkpointed case")
            return
        require(len(emitted) < MAX_CASES, "CASE_REPORT_LIMIT", "Too many case observations")
        on_case(copy.deepcopy(case), copy.deepcopy(binding))
        emitted[key] = case
    with tempfile.TemporaryDirectory(prefix="adapter-resource-", dir=state_root) as temporary:
        resources = Path(temporary)
        adapter.stage(state_root, copy.deepcopy(binding["runtime"]), resources, token)
        registry.validate_bindings({"current": binding})
        remaining = deadline - time.monotonic()
        require(not abort.is_set() and remaining > 0, "VERIFICATION_CANCELLED", "Verification expired during resource preparation")
        request = {"workspace": str(payload), "argv": execution["argv"], "cwd": execution["cwd"],
                   "timeoutMs": max(1, min(int(remaining * 1000), execution["timeout_seconds"] * 1000)),
                   "readonlyResource": str(resources), "observationArtifact": {"path": artifact_path, "maxBytes": MAX_REPORT_BYTES}}
        environment = clean_environment()
        environment["XDG_STATE_HOME"] = str(Path(state_root) / "sandbox-state")
        child = subprocess.Popen([bun, "run", str(bridge)], cwd=bridge.parent, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, env=environment, start_new_session=True)
        child.stdin.write(canonical_bytes(request))
        child.stdin.close()
        messages = queue.Queue()
        def read():
            try:
                total = 0
                while line := child.stdout.readline(MAX_REPORT_BYTES + 1):
                    total += len(line)
                    require(len(line) <= MAX_REPORT_BYTES and total <= MAX_REPORT_BYTES * 2, "CASE_REPORT_LIMIT", "Adapter transport exceeded bounds")
                    messages.put(json.loads(line))
            except Exception as error:
                messages.put(error)
            finally:
                messages.put(None)
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        def drain_errors():
            total = 0
            while chunk := child.stderr.read(4096):
                total += len(chunk)
                if total > MAX_REPORT_BYTES:
                    messages.put(HarnessError("CASE_REPORT_LIMIT", "Adapter stderr exceeded bounds"))
                    return
        errors = threading.Thread(target=drain_errors, daemon=True)
        errors.start()
        capture = None
        failure = None
        try:
            while True:
                if abort.is_set() or time.monotonic() >= deadline:
                    stop_child(child)
                    failure = "VERIFICATION_CANCELLED"
                    break
                try:
                    message = messages.get(timeout=.1)
                except queue.Empty:
                    continue
                if message is None:
                    break
                if isinstance(message, Exception):
                    raise message
                if message.get("type") == "adapter_event":
                    case = observer.feed(json.loads(message["line"]))
                    if case is not None:
                        emit(case)
                else:
                    require(capture is None and message.get("status") in {"completed", "error", "not_run"}, "CASE_REPORT_PROTOCOL", "Invalid final Sandbox receipt")
                    capture = message
            child.wait(timeout=5)
        except Exception as error:
            failure = getattr(error, "code", "CASE_REPORT_PROTOCOL")
        finally:
            if child.poll() is None:
                stop_child(child)
            reader.join(timeout=2)
            errors.join(timeout=2)
            child.stdout.close()
            child.stderr.close()
        if failure:
            capture = {"status": "error", "reason": failure}
        summary = observer.finish(failure)
        require(isinstance(summary, dict) and type(summary.get("collection_complete")) is bool
                and type(summary.get("session_complete")) is bool and isinstance(summary.get("cases"), list)
                and len(summary["cases"]) <= MAX_CASES and len(canonical_bytes(summary)) <= MAX_REPORT_BYTES,
                "CASE_REPORT_PROTOCOL", "Invalid normalized session summary")
        cases = [_case(case) for case in summary["cases"]]
        require(len({case["case_id"] for case in cases}) == len(cases)
                and set(emitted) <= {case["case_id"] for case in cases}, "CASE_REPORT_PROTOCOL", "Summary dropped or duplicated observed cases")
        for case in cases:
            emit(case)
        summary = copy.deepcopy(summary)
        if failure:
            summary.update(session_complete=False, protocol_error=failure)
        summary["adapter_identity"] = binding
        summary["case_scope_hash"] = canonical_hash({"discovered": sorted(case["case_id"] for case in cases),
            "selected": sorted(case["case_id"] for case in cases if case["selected"] is True)}) if summary["collection_complete"] else None
        registry.validate_bindings({"current": binding})
        return capture or {"status": "not_run", "reason": "CASE_REPORT_MISSING"}, summary
