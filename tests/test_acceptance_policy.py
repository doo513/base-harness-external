"""Configured acceptance is separate from revisable model exploration."""
import copy
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from harness_external import acceptance
from harness_external.errors import HarnessError
from harness_external.service import Harness
from harness_external.worker import execute_job


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.setattr("harness_external.worker.spawn_worker", lambda *args: None)
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "calculator.py").write_text("def add(a, b): return 0\n")
    root = tmp_path / "policies"
    policy = root / "sum"
    tests = policy / "bundle" / "acceptance_tests"
    tests.mkdir(parents=True)
    (tests / "test_sum.py").write_text(
        "import unittest\nfrom calculator import add\n"
        "class SumTest(unittest.TestCase):\n"
        "    def test_positive(self): self.assertEqual(add(2, 3), 5)\n"
        "    def test_negative(self): self.assertEqual(add(-3, 1), -2)\n")
    definition = {"schema_version": "acceptance-policy-v1", "policy_id": "sum", "revision": "1", "domain_id": "develop",
                  "parameters": {"profile": "execution", "inputs": ["calculator.py", "acceptance_tests/test_sum.py"],
                                 "artifacts": ["calculator.py"], "expectations": [
                                     {"id": "entry", "path": "calculator.py", "operator": "contains", "expected": "def add"}],
                                 "test_commands": [{"id": "behavior", "argv": ["python3", "-m", "unittest", "discover", "-s", "acceptance_tests", "-v"]}]},
                  "requirements": [{"id": "addition", "statement": "Add two integers, including negative values",
                                    "check_ids": ["domain.entry", "domain.behavior"], "minimum_evidence": "command"}],
                  "required_check_ids": ["domain.entry", "domain.behavior"],
                  "bundle": {"version": "sum-tests-1", "files": ["acceptance_tests/test_sum.py"]},
                  "approval": {"declared_by": "test operator", "reference": "test specification 1"}}
    (policy / "policy.json").write_text(json.dumps(definition))
    (root / "registry.json").write_text(json.dumps({"defaults": {"develop": "sum"}}))
    return Harness(tmp_path / "state", policy_root=root), workspace, root, definition


def start(configured, **kwargs):
    api, workspace, _, _ = configured
    return api.start(domain_id="develop", goal="Add integers", workspace=str(workspace), parameters={},
                     request_id=kwargs.pop("request_id", "start"), provenance={"declared_author": "model"}, **kwargs)


def fake_sandbox(check, payload, *args):
    # A unit-test receipt, not real Sandbox/e2e evidence. Live test below uses the
    # actual adapter and proves the positive and negative process outcomes.
    assert "class SumTest" in (payload / "acceptance_tests/test_sum.py").read_text()
    correct = "return a + b" in (payload / "calculator.py").read_text()
    return {"status": "completed", "provenance": {"fixture": True}, "capture": {
        "argv": check["argv"], "cwd": check["cwd"], "startedAt": "2026-10-01T00:00:00Z",
        "finishedAt": "2026-10-01T00:00:01Z", "execution": "completed", "exitCode": 0 if correct else 1,
        "stdout": "", "stderr": ""}}


def verify(api, run_id, key):
    api.submit(run_id, "submit-" + key)
    job_id = api.verify(run_id, "verify-" + key)["job_id"]
    execute_job(api.store.root, job_id)
    return api.status(run_id, job_id)


def test_configured_policy_compiles_mandatory_checks_and_separates_authority(configured):
    result = start(configured)
    api, _, _, _ = configured
    summary = api.resume(result["run_id"])
    assert result["policy"]["mode"] == "acceptance"
    assert result["policy"]["origin"] == "operator_configuration"
    assert result["acceptance"]["authority"]["approval_status"] == "configured_not_authenticated"
    checks = api.records(result["run_id"], "checks")["items"]
    assert all(check["role"] == "mandatory" and check["authored_by"]["declared_kind"] == "unknown"
               and check["authored_by"]["trust"] == "configuration_claim" for check in checks)
    assert result["policy"]["provenance"]["declared_author"] == "unknown"
    assert result["policy"]["caller_proposal_provenance"]["declared_author"] == "model"
    assert summary["resolution"]["policy_approval"]["status"] == "configured_not_authenticated"
    assert summary["resolution"]["measurement_scope"]["completed_commands"] == 0
    assert summary["resolution"]["acceptance"]["goal_coverage"] != "proven"
    assert not summary["ready"]


