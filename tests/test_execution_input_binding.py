"""The command's actual copy must belong to the pinned Candidate, not just its path."""
import os
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import threading

import pytest

from harness_external import worker, adapter_execution
from harness_external.errors import HarnessError
from harness_external.service import Harness
from test_adapter_registry import start_probe
from test_worker_optimizations import setup as structural_run
from harness_external import execution_inputs
from execution_input_fixtures import receipt, inputs


def command_run(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "data").write_text("ready", encoding="utf-8")
    monkeypatch.setattr(worker, "spawn_worker", lambda *args: None)
    api = Harness(tmp_path / "state")
    run = api.start(domain_id="develop", goal="Measure the submitted input", workspace=str(workspace), request_id="start",
                    parameters={"inputs": ["data"], "artifacts": ["data"],
                        "expectations": [{"path": "data", "operator": "equals", "expected": "ready"}],
                        "test_commands": [{"argv": ["python3", "-c",
                            "from pathlib import Path; print('COMMAND_STARTED'); assert Path('data').read_text() == 'wrong'"]}]})["run_id"]
    api.submit(run, "submit")
    return api, run


@pytest.mark.skipif(os.environ.get("BASE_HARNESS_EXTERNAL_LIVE") != "1", reason="actual strict Sandbox")
@pytest.mark.parametrize("route", ["command", "adapter"])
def test_live_temporary_source_swap_cannot_pass_or_execute_cases(tmp_path, monkeypatch, route):
    monkeypatch.setattr(worker, "spawn_worker", lambda *args: None)
    if route == "command":
        api, run = command_run(tmp_path, monkeypatch)
        module, name, filename, replacement = worker, "execute_sandbox", "data", b"wrong"
        control = api.verify(run, "control")["job_id"]
    else:
        api, run, control = start_probe(tmp_path, ok=False)
        module, name, filename, replacement = adapter_execution, "execute", "probe_data.json", b'{"ok":true}'
    # Negative control: the real, pinned inputs must not pass the command/check.
    worker.execute_job(api.store.root, control)
    assert api.status(run, control)["job"]["result"]["status"] == "failed"
    original = getattr(module, name)
    def swapped(check, root, *args, **kwargs):
        target = root / filename
        before, content = target.stat(), target.read_bytes()
        target.chmod(0o644)
        target.write_bytes(replacement)
        try:
            return original(check, root, *args, **kwargs)
        finally:
            target.write_bytes(content)
            target.chmod(before.st_mode & 0o777)
            os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
    monkeypatch.setattr(module, name, swapped)
    job = api.verify(run, "swap")["job_id"]
    worker.execute_job(api.store.root, job)
    state = api.status(run, job)["job"]
    assert state["result"]["status"] == "incomplete", state
    measured = api.records(run, "measurements", job_id=job)["items"]
    assert "EXECUTION_INPUT_MISMATCH" in str(measured)
    assert "COMMAND_STARTED" not in str(measured)
    assert api.records(run, "cases", job_id=job)["items"] == []
    with pytest.raises(HarnessError):
        api.finish(run, "finish", outcome="completed")


def test_cleanup_time_snapshot_edit_is_caught_before_job_publication(tmp_path, monkeypatch):
    api, run, job = structural_run(tmp_path, monkeypatch)
    candidate = api.status(run)["run"]["candidate"]
    target = api.store.directory(run) / candidate["candidate_id"] / "payload/data"
    original = worker.MeasurementEngine.close
    def changed(engine):
        original(engine)
        target.chmod(0o644)
        target.write_text("wrong", encoding="utf-8")
    monkeypatch.setattr(worker.MeasurementEngine, "close", changed)
    worker.execute_job(api.store.root, job)
    state = api.status(run, job)["job"]
    assert state["status"] == "error" and state["result"]["status"] == "incomplete"
    assert len(api.records(run, "measurements")["items"]) == 2


