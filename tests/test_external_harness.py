import json
import ast
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from harness.common import canonical_hash
from harness_external.domain import HarnessError
from harness_external.service import Harness, LEASE_SECONDS
from harness_external.worker import execute_job


@pytest.fixture
def case(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "main.py").write_text("print('hello')\n")
    monkeypatch.setattr("harness_external.worker.spawn_worker", lambda *args: None)
    parameters = {"inputs": ["main.py"], "artifacts": ["main.py"], "profile": "structural",
                  "expectations": [{"path": "main.py", "operator": "contains", "expected": "hello"}]}
    api = Harness(tmp_path / "state")
    return api, workspace, parameters


def start(case, **kwargs):
    api, workspace, parameters = case
    result = api.start(domain_id="develop", goal="Print a greeting", workspace=str(workspace), parameters=parameters,
                       request_id=kwargs.pop("request_id", "start"), **kwargs)
    return result["run_id"]


def verify(api, run_id, key="verify"):
    job = api.verify(run_id, key)["job_id"]
    execute_job(api.store.root, job)
    return api.status(run_id, job)["job"]


def test_full_flow_uses_real_measurement_without_host_or_ready(case):
    api, workspace, _ = case
    run_id = start(case)
    api.observe(run_id, {"note": "I think this is ready"}, "note")
    candidate = api.submit(run_id, "submit")["candidate"]
    result = verify(api, run_id)
    assert result["result"]["status"] == "passed"
    report = result["result"]["observations"][0]["observation"]
    assert report["producer"] == {"kind": "verifier", "id": "python-measurement", "revision": "5"}
    assert report["subject"]["sha256"] == canonical_hash(candidate["manifest"])
    assert api.status(run_id)["run"]["observations"][0]["trust"] == "untrusted"
    record = api.finish(run_id, "finish", outcome="completed")["record"]
    assert record["ready"] is False and record["signature"] is None
    assert record["assurance"] == "local-advisory"
    assert record["candidate_hash"] == candidate["candidate_hash"]
    assert record["verification"]["result_hash"] == result["result"]["result_hash"]
    assert "session" not in json.dumps(api.status(run_id)).lower()
    assert (workspace / "main.py").read_text() == "print('hello')\n"


def test_contract_pinned_and_no_check_policy_or_ready_in_observe(case):
    api, _, parameters = case
    run_id = start(case)
    parameters["expectations"][0]["expected"] = "weakened"
    assert api.status(run_id)["run"]["contract"]["checks"][0]["expected"] == "hello"
    with pytest.raises(HarnessError, match="Unexpected"):
        api.observe(run_id, {"note": "done", "ready": True, "verifier_result": "passed"}, "bad")
    with pytest.raises(HarnessError) as error:
        api.finish(run_id, "finish", outcome="completed")
    assert error.value.code == "VERIFICATION_REQUIRED"


@pytest.mark.parametrize("change", [
    {"inputs": ["../escape"]}, {"inputs": ["/absolute"]}, {"inputs": ["a", "A"]},
    {"inputs": ["a", "a/b"]}, {"inputs": ["CON"]}, {"profile": "execution"},
    {"expectations": []}, {"checks": [{"ready": True}]},
    {"expectations": [{"path": "main.py", "operator": "contains", "expected": ""}]},
])
def test_invalid_task_parameters_do_not_create_runs(case, change):
    api, _, parameters = case
    parameters.update(change)
    with pytest.raises(HarnessError):
        start(case)
    with api.store.transaction() as connection:
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 0


def test_state_inside_workspace_rejected(case):
    _, workspace, parameters = case
    api = Harness(workspace / "state")
    with pytest.raises(HarnessError) as error:
        api.start(domain_id="develop", goal="goal", workspace=str(workspace), parameters=parameters, request_id="start")
    assert error.value.code == "STATE_WORKSPACE_OVERLAP"
    assert not (workspace / "state").exists()


def test_idempotency_prevents_duplicate_work_and_rejects_conflict(case):
    api, workspace, parameters = case
    run_id = start(case)
    assert start(case) == run_id
    with pytest.raises(HarnessError) as error:
        api.start(domain_id="develop", goal="different", workspace=str(workspace), parameters=parameters, request_id="start")
    assert error.value.code == "REQUEST_CONFLICT"
    first = api.submit(run_id, "same")
    assert api.submit(run_id, "same") == first
    with pytest.raises(HarnessError):
        api.observe(run_id, {"note": "no"}, "same")
    job = api.verify(run_id, "verify")
    assert api.verify(run_id, "verify") == job
    assert api.status(run_id)["run"]["verification_attempts"] == 1
    assert api.status(run_id)["run"]["actions"] == 2