@pytest.mark.parametrize("kwargs,code", [
    ({"mode": "exploratory"}, "POLICY_MODE_CONFLICT"),
    ({"mode": "strict"}, "POLICY_MODE_CONFLICT"),
    ({"policy_id": "weaker"}, "POLICY_SELECTION_PINNED"),
    ({"mode": "acceptance", "required_checks": ["domain.entry"]}, "POLICY_MODE_CONFLICT"),
])
def test_caller_cannot_downgrade_operator_policy(configured, kwargs, code):
    with pytest.raises(HarnessError) as error:
        start(configured, **kwargs)
    assert error.value.code == code


def test_pinned_tests_ignore_changed_workspace_and_registry_copies(configured, monkeypatch):
    api, workspace, root, _ = configured
    run_id = start(configured)["run_id"]
    (workspace / "acceptance_tests").mkdir()
    (workspace / "acceptance_tests/test_sum.py").write_text("# all tests deleted\n")
    (root / "sum/bundle/acceptance_tests/test_sum.py").write_text("# registry changed after start\n")
    monkeypatch.setattr("harness_external.worker.execute_sandbox", fake_sandbox)
    result = verify(Harness(api.store.root), run_id, "broken")
    assert result["job"]["result"]["status"] == "failed"
    with pytest.raises(HarnessError, match="gates"):
        api.finish(run_id, "finish-early", outcome="completed")
    (workspace / "calculator.py").write_text("def add(a, b): return a + b\n")
    result = verify(api, run_id, "fixed")
    assert result["job"]["result"]["status"] == "passed"
    result = api.finish(run_id, "finish", outcome="completed")
    assert result["resolution"]["measurement_scope"]["completed_commands"] == 1
    assert result["resolution"]["measurement_scope"]["test_case_count"] is None
    assert (workspace / "acceptance_tests/test_sum.py").read_text() == "# all tests deleted\n"


def test_exploratory_failure_does_not_change_acceptance_or_disappear(configured, monkeypatch):
    api, workspace, _, _ = configured
    run_id = start(configured)["run_id"]
    (workspace / "calculator.py").write_text("def add(a, b): return a + b\n")
    new = api.register_check(run_id, {"check_id": "guess", "interpretation_revision": 1,
                             "parameters": {"kind": "file", "path": "calculator.py", "operator": "contains", "expected": "wrong hypothesis"}}, "guess")
    assert not new["gated"] and new["check"]["role"] == "exploratory"
    monkeypatch.setattr("harness_external.worker.execute_sandbox", fake_sandbox)
    verify(api, run_id, "one")
    closed = api.finish(run_id, "finish", outcome="completed")
    assert closed["record"]["gates"]["status"] == "passed"
    assert closed["record"]["measurement"]["status"] == "failed"
    assert any(m["role"] == "exploratory" and m["comparison_status"] == "failed" for m in api.records(run_id, "measurements")["items"])


def test_revise_cannot_replace_acceptance_and_can_extend_inputs(configured):
    api, workspace, _, _ = configured
    run_id = start(configured)["run_id"]
    for patch in ({"profile": "structural"}, {"expectations": [{"id": "entry", "path": "calculator.py", "operator": "contains", "expected": "return"}]}):
        with pytest.raises(HarnessError) as error:
            api.revise(run_id, {"expected_revision": 1, "parameters": patch}, "weaken")
        assert error.value.code in {"ACCEPTANCE_SCOPE", "ACCEPTANCE_CHECK_CHANGED"}
    (workspace / "notes.txt").write_text("a hypothesis")
    api.revise(run_id, {"expected_revision": 1, "parameters": {"inputs": ["notes.txt"]}}, "extend")
    run = api.status(run_id)["run"]
    assert "acceptance_tests/test_sum.py" in run["contract"]["inputs"] and "notes.txt" in run["contract"]["inputs"]
    assert run["policy"]["required_check_ids"] == ["domain.entry", "domain.behavior"]