def test_result_assembly_does_not_run_after_the_final_integrity_check(tmp_path, monkeypatch):
    api, run, job = structural_run(tmp_path, monkeypatch)
    candidate = api.status(run)["run"]["candidate"]
    target = api.store.directory(run) / candidate["candidate_id"] / "payload/data"
    original = worker.observation_links.report
    def changed(*args):
        result = original(*args)
        target.chmod(0o644)
        target.write_text("wrong", encoding="utf-8")
        return result
    monkeypatch.setattr(worker.observation_links, "report", changed)
    worker.execute_job(api.store.root, job)
    state = api.status(run, job)["job"]
    assert state["status"] == "error" and state["result"]["status"] == "incomplete"


def test_unexpected_aggregation_error_also_stops_lease_renewal(tmp_path, monkeypatch):
    api, run, job = structural_run(tmp_path, monkeypatch)
    monitors = []
    original = worker.threading.Thread
    def thread(*args, **kwargs):
        instance = original(*args, **kwargs)
        monitors.append(instance)
        return instance
    def broken(*args):
        raise RuntimeError("controlled aggregate failure")
    monkeypatch.setattr(worker.threading, "Thread", thread)
    monkeypatch.setattr(worker.observation_links, "report", broken)
    with pytest.raises(RuntimeError, match="controlled aggregate failure"):
        worker.execute_job(api.store.root, job)
    assert len(monitors) == 1 and not monitors[0].is_alive()
    assert len(api.records(run, "measurements")["items"]) == 2


def test_input_binding_is_metadata_only_and_detaches_inputs():
    files = [{"path": "없는 파일/한글😀", "size": 5, "sha256": hashlib.sha256(b"ready").hexdigest(), "executable": False}]
    binding = execution_inputs.make_binding(files, {"job": "one"})
    files[0]["size"] = 0
    assert binding["files"][0]["size"] == 5
    other = execution_inputs.make_binding(binding["files"], {"job": "two"})
    assert binding["input_hash"] == other["input_hash"] and binding["scope_hash"] != other["scope_hash"]


@pytest.mark.parametrize("change", [
    {"scope_hash": "wrong"}, {"input_hash": "0" * 64}, {"schema_version": "legacy"}, {"files": [True]},
    {"files": [{"path": "../outside", "sha256": "a" * 64, "size": 0, "executable": False}]},
])
def test_malformed_input_binding_is_rejected(change):
    value = execution_inputs.make_binding([], {"job": "one"})
    value.update(change)
    with pytest.raises(HarnessError):
        execution_inputs.validate_binding(value)


@pytest.mark.parametrize("mutation", ["absent", "scope", "input", "time", "changed", "malformed_paths"])
def test_worker_rejects_zero_exit_with_missing_or_foreign_input_receipt(tmp_path, monkeypatch, mutation):
    api, run = command_run(tmp_path, monkeypatch)
    def fake(check, root, *args):
        item = receipt(args[-1])
        if mutation == "scope":
            item["scope_hash"] = "0" * 64
        elif mutation == "input":
            item["input_hash"] = "0" * 64
        elif mutation == "time":
            item["post_checked_at"] = "2020-01-01T00:00:00Z"
        elif mutation == "changed":
            item.update(unchanged=False, changed_inputs=["data"])
        elif mutation == "malformed_paths":
            item["changed_inputs"] = [{}]
        return {"status": "completed", **({"input_receipt": item} if mutation != "absent" else {}), "capture": {
            "argv": check["argv"], "cwd": check["cwd"], "execution": "completed", "exitCode": 0,
            "startedAt": "2026-10-01T00:00:00Z", "finishedAt": "2026-10-01T00:00:01Z", "stdout": "", "stderr": ""}}
    monkeypatch.setattr(worker, "execute_sandbox", fake)
    job = api.verify(run, "verify")["job_id"]
    worker.execute_job(api.store.root, job)
    state = api.status(run, job)["job"]
    assert state["status"] == "error" and state["error"]["code"] == "RECEIPT_BINDING"
    assert len(api.records(run, "measurements", job_id=job)["items"]) == 1  # File check only.
    with pytest.raises(HarnessError):
        api.finish(run, "finish", outcome="completed")


