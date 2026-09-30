"""Behavioral conformance of the common track, not a model-quality benchmark."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from harness.common import canonical_hash
from harness_external.domain import DevelopModule, HarnessError
from harness_external.registry import DomainRegistry
from harness_external.service import Harness, LEASE_SECONDS
from harness_external.worker import execute_job


@pytest.fixture
def setup(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "output.txt").write_text("observed output")
    monkeypatch.setattr("harness_external.worker.spawn_worker", lambda *args: None)
    params = {"profile": "structural", "inputs": ["output.txt"], "artifacts": ["output.txt"],
              "expectations": [{"path": "output.txt", "operator": "equals", "expected": "observed output"}]}
    return Harness(tmp_path / "state"), workspace, params


def begin(setup, *, params=None, mode="exploratory", gates=None):
    api, workspace, initial = setup
    return api.start(domain_id="develop", goal="Investigate and explain the output", workspace=str(workspace),
                     parameters=initial if params is None else params, request_id="start", mode=mode, required_checks=gates)["run_id"]


def measure(api, run_id, suffix="1"):
    api.submit(run_id, "submit-" + suffix)
    job = api.verify(run_id, "verify-" + suffix)["job_id"]
    execute_job(api.store.root, job)
    return api.status(run_id, job)


def assess(api, run_id, status="partial", cited=None):
    state = api.status(run_id)
    return api.assess(run_id, {"interpretation_revision": state["run"]["interpretations"][-1]["ref"]["revision"],
                              "status": status, "summary": "Caller assessment", "uncertainties": ["Exploratory expectation may be wrong"],
                              "cited_observation_ids": cited or []}, "assessment")


def test_advisory_failure_preserves_facts_and_does_not_become_gate(setup):
    api, _, _ = setup
    run = begin(setup, gates=["file-0"])
    check = api.register_check(run, {"check_id": "hypothesis", "interpretation_revision": 1,
                               "parameters": {"kind": "file", "path": "output.txt", "operator": "equals", "expected": "disproved hypothesis"}}, "check")
    assert check["gated"] is False
    job = measure(api, run)
    assert job["job"]["result"]["status"] == "failed"
    ids = [item["observation_id"] for item in job["measurements"]]
    assess(api, run, cited=ids)
    finished = api.finish(run, "finish", outcome="completed")["record"]
    assert finished["lifecycle"] == "closed" and finished["termination_reason"] == "requested"
    assert finished["measurement"]["status"] == "failed"
    assert finished["gates"]["status"] == "passed"
    assert finished["assessment"]["status"] == "partial"
    assert finished["assessment"]["trust"] == "untrusted"
    assert len(api.status(run)["measurements"]) == 2
    assert finished["ready"] is False


def test_required_gate_cannot_be_overridden_by_model_assessment(setup):
    api, workspace, _ = setup
    run = begin(setup, gates=["file-0"])
    (workspace / "output.txt").write_text("actual mismatch")
    job = measure(api, run)
    assess(api, run, "satisfied", [job["measurements"][0]["observation_id"]])
    assert api.status(run)["closeout"]["gates"]["status"] == "unsatisfied"
    with pytest.raises(HarnessError):
        api.finish(run, "finish", outcome="completed")
    closed = api.finish(run, "partial", outcome="partial")["record"]
    assert closed["assessment"]["status"] == "satisfied"
    assert closed["gates"]["results"][0]["status"] == "failed"
    assert not closed["ready"]


def test_unknown_parameters_have_durable_waiting_input_and_resume(setup):
    api, _, params = setup
    run = begin(setup, params={})
    waiting = Harness(api.store.root).status(run)["run"]
    assert waiting["phase"] == "waiting_input" and waiting["domain_questions"]
    api.observe(run, {"note": "Explore test options"}, "note")
    with pytest.raises(HarnessError) as error:
        api.submit(run, "early")
    assert error.value.code == "NEEDS_INPUT"
    revised = api.revise(run, {"expected_revision": 1, "parameters": params}, "revise")
    assert revised["phase"] == "awaiting_submission"
    measure(api, run)
    assert api.status(run)["run"]["verification"]["status"] == "passed"
    assert api.status(run)["run"]["observations"][0]["note"] == "Explore test options"


def test_two_revisions_preserve_intent_policy_budget_and_history(setup):
    api, _, _ = setup
    run = begin(setup)
    initial = api.status(run)["run"]
    api.revise(run, {"expected_revision": 1, "goal_summary": "First hypothesis", "open_questions": ["Why?"]}, "r1")
    api.revise(run, {"expected_revision": 2, "goal_summary": "Revised explanation", "assumptions": [{"id": "a1", "statement": "Candidate assumption"}]}, "r2")
    state = Harness(api.store.root).status(run)["run"]
    assert state["intent"] == initial["intent"]
    assert state["policy"] == initial["policy"]
    assert state["created_at"] == initial["created_at"] and state["deadline_at"] == initial["deadline_at"]
    assert state["actions"] == 2 and state["verification_attempts"] == 0
    assert [item["ref"]["revision"] for item in state["interpretations"]] == [1, 2, 3]
    with pytest.raises(HarnessError) as error:
        api.revise(run, {"expected_revision": 1, "goal_summary": "stale"}, "stale")
    assert error.value.code == "STALE_INTERPRETATION"


def test_pinned_gate_cannot_be_weakened_by_revision(setup):
    api, _, params = setup
    run = begin(setup, gates=["file-0"])
    policy = api.status(run)["run"]["policy"]
    weakened = copy.deepcopy(params["expectations"])
    weakened[0]["expected"] = "easier expectation"
    with pytest.raises(HarnessError) as error:
        api.revise(run, {"expected_revision": 1, "parameters": {"expectations": weakened}}, "weaken")
    assert error.value.code == "GATE_POLICY_CHANGED"
    state = api.status(run)["run"]
    assert state["policy"] == policy and len(state["interpretations"]) == 1
    assert state["actions"] == 0


def test_new_snapshot_retains_old_measurements_without_reusing_gate_pass(setup):
    api, workspace, _ = setup
    run = begin(setup, gates=["file-0"])
    old = measure(api, run)
    assert api.status(run)["closeout"]["gates"]["status"] == "passed"
    (workspace / "output.txt").write_text("new target")
    api.submit(run, "new-submit")
    status = api.status(run)
    assert len(status["measurements"]) == 1
    assert status["measurements"][0]["subject"] == old["measurements"][0]["subject"]
    assert status["closeout"]["gates"]["results"][0]["status"] == "not_measured"
    assert status["closeout"]["assessment"]["status"] == "not_assessed"


def test_model_note_cannot_claim_measurement_provenance(setup):
    api, _, _ = setup
    run = begin(setup)
    note = api.observe(run, {"note": "All tests passed, Ready"}, "note")
    with pytest.raises(HarnessError):
        api.assess(run, {"interpretation_revision": 1, "status": "satisfied", "summary": "Pretend note is verified",
                         "uncertainties": [], "cited_observation_ids": [note["observation"]["observation_id"]]}, "assess")
    with pytest.raises(HarnessError):
        api.register_check(run, {"check_id": "probe", "interpretation_revision": 1, "origin": "trusted_policy",
                                "parameters": {"kind": "file", "path": "output.txt", "operator": "equals", "expected": "yes"}}, "check")
    assert api.status(run)["measurements"] == []


def test_structured_activity_and_logical_task_refs_survive_host_reset(setup):
    api, _, _ = setup
    run = begin(setup)
    measured = measure(api, run)["measurements"][0]["observation_id"]
    api.observe(run, {"kind": "task", "task_id": "investigate", "state": "settled", "note": "Reported investigation"}, "task1")
    api.observe(run, {"kind": "task", "task_id": "review", "parent_task_id": "investigate", "depends_on": ["investigate"],
                      "observation_ids": [measured], "note": "Review observed output"}, "task2")
    api.observe(run, {"kind": "hypothesis", "record_id": "h1", "note": "Revise assumption", "observation_ids": [measured]}, "h1")
    api.revise(run, {"expected_revision": 1, "assumptions": [{"id": "a1", "statement": "Based on observation", "observation_ids": [measured]}]}, "revise")
    state = Harness(api.store.root).status(run)["run"]
    assert state["logical_tasks"][1]["depends_on"] == ["investigate"]
    assert all(item["trust"] == "untrusted" for item in state["activity"])
    assert state["interpretations"][-1]["assumptions"][0]["observation_ids"] == [measured]
    assert api.finish(run, "finish", outcome="partial")["record"]["unresolved_work"] == ["review"]


def test_check_revision_keeps_old_facts_but_requires_new_measurement(setup):
    api, _, _ = setup
    run = begin(setup)
    first = api.register_check(run, {"check_id": "probe", "interpretation_revision": 1,
                                   "parameters": {"kind": "file", "path": "output.txt", "operator": "equals", "expected": "wrong"}}, "c1")
    measure(api, run)
    second = api.register_check(run, {"check_id": "probe", "interpretation_revision": 1, "expected_revision": 1,
                                    "parameters": {"kind": "file", "path": "output.txt", "operator": "equals", "expected": "observed output"}}, "c2")
    assert first["check"]["ref"]["revision"] == 1 and second["check"]["ref"]["revision"] == 2
    assert api.status(run)["run"]["verification"]["status"] == "not_run"
    assert any(item["check_ref"] == first["check"]["ref"] for item in api.status(run)["measurements"])
    job = api.verify(run, "v2")["job_id"]
    execute_job(api.store.root, job)
    assert api.status(run, job)["job"]["result"]["status"] == "passed"


def test_partial_measurements_survive_later_verifier_exception(setup, monkeypatch):
    api, _, params = setup
    params = {**params, "profile": "execution", "test_commands": [{"argv": ["python3", "-c", "pass"]}]}
    run = begin(setup, params=params)
    monkeypatch.setattr("harness_external.worker.execute_sandbox", lambda *args: (_ for _ in ()).throw(HarnessError("PROBE_STOP", "Simulated later adapter failure")))
    job = measure(api, run)
    assert job["job"]["status"] == "error"
    assert job["job"]["result"]["status"] == "incomplete"
    assert len(job["measurements"]) == 1
    assert job["measurements"][0]["comparison_status"] == "passed"


@pytest.mark.skipif(os.name == "nt", reason="SIGKILL interruption case is Unix-specific")
def test_real_worker_kill_retains_completed_measurement(setup):
    api, _, params = setup
    params = {**params, "profile": "execution", "test_commands": [{"argv": ["python3", "-c", "pass"]}]}
    run = begin(setup, params=params, mode="strict")
    api.submit(run, "submit")
    job = api.verify(run, "verify")["job_id"]
    source = str(Path(__file__).resolve().parents[1] / "src")
    marker = api.store.root / "blocking-stage"
    # Real process interruption; a controlled blocking adapter replaces only the
    # second check so this case needs neither a model nor namespace privileges.
    code = (f"import sys,time; sys.path.insert(0,{source!r}); from pathlib import Path; import harness_external.worker as w; "
            f"w.execute_sandbox=lambda *a: (Path({str(marker)!r}).write_text('started'),time.sleep(60))[1]; "
            f"w.execute_job({str(api.store.root)!r},{job!r})")
    child = subprocess.Popen([sys.executable, "-I", "-c", code], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not marker.exists():
            time.sleep(0.05)
        assert marker.exists() and len(api.status(run, job)["measurements"]) == 1
        child.kill()
        child.wait(3)
        with api.store.transaction() as connection:
            saved = api.store.job(connection, job)
            saved["heartbeat_at"] = time.time() - LEASE_SECONDS - 1
            api.store.save_job(connection, saved)
        restarted = Harness(api.store.root).status(run, job)
        assert restarted["job"]["status"] == "interrupted"
        assert len(restarted["measurements"]) == 1
        assert restarted["job"]["result"] is None
        with pytest.raises(HarnessError):
            api.finish(run, "finish", outcome="completed")
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(3)


def test_other_domain_module_can_be_injected_without_core_changes(setup):
    api, workspace, params = setup
    class ReviewModule:
        domain_id = "review"
        revision = "review-port-test-1"
        def prepare(self, goal, parameters, verifier, *, exploratory, intent=None):
            result = DevelopModule().prepare(goal, parameters, verifier, exploratory=exploratory)
            result["contract"]["domain_id"] = self.domain_id
            result["contract"]["contract_hash"] = canonical_hash({k: v for k, v in result["contract"].items() if k != "contract_hash"})
            return result
        def normalize_check(self, parameters, contract):
            return DevelopModule().normalize_check(parameters, contract)
    injected = Harness(api.store.root, domains=DomainRegistry([ReviewModule()]))
    run = injected.start(domain_id="review", goal="Review a result", workspace=str(workspace), parameters=params, request_id="custom")["run_id"]
    assert injected.status(run)["run"]["domain_module"] == {"id": "review", "revision": "review-port-test-1"}
    assert measure(injected, run)["job"]["result"]["status"] == "passed"


def test_strict_policy_still_gates_registered_probes(setup):
    api, _, _ = setup
    run = begin(setup, mode="strict")
    response = api.register_check(run, {"check_id": "probe", "interpretation_revision": 1,
                                      "parameters": {"kind": "file", "path": "output.txt", "operator": "equals", "expected": "wrong"}}, "probe")
    assert response["gated"]
    measure(api, run)
    with pytest.raises(HarnessError):
        api.finish(run, "finish", outcome="completed")


def test_budget_handoff_can_preserve_final_uncertainty(setup):
    api, workspace, params = setup
    run = api.start(domain_id="develop", goal="Inspect output", workspace=str(workspace), parameters=params,
                    request_id="limited", budget={"max_actions": 1}, constraints=["Do not claim tests ran without observations"])["run_id"]
    api.observe(run, {"note": "One recorded activity"}, "note")
    with pytest.raises(HarnessError):
        api.submit(run, "over-budget")
    result = api.finish(run, "handoff", outcome="partial", assessment={"interpretation_revision": 1, "status": "unsolved",
                        "summary": "Hand off without execution evidence", "uncertainties": ["Test not executed"], "cited_observation_ids": []})["record"]
    assert result["termination_reason"] == "ACTION_BUDGET_EXHAUSTED"
    assert result["assessment"]["status"] == "unsolved" and result["measurement"]["status"] == "not_run"
    assert result["gates"]["status"] == "unsatisfied" and not result["ready"]
    assert api.status(run)["run"]["intent"]["constraints"] == ["Do not claim tests ran without observations"]


def test_v1_request_keys_and_records_remain_readable(setup):
    from harness.common import canonical_bytes
    api, workspace, params = setup
    result = api.start(domain_id="develop", goal="Original goal", workspace=str(workspace), parameters=params, request_id="legacy")
    run_id = result["run_id"]
    legacy_fingerprint = canonical_hash({"operation": "start", "domain_id": "develop", "goal": "Original goal",
                                         "workspace": str(workspace), "parameters": params, "budget": None})
    with api.store.transaction() as connection:
        fingerprint = connection.execute("SELECT fingerprint FROM requests WHERE scope='start' AND request_id='legacy'").fetchone()[0]
        assert fingerprint == legacy_fingerprint
        saved = api.store.run(connection, run_id)
        for key in ("semantic_schema_version", "semantic_projection_origin", "intent", "policy", "domain_module", "interpretations",
                    "check_records", "assessments", "activity", "logical_tasks", "gate_bindings", "domain_questions"):
            saved.pop(key)
        saved.pop("domain_preparation")
        connection.execute("UPDATE runs SET data=? WHERE run_id=?", (canonical_bytes(saved).decode(), run_id))
    assert api.start(domain_id="develop", goal="Original goal", workspace=str(workspace), parameters=params, request_id="legacy") == result
    restored = Harness(api.store.root).status(run_id)["run"]
    assert restored["semantic_projection_origin"] == "legacy_strict_projection"
    assert restored["policy"]["mode"] == "strict" and restored["goal"] == "Original goal"


def test_foreign_measurement_cannot_be_cited_by_another_run(setup):
    api, workspace, params = setup
    first = begin(setup)
    observed = measure(api, first)["measurements"][0]["observation_id"]
    other = api.start(domain_id="develop", goal="Other goal", workspace=str(workspace), parameters=params, request_id="other")["run_id"]
    with pytest.raises(HarnessError) as error:
        api.assess(other, {"interpretation_revision": 1, "status": "satisfied", "summary": "Borrow evidence",
                          "uncertainties": [], "cited_observation_ids": [observed]}, "assessment")
    assert error.value.code == "OBSERVATION_REFERENCE"


def test_cli_tool_roundtrip_restores_exploration_and_closeout(setup, tmp_path):
    _, workspace, params = setup
    state = tmp_path / "cli-state"
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    def cli(*args):
        child = subprocess.run([sys.executable, "-m", "harness_external", "--state-dir", str(state), *args],
                               capture_output=True, text=True, env=environment, timeout=20)
        assert child.returncode == 0, child.stderr + child.stdout
        return json.loads(child.stdout)
    run = cli("start", "--domain", "develop", "--mode", "exploratory", "--required-check", "file-0",
              "--workspace", str(workspace), "--goal", "Investigate output", "--request-id", "start")["run_id"]
    assert cli("status", "--run-id", run)["run"]["phase"] == "waiting_input"
    proposal = tmp_path / "revision.json"
    proposal.write_text(json.dumps({"expected_revision": 1, "parameters": params}))
    cli("revise", "--run-id", run, "--data", str(proposal), "--request-id", "revise")
    proposal.write_text(json.dumps({"check_id": "probe", "interpretation_revision": 2,
                                  "parameters": {"kind": "file", "path": "output.txt", "operator": "equals", "expected": "hypothesis"}}))
    cli("check", "--run-id", run, "--data", str(proposal), "--request-id", "check")
    cli("submit", "--run-id", run, "--request-id", "submit")
    job = cli("verify", "--run-id", run, "--request-id", "verify")["job_id"]
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        status = cli("status", "--run-id", run, "--job-id", job)
        if status["job"]["status"] not in {"queued", "running"}:
            break
        time.sleep(0.05)
    assert status["job"]["status"] == "completed" and len(status["measurements"]) == 2
    proposal.write_text(json.dumps({"interpretation_revision": 2, "status": "partial", "summary": "Probe disproved",
                                  "uncertainties": ["Further investigation possible"], "cited_observation_ids": [m["observation_id"] for m in status["measurements"]]}))
    cli("assess", "--run-id", run, "--data", str(proposal), "--request-id", "assess")
    record = cli("finish", "--run-id", run, "--outcome", "completed", "--request-id", "finish")["record"]
    assert record["measurement"]["status"] == "failed" and record["gates"]["status"] == "passed"
    assert record["assessment"]["status"] == "partial" and record["lifecycle"] == "closed"
    assert record["ready"] is False


def test_hypothesis_decision_revision_and_reported_task_state_are_linked(setup):
    api, _, _ = setup
    run = begin(setup)
    hypothesis = api.observe(run, {"kind": "hypothesis", "record_id": "h1", "note": "Initial hypothesis"}, "h1")["activity"]
    observed = measure(api, run)["measurements"][0]["observation_id"]
    decision = api.observe(run, {"kind": "decision", "note": "Reconsider hypothesis", "related_activity_id": hypothesis["ref"]["id"],
                                "relation": "refutes", "observation_ids": [observed]}, "decision")["activity"]
    api.revise(run, {"expected_revision": 1, "goal_summary": "Revised interpretation", "activity_ids": [decision["ref"]["id"]],
                     "observation_ids": [observed]}, "revision")
    api.observe(run, {"kind": "task", "task_id": "review", "note": "Review result"}, "task")
    api.observe(run, {"kind": "task", "task_id": "review", "task_revision": 1, "state": "settled", "note": "Caller reports review done"}, "task-done")
    state = Harness(api.store.root).status(run)["run"]
    assert state["interpretations"][-1]["activity_ids"] == [decision["ref"]["id"]]
    assert state["activity"][1]["related_activity_id"] == hypothesis["ref"]["id"]
    assert state["activity"][1]["relation"] == "refutes"
    assert state["logical_tasks"][0]["objective"] == "Review result"
    assert state["logical_tasks"][0]["revision"] == 2 and state["logical_tasks"][0]["state"] == "settled"
    assert all(a["trust"] == "untrusted" for a in state["activity"])
    with pytest.raises(HarnessError):
        api.observe(run, {"kind": "task", "task_id": "review", "task_revision": 1, "state": "blocked", "note": "stale update"}, "stale-task")


def test_exploratory_measurement_can_precede_final_criteria(setup):
    api, _, params = setup
    run = begin(setup, params={"profile": "structural", "inputs": ["output.txt"]})
    assert api.status(run)["run"]["phase"] == "waiting_input"
    api.register_check(run, {"check_id": "probe", "interpretation_revision": 1,
                            "parameters": {"kind": "file", "path": "output.txt", "operator": "equals", "expected": "hypothesis"}}, "probe")
    observed = measure(api, run)
    assert observed["measurements"][0]["comparison_status"] == "failed"
    with pytest.raises(HarnessError):
        api.finish(run, "premature", outcome="completed")
    ref = observed["measurements"][0]["observation_id"]
    api.revise(run, {"expected_revision": 1, "parameters": params, "observation_ids": [ref],
                     "assumptions": [{"id": "h1", "statement": "Hypothesis did not match observed file", "observation_ids": [ref]}]}, "refine")
    state = api.status(run)
    assert state["run"]["run_id"] == run and state["run"]["interpretations"][-1]["ref"]["revision"] == 2
    assert state["measurements"][0]["observation_id"] == ref
    assert state["run"]["domain_preparation"]["status"] == "proceed"