def test_concurrent_verify_has_one_owner(case):
    api, _, _ = case
    run_id = start(case)
    api.submit(run_id, "submit")
    def attempt(index):
        try:
            return api.verify(run_id, "verify-" + str(index))
        except HarnessError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, [1, 2]))
    assert sum(isinstance(item, dict) for item in results) == 1
    assert "VERIFICATION_ACTIVE" in results
    assert api.status(run_id)["run"]["verification_attempts"] == 1
    with pytest.raises(HarnessError):
        api.submit(run_id, "second-submit")


def test_failed_check_cannot_finish_and_new_submit_requires_new_verification(case):
    api, workspace, _ = case
    run_id = start(case)
    (workspace / "main.py").write_text("wrong")
    api.submit(run_id, "submit")
    assert verify(api, run_id)["result"]["status"] == "failed"
    with pytest.raises(HarnessError):
        api.finish(run_id, "f", outcome="completed")
    (workspace / "main.py").write_text("hello")
    api.submit(run_id, "submit-2")
    assert api.status(run_id)["run"]["verification"]["status"] == "not_run"
    with pytest.raises(HarnessError):
        api.finish(run_id, "f", outcome="completed")
    assert verify(api, run_id, "verify-2")["result"]["status"] == "passed"


def test_workspace_edits_do_not_change_snapshot_but_snapshot_edits_reject_finish(case):
    api, workspace, _ = case
    run_id = start(case)
    candidate = api.submit(run_id, "submit")["candidate"]
    (workspace / "main.py").write_text("later workspace edit")
    assert verify(api, run_id)["result"]["status"] == "passed"
    assert api.status(run_id, check_workspace=True)["current_workspace_matches_submitted_files"] is False
    snapshot = api.store.directory(run_id) / candidate["candidate_id"] / "payload" / "main.py"
    snapshot.chmod(0o600)
    snapshot.write_text("tampered")
    with pytest.raises(HarnessError) as error:
        api.finish(run_id, "finish", outcome="completed")
    assert error.value.code == "CANDIDATE_CORRUPT"


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_snapshot_rejects_linked_inputs(case, kind):
    api, workspace, _ = case
    run_id = start(case)
    target = workspace / "main.py"
    other = workspace / "original.py"
    target.rename(other)
    try:
        if kind == "symlink":
            target.symlink_to(other)
        else:
            os.link(other, target)
    except OSError:
        pytest.skip("platform does not support creating the test link")
    with pytest.raises((HarnessError, OSError)):
        api.submit(run_id, "submit")
    assert api.status(run_id)["run"]["candidate"] is None


def test_expired_lease_is_interrupted_and_does_not_resume(case):
    api, _, _ = case
    run_id = start(case)
    api.submit(run_id, "submit")
    job_id = api.verify(run_id, "verify")["job_id"]
    with api.store.transaction() as connection:
        job = api.store.job(connection, job_id)
        job["heartbeat_at"] = time.time() - LEASE_SECONDS - 1
        api.store.save_job(connection, job)
    restarted = Harness(api.store.root)
    assert restarted.status(run_id, job_id)["job"]["status"] == "interrupted"
    execute_job(api.store.root, job_id)
    assert restarted.status(run_id)["run"]["verification"]["status"] == "interrupted"


def test_budget_exhaustion_is_persisted_and_handoff_not_ready(case):
    api, _, _ = case
    run_id = start(case, budget={"max_verifications": 1})
    api.submit(run_id, "submit")
    verify(api, run_id)
    with pytest.raises(HarnessError) as error:
        api.verify(run_id, "extra")
    assert error.value.code == "VERIFICATION_BUDGET_EXHAUSTED"
    assert api.status(run_id)["run"]["status"] == "blocked"
    assert api.finish(run_id, "handoff", outcome="partial")["record"]["ready"] is False


def test_deadline_expiration_persists_and_old_job_cannot_complete(case):
    api, _, _ = case
    run_id = start(case)
    with api.store.transaction() as connection:
        run = api.store.run(connection, run_id)
        run["deadline_at"] = time.time() - 1
        api.store.save_run(connection, run)
    with pytest.raises(HarnessError) as error:
        api.submit(run_id, "submit")
    assert error.value.code == "DEADLINE_EXCEEDED"
    assert Harness(api.store.root).status(run_id)["run"]["status"] == "blocked"