def test_scope_covers_subject_owner_check_and_actual_materialized_command():
    from harness.common import canonical_hash
    candidate = {"manifest": {"files": []}, "executable": {}, "subject": {"id": "subject"}, "candidate_hash": "a" * 64}
    context = [candidate, "run", "job", "owner", {"sha256": "check"}, {"argv": ["a"], "cwd": ".", "timeout_seconds": 1}]
    initial = execution_inputs.job_binding(*context)
    for index in range(len(context)):
        changed = copy.deepcopy(context)
        if index == 0:
            changed[index]["subject"]["id"] = "different"
        elif index < 4:
            changed[index] += "-different"
        elif index == 4:
            changed[index]["sha256"] = "different"
        else:
            changed[index]["argv"] = ["different"]
        result = execution_inputs.job_binding(*changed)
        assert result["input_hash"] == initial["input_hash"] == canonical_hash([])
        assert result["scope_hash"] != initial["scope_hash"]


def test_external_bridge_rejects_unbound_legacy_request(tmp_path):
    bridge = Path(__file__).resolve().parents[1] / "runtime/script/external-harness-sandbox.ts"
    process = subprocess.run(["bun", "run", str(bridge)], input=json.dumps({"workspace": str(tmp_path),
        "argv": ["echo", "COMMAND_STARTED"], "cwd": ".", "timeoutMs": 1000}), text=True, capture_output=True, timeout=15)
    value = json.loads(process.stdout)
    assert value["status"] == "not_run" and "capture" not in value
    assert "COMMAND_STARTED" not in process.stdout


@pytest.mark.skipif(os.environ.get("BASE_HARNESS_EXTERNAL_LIVE") != "1", reason="real before/after sandbox receipts")
@pytest.mark.parametrize("mutation", ["bytes", "executable", "delete", "outputs_only"])
def test_live_copy_changes_measured_against_frozen_preexecution_inventory(tmp_path, mutation):
    root = tmp_path / "payload"
    root.mkdir()
    (root / "한글😀.txt").write_text("ready", encoding="utf-8")
    pinned = inputs(root)
    action = {"bytes": "p.write_text('wrong', encoding='utf-8')", "executable": "p.chmod(0o755)",
              "delete": "p.unlink()", "outputs_only": "Path('generated').write_text('allowed', encoding='utf-8')"}[mutation]
    command = {"argv": ["python3", "-c", "from pathlib import Path; p=Path('한글😀.txt'); " + action], "cwd": ".", "timeout_seconds": 10}
    result = worker.execute_sandbox(command, root, tmp_path / "state", threading.Event(), 20, pinned)
    observed = result["input_receipt"]
    assert observed["input_hash"] == pinned["input_hash"] and observed["scope_hash"] == pinned["scope_hash"]
    if mutation == "outputs_only":
        assert result["status"] == "completed" and observed["unchanged"]
    else:
        assert result["status"] == "error" and observed["unchanged"] is False
        assert observed["changed_inputs"] == ["한글😀.txt"]
        assert result["capture"]["error"]["code"] == "TEST_MODIFIED_INPUT"
    assert (root / "한글😀.txt").read_text(encoding="utf-8") == "ready"
    assert not (root / "generated").exists()


@pytest.mark.skipif(os.environ.get("BASE_HARNESS_EXTERNAL_LIVE") != "1", reason="persisted actual execution input provenance")
def test_live_worker_persists_measured_input_identity_without_changing_goal_or_gate(tmp_path, monkeypatch):
    api, run = command_run(tmp_path, monkeypatch)
    original = api.status(run)["run"]
    job = api.verify(run, "verify")["job_id"]
    worker.execute_job(api.store.root, job)
    observed = api.records(run, "measurements", job_id=job)["items"][-1]
    actual = observed["report"]["sandbox"]["execution_input"]
    with api.store.transaction(write=False) as connection:
        saved = api.store.job(connection, job)
    check = saved["checks"][-1]
    execution = {**check["spec"]["parameters"], "timeout_seconds": check["spec"]["timeoutMs"] // 1000}
    expected = execution_inputs.job_binding(saved["candidate"], run, job, saved["owner"], check["ref"], execution)
    assert actual["scope_hash"] == expected["scope_hash"] and actual["input_hash"] == expected["input_hash"]
    assert actual["unchanged"] and observed["comparison_status"] == "failed"  # True input identity != passing behavior.
    current = api.status(run)["run"]
    assert current["contract"] == original["contract"] and current["gate_bindings"] == original["gate_bindings"]