def test_policy_replay_and_linked_revision_preserve_old_bundle(configured):
    api, workspace, root, definition = configured
    old = start(configured)
    updated = copy.deepcopy(definition)
    updated["revision"] = "2"
    (root / "sum/policy.json").write_text(json.dumps(updated))
    assert start(configured) == old
    with pytest.raises(HarnessError, match="terminal predecessor"):
        start(configured, request_id="replacement", predecessor_run_id=old["run_id"], policy_change_reason="new operator policy")
    api.finish(old["run_id"], "close", outcome="partial")
    new = start(configured, request_id="replacement", predecessor_run_id=old["run_id"], policy_change_reason="new operator policy")
    assert new["acceptance"]["revision"] == "2"
    assert api.status(old["run_id"])["run"]["acceptance"]["definition"]["revision"] == "1"
    assert api.status(new["run_id"])["run"]["predecessor"]["run_id"] == old["run_id"]


def test_bundle_corruption_is_not_accepted(configured):
    api, _, _, _ = configured
    run_id = start(configured)["run_id"]
    run = api.status(run_id)["run"]
    path = acceptance.bundle_payload(run["acceptance"], api.store.directory(run_id)) / "acceptance_tests/test_sum.py"
    path.chmod(0o600)
    path.write_text("pass")
    with pytest.raises(HarnessError) as error:
        api.submit(run_id, "submit")
    assert error.value.code == "CANDIDATE_CORRUPT"


def test_file_only_mapping_cannot_claim_command_requirement(configured):
    api, _, root, definition = configured
    definition["requirements"][0]["check_ids"] = ["domain.entry"]
    definition["required_check_ids"] = ["domain.entry"]
    (root / "sum/policy.json").write_text(json.dumps(definition))
    with pytest.raises(HarnessError) as error:
        start(configured)
    assert error.value.code == "REQUIREMENT_EVIDENCE"


def test_weak_caller_check_is_not_presented_as_goal_or_command_verification(configured):
    configured_api, workspace, _, _ = configured
    api = Harness(configured_api.store.root / "caller-defined")
    run_id = api.start(domain_id="develop", goal="Implement correct integer addition", workspace=str(workspace),
                       parameters={"profile": "structural", "inputs": ["calculator.py"], "artifacts": ["calculator.py"],
                                   "expectations": [{"path": "calculator.py", "operator": "contains", "expected": "def add"}]},
                       request_id="weak", provenance={"declared_author": "model"})["run_id"]
    api.submit(run_id, "submit")
    job = api.verify(run_id, "verify")["job_id"]
    execute_job(api.store.root, job)
    closed = api.finish(run_id, "finish", outcome="completed")
    assert closed["record"]["gates"]["status"] == "passed"
    assert closed["resolution"]["acceptance"]["source"] == "caller_defined"
    scope = closed["resolution"]["measurement_scope"]
    assert scope["command_checks"] == scope["completed_commands"] == 0
    assert scope["goal_satisfaction"] == "not_measured" and scope["test_case_count"] is None
    assert closed["ready"] is False and closed["record"]["signature"] is None
    # The intentionally broken mathematical fixture is known test code, not
    # an external user program. A substring pass must not become a goal proof.
    namespace = {}
    exec((workspace / "calculator.py").read_text(), namespace)
    assert namespace["add"](2, 3) != 5