def test_job_identity_does_not_cross_runs(case):
    api, _, _ = case
    first = start(case)
    second = start(case, request_id="other-start")
    api.submit(first, "submit")
    job = api.verify(first, "verify")["job_id"]
    with pytest.raises(HarnessError) as error:
        api.status(second, job)
    assert error.value.code == "JOB_RUN_MISMATCH"


def test_abandon_during_command_discards_late_result(case, monkeypatch):
    api, _, parameters = case
    parameters.update(profile="execution", test_commands=[{"argv": ["python3", "-c", "print('test')"]}])
    started, release = threading.Event(), threading.Event()
    def slow_command(*args):
        started.set()
        assert release.wait(5)
        return {"status": "not_run", "reason": "test cancellation"}
    monkeypatch.setattr("harness_external.worker.execute_sandbox", slow_command)
    run_id = start(case)
    api.submit(run_id, "submit")
    job_id = api.verify(run_id, "verify")["job_id"]
    worker = threading.Thread(target=execute_job, args=(api.store.root, job_id))
    worker.start()
    try:
        assert started.wait(5)
        api.finish(run_id, "finish", outcome="abandoned")
    finally:
        release.set()
        worker.join(5)
    state = api.status(run_id)
    assert state["run"]["status"] == "finished"
    assert state["jobs"][0]["status"] == "cancelled"
    assert state["jobs"][0]["result"] is None
    assert state["jobs"][0]["cleanup"] == "complete"


def test_unavailable_sandbox_is_not_run_and_has_no_fallback(case, monkeypatch):
    api, _, parameters = case
    parameters.update(profile="execution", test_commands=[{"argv": ["python3", "-c", "print('test')"]}])
    monkeypatch.setattr("harness_external.worker.execute_sandbox", lambda *args: {"status": "not_run", "reason": "sandbox unavailable"})
    run_id = start(case)
    api.submit(run_id, "submit")
    result = verify(api, run_id)["result"]
    assert result["status"] == "incomplete"
    assert result["observations"][-1]["observation"]["result"]["execution"] == "not_run"
    with pytest.raises(HarnessError):
        api.finish(run_id, "finish", outcome="completed")


def test_contract_corruption_detected(case):
    api, _, _ = case
    run_id = start(case)
    with api.store.transaction() as connection:
        run = api.store.run(connection, run_id)
        run["contract"]["checks"] = []
        api.store.save_run(connection, run)
    with pytest.raises(HarnessError) as error:
        api.status(run_id)
    assert error.value.code == "CONTRACT_CORRUPT"


def test_completion_does_not_need_an_extra_action_budget_slot(case):
    api, _, _ = case
    run_id = start(case, budget={"max_actions": 2})
    api.submit(run_id, "submit")
    verify(api, run_id)
    assert api.finish(run_id, "finish", outcome="completed")["record"]["outcome"] == "completed"


def test_result_corruption_rejects_completion(case):
    api, _, _ = case
    run_id = start(case)
    api.submit(run_id, "submit")
    job = verify(api, run_id)
    with api.store.transaction() as connection:
        saved = api.store.job(connection, job["job_id"])
        saved["result"]["observations"] = []
        api.store.save_job(connection, saved)
    with pytest.raises(HarnessError) as error:
        api.finish(run_id, "finish", outcome="completed")
    assert error.value.code == "RESULT_CORRUPT"


def test_repeated_worker_delivery_does_not_rerun_checks(case, monkeypatch):
    api, _, _ = case
    run_id = start(case)
    api.submit(run_id, "submit")
    job = verify(api, run_id)
    monkeypatch.setattr("harness_external.worker.validate_candidate", lambda *args: pytest.fail("duplicate worker ran"))
    execute_job(api.store.root, job["job_id"])
    assert api.status(run_id, job["job_id"])["job"]["result"] == job["result"]


def test_external_package_has_no_host_ui_or_provider_imports():
    root = Path(__file__).resolve().parents[1]
    for source in (root / "src" / "harness_external").glob("*.py"):
        for node in ast.walk(ast.parse(source.read_text())):
            names = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            assert not any(name.startswith(("harness_ui", "harness.model", "harness.core")) for name in names)
    bridge = (root / "runtime" / "script" / "external-harness-sandbox.ts").read_text()
    assert "../packages/security/src/sandbox" in bridge
    assert "packages/base-harness" not in bridge
    extracted = json.loads((root / "src" / "harness_external" / "develop_manifest.json").read_text())
    original = json.loads((root / "runtime" / "packages" / "domain" / "resources" / "develop" / "spec.json").read_text())
    assert extracted == original


