"""External caller recovery, check scope, cancellation and maintenance."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from harness.common import canonical_bytes
from harness_external.errors import HarnessError
from harness_external.service import Harness
from harness_external import worker, maintenance, diagnostics


@pytest.fixture
def case(tmp_path, monkeypatch):
    ws = tmp_path / "project"
    ws.mkdir()
    (ws / "a.txt").write_text("a")
    (ws / "b.txt").write_text("b")
    params = {"profile": "structural", "inputs": ["a.txt", "b.txt"], "artifacts": ["a.txt"],
              "expectations": [{"path": "a.txt", "operator": "equals", "expected": "a"}]}
    api = Harness(tmp_path / "state")
    monkeypatch.setattr(worker, "spawn_worker", lambda *args: None)
    def start(key="start", **options):
        return api.start(domain_id="develop", goal="Inspect files", workspace=str(ws), parameters=options.pop("parameters", params),
                         mode=options.pop("mode", "exploratory"), request_id=key, **options)["run_id"]
    return api, start, params


def check(api, run, key="probe", *, command=False, expected=0, interpretation=1):
    spec = {"kind": "command", "argv": ["python3", "-c", "pass"]} if command else {"kind": "file", "path": "b.txt", "operator": "equals", "expected": "b"}
    return api.register_check(run, {"check_id": key, "interpretation_revision": interpretation,
                                   "expected_revision": expected, "parameters": spec}, f"{key}-{interpretation}-{expected}")


def retire(api, run, key="probe", *, expected=1, interpretation=1):
    return api.retire_check(run, {"check_id": key, "expected_revision": expected, "interpretation_revision": interpretation,
                                  "reason": "Probe no longer applies"}, f"retire-{key}-{expected}")


def measure(api, run, key="verify"):
    job = api.verify(run, key)["job_id"]
    worker.execute_job(api.store.root, job)
    return api.status(run, job)


def test_scope_revision_requires_retiring_incompatible_probe(case):
    api, start, _ = case
    run = start()
    check(api, run)
    api.submit(run, "original")
    before = api.resume(run)
    proposal = {"expected_revision": 1, "parameters": {"inputs": ["a.txt"]}}
    with pytest.raises(HarnessError) as error:
        api.revise(run, proposal, "narrow")
    assert error.value.code == "CHECK_SCOPE_CONFLICT"
    assert api.resume(run) == before
    assert retire(api, run) == retire(api, run)
    api.revise(run, proposal, "narrow")
    api.submit(run, "new")
    assert measure(api, run)["job"]["result"]["status"] == "passed"
    assert len(api.records(run, "checks")["items"]) == 3


def test_structural_revision_cannot_keep_command_probe(case):
    api, start, params = case
    execution = {**params, "profile": "execution", "test_commands": [{"argv": ["python3", "-c", "pass"]}]}
    run = start(parameters=execution)
    check(api, run, command=True)
    proposal = {"expected_revision": 1, "parameters": {"profile": "structural", "test_commands": []}}
    with pytest.raises(HarnessError) as error:
        api.revise(run, proposal, "structural")
    assert error.value.code == "CHECK_SCOPE_CONFLICT"
    retire(api, run)
    api.revise(run, proposal, "structural")
    api.submit(run, "submit")
    assert measure(api, run)["job"]["result"]["status"] == "passed"
    assert all(c["kind"] == "file" for c in api.resume(run)["checks"])


def test_domain_check_reactivation_is_monotonic(case):
    api, start, params = case
    execution = {**params, "profile": "execution", "test_commands": [{"argv": ["python3", "-c", "pass"]}]}
    run = start(parameters=execution)
    api.revise(run, {"expected_revision": 1, "parameters": {"profile": "structural", "test_commands": []}}, "remove")
    api.revise(run, {"expected_revision": 2, "parameters": execution}, "restore")
    checks = [c for c in api.records(run, "checks")["items"] if c["check_id"] == "command-0"]
    assert [c["ref"]["revision"] for c in checks] == [1, 2, 3]
    assert [c["active"] for c in checks] == [True, False, True]


def test_retired_probe_reactivation_keeps_measurement_history(case):
    api, start, _ = case
    run = start()
    first = check(api, run)["check"]
    api.submit(run, "submit")
    measure(api, run)
    retire(api, run)
    with pytest.raises(HarnessError) as error:
        check(api, run, expected=1)
    assert error.value.code == "STALE_CHECK"
    restored = check(api, run, expected=2)["check"]
    assert restored["ref"]["revision"] == 3
    assert any(m["check_ref"] == first["ref"] for m in api.records(run, "measurements")["items"])
    assert not any(m["check_ref"] == restored["ref"] for m in api.records(run, "measurements")["items"])


def test_required_probe_cannot_be_retired_or_removed_by_scope(case):
    api, start, _ = case
    run = start(deferred_checks=["probe"])
    check(api, run)
    with pytest.raises(HarnessError) as error:
        retire(api, run)
    assert error.value.code == "GATE_POLICY_CHANGED"
    with pytest.raises(HarnessError):
        api.revise(run, {"expected_revision": 1, "parameters": {"inputs": ["a.txt"]}}, "scope")
    assert api.resume(run)["gates"]["status"] == "unsatisfied"


def test_late_spawn_failure_does_not_change_closed_run(case, monkeypatch):
    api, start, _ = case
    run = start()
    api.submit(run, "submit")
    def late_failure(*args):
        api.finish(run, "finish", outcome="abandoned")
        raise OSError("Controlled late failure")
    monkeypatch.setattr(worker, "spawn_worker", late_failure)
    job = api.verify(run, "verify")["job_id"]
    state = api.status(run)
    assert state["run"]["phase"] == "finished"
    assert state["run"]["verification"] == state["run"]["record"]["verification"]
    assert api.status(run, job)["job"]["status"] == "cancelled"
    assert api.status(run, job)["job"]["cleanup"] == "complete"


def test_cancel_and_late_spawn_failure_cannot_clear_new_job(case, monkeypatch):
    api, start, _ = case
    run = start()
    api.submit(run, "submit")
    newer = []
    def late_failure(state, old):
        api.cancel(run, old, "cancel")
        monkeypatch.setattr(worker, "spawn_worker", lambda *a: None)
        newer.append(api.verify(run, "new")["job_id"])
        raise OSError("Controlled late failure")
    monkeypatch.setattr(worker, "spawn_worker", late_failure)
    old = api.verify(run, "old")["job_id"]
    assert api.resume(run)["run"]["active_job"] == newer[0]
    assert api.status(run, old)["job"]["status"] == "cancelled"
    worker.execute_job(api.store.root, newer[0])
    assert api.resume(run)["measurement"]["status"] == "passed"


def test_cancel_running_job_rejects_late_measurement_and_allows_retry(case, monkeypatch):
    api, start, params = case
    run = start(parameters={**params, "profile": "execution", "test_commands": [{"argv": ["python3", "-c", "pass"]}]})
    api.submit(run, "submit")
    old = api.verify(run, "old")["job_id"]
    entered, release = threading.Event(), threading.Event()
    def blocked(*args):
        entered.set()
        assert release.wait(8)
        return {"status": "not_run", "reason": "Controlled cancellation"}
    monkeypatch.setattr(worker, "execute_sandbox", blocked)
    with ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(worker.execute_job, api.store.root, old)
        try:
            assert entered.wait(4)
            cancelled = api.cancel(run, old, "cancel")
            assert cancelled["job"]["cleanup"] == "pending"
            new = api.verify(run, "new")["job_id"]
        finally:
            release.set()
        task.result(timeout=5)
    assert api.cancel(run, old, "cancel") == cancelled
    assert api.resume(run)["run"]["active_job"] == new
    assert api.status(run, old)["job"]["cleanup"] == "complete"
    assert len(api.records(run, "measurements", job_id=old)["items"]) == 1
    api.cancel(run, new, "cancel-new")


def test_paginated_recovery_survives_run_updates_and_preserves_gates(case):
    api, start, _ = case
    first, second = start("first", required_checks=["file-0"]), start("second")
    assert api.list_runs(limit=1)["items"][0]["run_id"] == first
    api.submit(first, "submit")
    check(api, first)
    measure(api, first)
    restarted = Harness(api.store.root)
    assert restarted.list_runs(offset=1, limit=1)["items"][0]["run_id"] == second
    assert restarted.list_runs(offset=1, limit=1)["next_offset"] is None
    summary = restarted.resume(first)
    assert summary["gates"] == restarted.status(first)["closeout"]["gates"]
    assert summary["gates"]["status"] == "passed"
    assert "observations" not in summary["recent_jobs"][0]
    page = restarted.records(first, "measurements", limit=1)
    next_page = restarted.records(first, "measurements", offset=page["next_offset"], limit=1)
    assert len(page["items"]) == len(next_page["items"]) == 1
    assert page["items"][0]["observation_id"] != next_page["items"][0]["observation_id"]
    assert next_page["next_offset"] is None
    foreign = restarted.records(first, "jobs")["items"][0]["job_id"]
    with pytest.raises(HarnessError):
        restarted.records(second, "measurements", job_id=foreign)
    with pytest.raises(HarnessError):
        restarted.records(first, "notes", limit=101)


def test_summary_checks_completion_record_integrity(case):
    api, start, _ = case
    run = start()
    api.finish(run, "finish", outcome="partial")
    with api.store.transaction() as connection:
        saved = api.store.run(connection, run)
        saved["record"]["outcome"] = "completed"
        api.store.save_run(connection, saved)
    with pytest.raises(HarnessError) as error:
        api.resume(run)
    assert error.value.code == "RECORD_CORRUPT"


@pytest.mark.skipif(os.name != "posix", reason="POSIX capture recovery locks")
def test_cleanup_quarantines_only_unlocked_abandoned_staging(case):
    api, start, _ = case
    run = start()
    candidate = api.submit(run, "submit")["candidate"]
    directory = api.store.directory(run)
    abandoned = Path(tempfile.mkdtemp(prefix=".submit_", dir=directory))
    active = Path(tempfile.mkdtemp(prefix=".submit_", dir=directory))
    with maintenance.capture_lease(abandoned):
        (abandoned / "retained.txt").write_text("recover me")
    past = time.time() - 120
    os.utime(abandoned, (past, past))
    with maintenance.capture_lease(active):
        os.utime(active, (past, past))
        preview = api.cleanup(run, min_age_seconds=60)
        assert {i["state"] for i in preview["items"]} == {"active", "abandoned"}
        assert abandoned.exists()
        result = api.cleanup(run, apply=True, min_age_seconds=60)
        recovered = next(i for i in result["items"] if i["state"] == "quarantined")
        assert (Path(recovered["recovery_path"]) / "retained.txt").read_text() == "recover me"
        assert active.exists() and not abandoned.exists()
    assert api.resume(run)["candidate"]["candidate_id"] == candidate["candidate_id"]
    assert (directory / candidate["candidate_id"] / "payload/a.txt").read_text() == "a"


def test_doctor_does_not_claim_sandbox_without_probe(case, monkeypatch):
    api, _, _ = case
    result = diagnostics.doctor(api.store)
    assert result["healthy"] and result["sandbox_probe"] == "not_requested"
    assert result["command_execution_verified"] is False
    monkeypatch.setattr(diagnostics, "execute_sandbox", lambda *a: {"status": "not_run", "reason": "Controlled unavailable adapter"})
    assert diagnostics.doctor(api.store, sandbox=True)["healthy"] is False


def test_cli_recovery_tools_roundtrip(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "result.txt").write_text("result")
    state = tmp_path / "state"
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    def cli(*args):
        result = subprocess.run([sys.executable, "-m", "harness_external", "--state-dir", str(state), *args],
                                env=env, capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stdout + result.stderr
        return json.loads(result.stdout)
    assert cli("doctor")["structural_available"]
    run = cli("start", "--workspace", str(ws), "--goal", "Inspect output", "--mode", "exploratory", "--request-id", "start")["run_id"]
    assert cli("list-runs", "--limit", "1")["items"][0]["run_id"] == run
    assert cli("resume", "--run-id", run)["run"]["lifecycle"] == "waiting_input"
    assert cli("records", "--run-id", run, "--kind", "interpretations", "--limit", "1")["total"] == 1
    assert cli("cleanup", "--run-id", run)["items"] == []
    proposal = tmp_path / "proposal.json"
    proposal.write_text(json.dumps({"expected_revision": 1, "parameters": {
        "profile": "structural", "inputs": ["result.txt"], "artifacts": ["result.txt"],
        "expectations": [{"path": "result.txt", "operator": "equals", "expected": "result"}]}}))
    cli("revise", "--run-id", run, "--data", str(proposal), "--request-id", "revise")
    proposal.write_text(json.dumps({"check_id": "probe", "interpretation_revision": 2,
                                    "parameters": {"kind": "file", "path": "result.txt", "operator": "equals", "expected": "hypothesis"}}))
    cli("check", "--run-id", run, "--data", str(proposal), "--request-id", "check")
    proposal.write_text(json.dumps({"check_id": "probe", "interpretation_revision": 2, "expected_revision": 1, "reason": "Finished exploration"}))
    cli("retire-check", "--run-id", run, "--data", str(proposal), "--request-id", "retire")
    cli("submit", "--run-id", run, "--request-id", "submit")
    job = cli("verify", "--run-id", run, "--request-id", "verify")["job_id"]
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        summary = cli("resume", "--run-id", run)
        if summary["run"]["active_job"] is None:
            break
        time.sleep(0.05)
    assert summary["measurement"]["status"] == "passed"
    assert cli("records", "--run-id", run, "--kind", "measurements", "--job-id", job)["total"] == 1
    assert cli("cancel", "--run-id", run, "--job-id", job, "--request-id", "cancel-finished")["job"]["status"] == "completed"
    cli("finish", "--run-id", run, "--outcome", "completed", "--request-id", "finish")
    assert cli("resume", "--run-id", run)["run"]["lifecycle"] == "closed"


@pytest.mark.skipif(os.name != "posix", reason="WSL/Linux checkout launcher")
def test_checkout_launcher_works_from_other_directory(tmp_path):
    launcher = Path(__file__).resolve().parents[1] / "scripts/harness-tool"
    state = tmp_path / "상태 with spaces"
    environment = {**os.environ, "PATH": os.defpath}
    environment.pop("BUN", None)
    result = subprocess.run(["bash", str(launcher), "--state-dir", str(state), "doctor"], cwd=tmp_path, env=environment,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr + result.stdout
    data = json.loads(result.stdout)
    assert data["state_directory"] == str(state)
    assert data["healthy"] and not data["command_execution_verified"]
    standard_bun = Path.home() / ".bun/bin/bun"
    if standard_bun.is_file():
        assert data["bun"] is not None