def test_baseline_pairing_does_not_gate_on_original_failure(configured, monkeypatch):
    api, workspace, _, _ = configured
    run_id = start(configured, capture_baseline=True)["run_id"]
    (workspace / "calculator.py").write_text("def add(a, b): return a + b\n")
    monkeypatch.setattr("harness_external.worker.execute_sandbox", fake_sandbox)
    api.submit(run_id, "submit")
    job_id = api.verify(run_id, "verify", compare_baseline=True)["job_id"]
    execute_job(api.store.root, job_id)
    state = api.status(run_id, job_id)
    assert state["job"]["result"]["status"] == "passed"
    assert state["job"]["completed_checks"] == state["job"]["total_checks"] == 4
    pairs = state["job"]["result"]["baseline_comparison"]
    assert pairs["status"] == "compared"
    assert {x["check_id"]: x["status"] for x in pairs["changes"]} == {"domain.entry": "already_passing", "domain.behavior": "improved"}
    assert {x["subject_role"] for x in state["measurements"]} == {"baseline", "candidate"}
    assert len({x["candidate_hash"] for x in state["measurements"]}) == 2
    record = api.finish(run_id, "finish", outcome="completed")["record"]
    assert record["gates"]["status"] == "passed"
    assert record["baseline_comparison"] == pairs


def test_baseline_failure_cannot_satisfy_current_candidate_gate(configured, monkeypatch):
    api, workspace, _, _ = configured
    (workspace / "calculator.py").write_text("def add(a, b): return a + b\n")
    run_id = start(configured, capture_baseline=True)["run_id"]
    (workspace / "calculator.py").write_text("def add(a, b): return 0\n")
    monkeypatch.setattr("harness_external.worker.execute_sandbox", fake_sandbox)
    api.submit(run_id, "submit")
    job_id = api.verify(run_id, "verify", compare_baseline=True)["job_id"]
    execute_job(api.store.root, job_id)
    comparison = api.status(run_id, job_id)["job"]["result"]["baseline_comparison"]
    assert comparison["changes"][1]["status"] == "regressed"
    with pytest.raises(HarnessError) as error:
        api.finish(run_id, "finish", outcome="completed")
    assert error.value.code == "VERIFICATION_REQUIRED"


def test_missing_baseline_and_unavailable_command_do_not_show_improvement(configured, monkeypatch):
    api, workspace, _, _ = configured
    run_id = start(configured)["run_id"]
    api.submit(run_id, "submit")
    with pytest.raises(HarnessError) as error:
        api.verify(run_id, "verify", compare_baseline=True)
    assert error.value.code == "BASELINE_REQUIRED"
    with_baseline = start(configured, request_id="baseline", capture_baseline=True)["run_id"]
    monkeypatch.setattr("harness_external.worker.execute_sandbox", lambda *args: {"status": "not_run", "reason": "SANDBOX_SETUP_FAILED"})
    api.submit(with_baseline, "submit")
    job_id = api.verify(with_baseline, "verify", compare_baseline=True)["job_id"]
    execute_job(api.store.root, job_id)
    result = api.status(with_baseline, job_id)["job"]["result"]
    assert result["status"] == "incomplete"
    assert result["measurement_scope"]["execution_statuses"]["unavailable"] == 1
    assert result["baseline_comparison"]["changes"][1]["status"] == "inconclusive"


def test_concurrent_start_publishes_one_policy_and_baseline(configured, monkeypatch):
    from harness_external import service
    api, _, _, _ = configured
    start(configured, request_id="other")  # initialize the store first
    barrier = threading.Barrier(2)
    original = service.capture
    def together(*args, **kwargs):
        value = original(*args, **kwargs)
        barrier.wait(timeout=10)
        return value
    monkeypatch.setattr(service, "capture", together)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: start(configured, request_id="concurrent", capture_baseline=True), range(2)))
    assert results[0] == results[1]
    assert api.list_runs()["total"] == 2
    directory = api.store.directory(results[0]["run_id"])
    assert len(list(directory.glob("candidate_*"))) == 2  # one bundle, one baseline
    assert not list(api.store.root.glob(".start_*"))