def test_snapshot_rechecks_source_before_sealing(case, monkeypatch):
    from harness_external import snapshots
    api, workspace, _ = case
    run_id = start(case)
    original_read = snapshots.read_file
    edited = False
    def racing_read(root, relative):
        nonlocal edited
        result = original_read(root, relative)
        if root == workspace and not edited:
            edited = True
            (workspace / relative).write_text("changed during capture")
        return result
    monkeypatch.setattr(snapshots, "read_file", racing_read)
    with pytest.raises(HarnessError) as error:
        api.submit(run_id, "submit")
    assert error.value.code == "INPUT_CHANGED"
    assert api.status(run_id)["run"]["candidate"] is None


def test_real_cli_async_worker_and_restart(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "result.txt").write_text("actual result\n")
    params = tmp_path / "parameters.json"
    params.write_text(json.dumps({"profile": "structural", "inputs": ["result.txt"], "artifacts": ["result.txt"],
                                  "expectations": [{"path": "result.txt", "operator": "equals", "expected": "actual result\n"}]}))
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    def cli(*args, expected=0):
        child = subprocess.run([sys.executable, "-m", "harness_external", "--state-dir", str(tmp_path / "state"), *args],
                               env=environment, capture_output=True, text=True, timeout=20)
        assert child.returncode == expected, child.stderr + child.stdout
        return json.loads(child.stdout)
    result = cli("start", "--workspace", str(workspace), "--goal", "write actual result", "--parameters", str(params), "--request-id", "start")
    run_id = result["run_id"]
    cli("submit", "--run-id", run_id, "--request-id", "submit")
    job = cli("verify", "--run-id", run_id, "--request-id", "verify")
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        state = cli("status", "--run-id", run_id, "--job-id", job["job_id"])["job"]
        if state["status"] not in {"queued", "running"}:
            break
        time.sleep(0.1)
    assert state["status"] == "completed", state
    assert state["result"]["status"] == "passed"
    finished = cli("finish", "--run-id", run_id, "--request-id", "finish", "--outcome", "completed")
    assert not finished["ready"] and finished["record"]["signature"] is None
    assert cli("status", "--run-id", run_id)["run"]["record"] == finished["record"]
    assert (workspace / "result.txt").read_text() == "actual result\n"


@pytest.mark.skipif(os.environ.get("BASE_HARNESS_EXTERNAL_LIVE") != "1", reason="opt-in actual namespace Sandbox test")
def test_live_sandboxed_python_tests_without_host(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "tests").mkdir()
    (workspace / "main.py").write_text("def add(a, b):\n    return a + b\n")
    (workspace / "tests" / "test_main.py").write_text(
        "import os, unittest\nfrom main import add\n"
        "class AddTest(unittest.TestCase):\n"
        "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n"
        "    def test_clean_environment(self):\n        self.assertNotIn('HARNESS_TEST_SECRET', os.environ)\n"
    )
    parameters = {"inputs": ["main.py", "tests/test_main.py"], "artifacts": ["main.py"],
                  "expectations": [{"path": "main.py", "operator": "contains", "expected": "return a + b"}],
                  "test_commands": [{"argv": ["python3", "-m", "unittest", "discover", "-s", "tests", "-v"]}]}
    api = Harness(tmp_path / "state")
    run_id = api.start(domain_id="develop", goal="Implement integer addition and test it", workspace=str(workspace),
                       parameters=parameters, request_id="start")["run_id"]
    candidate = api.submit(run_id, "submit")["candidate"]
    job_id = api.verify(run_id, "verify")["job_id"]
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        job = api.status(run_id, job_id)["job"]
        if job["status"] not in {"queued", "running"}:
            break
        time.sleep(0.2)
    assert job["status"] == "completed", job
    assert job["result"]["status"] == "passed", job
    command = job["result"]["observations"][-1]
    assert command["sandbox"]["containment"] == "user_mount_pid_net_namespace"
    assert command["sandbox"]["network"] == "loopback_only"
    record = api.finish(run_id, "finish", outcome="completed")["record"]
    assert not record["ready"] and record["signature"] is None
    assert (workspace / "main.py").read_text() == "def add(a, b):\n    return a + b\n"
    assert not (workspace / "__pycache__").exists()
    print(json.dumps({"run_id": run_id, "job_id": job_id, "candidate_hash": candidate["candidate_hash"],
                      "verification": job["result"]["status"], "sandbox": command["sandbox"],
                      "ready": record["ready"], "assurance": record["assurance"]}))
