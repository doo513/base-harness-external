import io
from pathlib import Path
import subprocess
import time
import threading

import pytest

from harness_external import worker
from harness_external.errors import HarnessError
from harness_external.service import Harness


def test_outer_sandbox_timeout_uses_monotonic_clock_and_closes_streams(tmp_path, monkeypatch):
    class Child:
        stopped = False
        returncode = None
        stdin, stdout, stderr = io.BytesIO(), io.BytesIO(), io.BytesIO()
        def communicate(self, *args, **kwargs):
            time.sleep(.01)
            raise subprocess.TimeoutExpired("fake transport", kwargs["timeout"])
        def poll(self):
            return 0 if self.stopped else None
    child = Child()
    def stop(instance):
        instance.stopped = True
    monkeypatch.setattr(worker.subprocess, "Popen", lambda *args, **kwargs: child)
    monkeypatch.setattr(worker.shutil, "which", lambda *args, **kwargs: "/fake/bun")
    monkeypatch.setattr(worker, "stop_child", stop)
    # Clock jumps cannot extend the outer elapsed-time allowance. Persisted Run
    # deadlines and heartbeat wall timestamps are tested separately in Core.
    monkeypatch.setattr(worker.time, "time", lambda: -1000000)
    started = time.monotonic()
    with pytest.raises(HarnessError) as error:
        worker.execute_sandbox({"argv": ["python3", "-c", "pass"], "cwd": ".", "timeout_seconds": 1},
                               tmp_path, tmp_path, threading.Event(), .05,
                               worker.execution_inputs.make_binding([], {"test": "outer-timeout"}))
    assert error.value.code == "VERIFICATION_CANCELLED"
    assert time.monotonic() - started < 1
    assert child.stopped and all(stream.closed for stream in (child.stdin, child.stdout, child.stderr))


def setup(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "data").write_text("ready", encoding="utf-8")
    api = Harness(tmp_path / "state")
    monkeypatch.setattr(worker, "spawn_worker", lambda *args: None)
    run = api.start(domain_id="develop", goal="Observe actual bytes", workspace=str(workspace), request_id="start",
                    mode="exploratory", required_checks=["domain.data"], parameters={"profile": "structural",
                    "inputs": ["data"], "artifacts": ["data"], "expectations": [
                        {"id": "data", "path": "data", "operator": "equals", "expected": "ready"},
                        {"id": "contains", "path": "data", "operator": "contains", "expected": "read"}]})["run_id"]
    api.submit(run, "submit")
    return api, run, api.verify(run, "verify")["job_id"]


def test_candidate_has_fresh_pre_post_and_final_checks_without_duplicate_subject_open(tmp_path, monkeypatch):
    api, run, job = setup(tmp_path, monkeypatch)
    calls = []
    original = worker.validate_candidate
    def counted(candidate, root):
        calls.append(candidate["candidate_id"])
        return original(candidate, root)
    monkeypatch.setattr(worker, "validate_candidate", counted)
    worker.execute_job(api.store.root, job)
    assert api.status(run, job)["job"]["result"]["status"] == "passed"
    assert len(calls) == 5  # Two Checks: pre/post each, then final integrity.


def test_post_measurement_integrity_failure_never_publishes_success(tmp_path, monkeypatch):
    api, run, job = setup(tmp_path, monkeypatch)
    original = worker.MeasurementEngine.handle
    def change_after_measurement(instance, envelope):
        result = original(instance, envelope)
        if envelope["type"] == "measure":
            target = Path(instance.root) / "data"
            target.chmod(0o644)
            target.write_text("tampered after measurement", encoding="utf-8")
        return result
    monkeypatch.setattr(worker.MeasurementEngine, "handle", change_after_measurement)
    worker.execute_job(api.store.root, job)
    state = api.status(run, job)
    assert state["job"]["status"] == "error"
    assert not api.records(run, "measurements")["items"]
    assert api.resume(run)["gates"]["status"] == "unsatisfied"


def test_changed_snapshot_before_first_check_is_rejected(tmp_path, monkeypatch):
    api, run, job = setup(tmp_path, monkeypatch)
    candidate = api.status(run)["run"]["candidate"]
    target = api.store.directory(run) / candidate["candidate_id"] / "payload/data"
    target.chmod(0o644)
    target.write_text("changed before checking", encoding="utf-8")
    worker.execute_job(api.store.root, job)
    assert api.status(run, job)["job"]["status"] == "error"
    assert api.records(run, "measurements")["items"] == []


@pytest.mark.parametrize("after_check", [1, 2])
def test_change_after_checkpoint_is_detected_before_next_check_or_final_result(tmp_path, monkeypatch, after_check):
    api, run, job = setup(tmp_path, monkeypatch)
    candidate = api.status(run)["run"]["candidate"]
    target = api.store.directory(run) / candidate["candidate_id"] / "payload/data"
    original = worker.Store.checkpoint
    completed = 0
    def change_after_checkpoint(store, *args, **kwargs):
        nonlocal completed
        result = original(store, *args, **kwargs)
        completed += 1
        if completed == after_check:
            target.chmod(0o644)
            target.write_text("changed between integrity boundaries", encoding="utf-8")
        return result
    monkeypatch.setattr(worker.Store, "checkpoint", change_after_checkpoint)
    worker.execute_job(api.store.root, job)
    status = api.status(run, job)["job"]
    assert status["status"] == "error" and status["result"]["status"] == "incomplete"
    assert len(api.records(run, "measurements")["items"]) == after_check
    with pytest.raises(HarnessError):
        api.finish(run, "finish", outcome="completed")
    assert api.status(run)["run"]["status"] != "finished"
