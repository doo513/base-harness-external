"""Application adapter dispatch and bounded structured Sandbox transport."""
import json
import hashlib
import os
from pathlib import Path
import queue
import shutil
import subprocess
import tempfile
import threading
import time
import uuid

from harness.common import canonical_bytes
from .errors import HarnessError, require
from .pytest_adapter import Collector, MAX_REPORT_BYTES, prepare_runtime, load_runtime


def bind(checks, state_root, pinned=None):
    result = dict(pinned or {})
    for check in checks:
        adapter = check["spec"]["parameters"].get("adapter")
        if not adapter:
            continue
        require(adapter["id"] == "pytest-cases-v1", "ADAPTER_UNREGISTERED", "Execution adapter is not registered")
        previous = result.get(check["check_id"])
        if previous is not None and previous.get("status") != "unavailable":
            load_runtime(state_root, previous)
            continue
        try:
            runtime = prepare_runtime(state_root)["identity"]
        except HarnessError as error:
            if error.code != "PYTEST_RUNTIME_UNAVAILABLE":
                raise
            result[check["check_id"]] = {"id": adapter["id"], "status": "unavailable", "reason": error.code}
            continue
        require(previous is None or previous.get("status") == "unavailable" or previous == runtime,
                "ADAPTER_RUNTIME_CHANGED", "Pinned adapter runtime changed; use a new Run")
        result[check["check_id"]] = runtime
    return result


def execute(check, payload, state_root, abort, remaining, binding, on_case):
    from .worker import clean_environment, stop_child
    if binding.get("status") == "unavailable":
        return {"status": "not_run", "reason": "ADAPTER_UNAVAILABLE: " + binding["reason"]}, None
    runtime = load_runtime(state_root, binding)
    token = uuid.uuid4().hex
    collector = Collector(token)
    bridge = Path(__file__).resolve().parents[2] / "runtime/script/external-harness-sandbox.ts"
    bun = shutil.which(os.environ.get("BUN", "bun"))
    if not bun or not bridge.is_file():
        return {"status": "not_run", "reason": "SANDBOX_ADAPTER_UNAVAILABLE"}, None
    emitted = set()
    def emit(case):
        if case["case_id"] not in emitted:
            on_case(case, binding)
            emitted.add(case["case_id"])
    with tempfile.TemporaryDirectory(prefix="pytest-resource-", dir=state_root) as temporary:
        resources = Path(temporary)
        for name in ("runner.py", "packages.zip"):
            shutil.copyfile(Path(runtime["directory"]) / name, resources / name)
        require(hashlib.sha256((resources / "packages.zip").read_bytes()).hexdigest() == binding["runtime_hash"]
                and hashlib.sha256((resources / "runner.py").read_bytes()).hexdigest() == binding["runner_hash"],
                "ADAPTER_RUNTIME_CHANGED", "Runtime changed while preparing resources")
        (resources / "capture.json").write_bytes(canonical_bytes({"token": token}))
        request = {"workspace": str(payload), "argv": check["argv"], "cwd": check["cwd"],
                   "timeoutMs": max(1, min(int(remaining * 1000), check["timeout_seconds"] * 1000)),
                   "readonlyResource": str(resources), "observationArtifact": {"path": ".harness-cases.jsonl", "maxBytes": MAX_REPORT_BYTES}}
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
        started = time.monotonic()
        capture = None
        failure = None
        try:
            while True:
                if abort.is_set() or time.monotonic() - started > remaining:
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
                    case = collector.feed(json.loads(message["line"]))
                    require(collector.framework_version == binding["versions"]["pytest"], "ADAPTER_RUNTIME_CHANGED", "Reporter framework version differs from pinned runtime")
                    if case:
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
            collector.protocol_error = failure
            collector.session_complete = False
            capture = {"status": "error", "reason": failure}
        for case in collector.cases.values():
            if case["selected"] and not case["finished"]:
                case["outcome"] = "incomplete" if case["started"] else "not_run"
            emit(case)
        summary = collector.summary()
        summary["protocol_error"] = collector.protocol_error
        summary["adapter_identity"] = binding
        return capture or {"status": "not_run", "reason": "CASE_REPORT_MISSING"}, summary