def test_configured_authorship_is_not_inferred_from_generator_or_approver(configured):
    api, _, root, definition = configured
    definition["authorship"] = {"criteria": "model", "tests": "user"}
    (root / "sum/policy.json").write_text(json.dumps(definition))
    result = start(configured)
    checks = api.records(result["run_id"], "checks")["items"]
    assert all(c["authored_by"]["declared_kind"] == "model" for c in checks)
    assert all(c["generated_by"]["kind"] == "domain" for c in checks)
    assert result["acceptance"]["authority"]["authorship"]["tests"] == "user"
    assert result["policy"]["provenance"]["approval"]["configured_approver"] == "test operator"
    assert result["policy"]["provenance"]["approval"]["authenticated_by"] is None


def test_domain_compiler_cannot_drop_configured_mandatory_references(configured):
    from harness_external.domain import DevelopModule
    from harness_external.registry import DomainRegistry
    api, workspace, root, _ = configured
    class InconsistentCompiler(DevelopModule):
        def prepare_acceptance(self, *args, **kwargs):
            result = super().prepare_acceptance(*args, **kwargs)
            result["acceptance_check_ids"] = ["domain.entry"]
            return result
    api = Harness(api.store.root, policy_root=root, domains=DomainRegistry([InconsistentCompiler()]))
    with pytest.raises(HarnessError) as error:
        api.start(domain_id="develop", goal="Add integers", workspace=str(workspace), parameters={}, request_id="start")
    assert error.value.code == "ACCEPTANCE_POLICY_BINDING"
    assert api.list_runs()["total"] == 0


def test_revision_rejects_domain_mandatory_reference_drift(configured):
    from harness_external.domain import DevelopModule
    from harness_external.registry import DomainRegistry
    api, workspace, root, _ = configured
    class DriftingCompiler(DevelopModule):
        def prepare_acceptance(self, goal, parameters, verifier, **kwargs):
            result = super().prepare_acceptance(goal, parameters, verifier, **kwargs)
            if "inputs" in parameters:
                result["acceptance_check_ids"] = []
            return result
    api = Harness(api.store.root, policy_root=root, domains=DomainRegistry([DriftingCompiler()]))
    run_id = api.start(domain_id="develop", goal="Add integers", workspace=str(workspace), parameters={}, request_id="start")["run_id"]
    original = api.resume(run_id)
    with pytest.raises(HarnessError) as error:
        api.revise(run_id, {"expected_revision": 1, "parameters": {"inputs": []}}, "revision")
    assert error.value.code == "ACCEPTANCE_POLICY_BINDING"
    current = api.resume(run_id)
    assert current["policy"] == original["policy"] and current["usage"] == original["usage"]
    assert current["interpretation"]["ref"] == original["interpretation"]["ref"]


def test_scope_change_does_not_claim_pinned_bundle_is_missing(configured, monkeypatch):
    api, workspace, _, _ = configured
    run_id = start(configured, capture_baseline=True)["run_id"]
    (workspace / "calculator.py").write_text("def add(a, b): return a + b\n")
    (workspace / "README.md").write_text("Extra report")
    api.revise(run_id, {"expected_revision": 1, "parameters": {"inputs": ["README.md"]}}, "scope")
    monkeypatch.setattr("harness_external.worker.execute_sandbox", fake_sandbox)
    api.submit(run_id, "submit")
    job_id = api.verify(run_id, "verify", compare_baseline=True)["job_id"]
    execute_job(api.store.root, job_id)
    result = api.status(run_id, job_id)["job"]["result"]
    assert result["status"] == "passed"
    compared = result["baseline_comparison"]["changes"][1]
    assert compared["status"] == "inconclusive"
    assert compared["incomparable_reasons"] == ["input_scope_changed"]
    assert compared["comparison_basis"] == "configured_command_and_pinned_declared_test_bundle"


def test_runtime_identity_cache_rehashes_a_changed_executable(tmp_path):
    from harness_external.worker import runtime_digest, file_fingerprint
    path = tmp_path / "runtime"
    path.write_bytes(b"first runtime")
    before = runtime_digest(str(path), file_fingerprint(path.stat()))
    path.write_bytes(b"a different runtime")
    after = runtime_digest(str(path), file_fingerprint(path.stat()))
    assert after != before


def test_start_capture_does_not_block_another_run_measurement(configured, monkeypatch):
    from harness_external import service
    api, _, _, _ = configured
    other = start(configured, request_id="other")["run_id"]
    api.submit(other, "submit")
    job_id = api.verify(other, "verify")["job_id"]
    original = service.capture
    entered, release = threading.Event(), threading.Event()
    def blocking(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)
    monkeypatch.setattr(service, "capture", blocking)
    monkeypatch.setattr("harness_external.worker.execute_sandbox", fake_sandbox)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(start, configured, request_id="slow", capture_baseline=True)
        try:
            assert entered.wait(3)
            pool.submit(execute_job, api.store.root, job_id).result(timeout=5)
            assert api.status(other, job_id)["job"]["status"] == "completed"
        finally:
            release.set()
        assert pending.result(timeout=5)["run_id"] != other


def test_interrupted_pair_preserves_baseline_without_satisfying_candidate(configured, monkeypatch):
    api, workspace, _, _ = configured
    run_id = start(configured, capture_baseline=True)["run_id"]
    calls = []
    def failed_later(*args):
        calls.append(True)
        if len(calls) == 2:
            raise OSError("controlled adapter failure")
        return fake_sandbox(*args)
    monkeypatch.setattr("harness_external.worker.execute_sandbox", failed_later)
    (workspace / "calculator.py").write_text("def add(a, b): return a + b\n")
    api.submit(run_id, "submit")
    job_id = api.verify(run_id, "verify", compare_baseline=True)["job_id"]
    execute_job(api.store.root, job_id)
    result = api.status(run_id, job_id)
    assert result["job"]["status"] == "error" and len(result["measurements"]) == 3
    assert result["job"]["result"]["baseline_comparison"]["changes"][1]["status"] == "inconclusive"
    with pytest.raises(HarnessError) as error:
        api.finish(run_id, "finish", outcome="completed")
    assert error.value.code == "VERIFICATION_REQUIRED"


@pytest.mark.skipif(os.environ.get("BASE_HARNESS_EXTERNAL_LIVE") != "1", reason="opt-in actual strict Sandbox")
def test_live_acceptance_bundle_negative_and_positive_controls(configured):
    api, workspace, _, _ = configured
    run_id = start(configured, capture_baseline=True)["run_id"]
    negative = verify(api, run_id, "negative")
    assert negative["job"]["result"]["status"] == "failed"
    assert negative["measurements"][1]["execution_status"] == "completed"
    (workspace / "calculator.py").write_text("def add(a, b): return a + b\n")
    api.submit(run_id, "submit-positive")
    job_id = api.verify(run_id, "verify-positive", compare_baseline=True)["job_id"]
    execute_job(api.store.root, job_id)
    positive = api.status(run_id, job_id)
    assert positive["job"]["result"]["status"] == "passed"
    record = api.finish(run_id, "finish", outcome="completed")["record"]
    assert record["acceptance"]["bundle_pinned"] and record["ready"] is False
    assert record["baseline_comparison"]["changes"][1]["status"] == "improved"


@pytest.mark.skipif(os.environ.get("BASE_HARNESS_EXTERNAL_LIVE") != "1", reason="opt-in actual strict Sandbox")
def test_live_command_timeout_is_not_an_ordinary_exit_124(tmp_path, monkeypatch):
    from harness_external.worker import execute_sandbox
    import threading
    payload = tmp_path / "payload"
    payload.mkdir()
    abort = threading.Event()
    command = {"argv": ["python3", "-c", "import time; time.sleep(10)"], "cwd": ".", "timeout_seconds": 1}
    timed = execute_sandbox(command, payload, tmp_path / "state", abort, 15)
    assert timed["status"] == "error"
    assert timed["capture"]["error"]["code"] == "COMMAND_TIMEOUT"
    command["argv"] = ["python3", "-c", "import sys; sys.exit(124)"]
    returned = execute_sandbox(command, payload, tmp_path / "state", abort, 15)
    assert returned["status"] == "completed" and returned["capture"]["exitCode"] == 124
    assert returned["capture"]["stdout"] == ""
