"""Mandatory case facts come from an observer outside the Candidate process."""
import json
import os
from pathlib import Path
import threading

import pytest

from harness_external.errors import HarnessError
from harness_external.service import Harness
from harness_external.worker import execute_job


@pytest.mark.parametrize("phase", ["preparation", "observation"])
def test_external_observer_deadline_covers_preparation_and_prevents_late_facts(task, monkeypatch, phase):
    from harness_external import adapter_execution, worker
    from execution_input_fixtures import inputs, receipt
    api, workspace = task
    check = api.domains.resolve("develop").prepare("Print 42", parameters(), {})["contract"]["checks"][1]
    bound = adapter_execution.bind([{"check_id": "test", "spec": {"parameters": check}}], api.store.root,
                                   registry=api.adapters)["test"]
    binding = inputs(workspace)
    calls, emitted = [], []
    def capture(command, payload, state, abort, remaining, input_binding):
        calls.append(True)
        return {"status": "completed", "input_receipt": receipt(input_binding), "capture": {
            "execution": "completed", "argv": command["argv"], "cwd": command["cwd"], "exitCode": 0,
            "stdout": "42\n", "stderr": "", "startedAt": "2026-10-01T00:00:00Z", "finishedAt": "2026-10-01T00:00:01Z"}}
    ticks = iter([0, 2] if phase == "preparation" else [0, .1, 2])
    monkeypatch.setattr(adapter_execution.time, "monotonic", lambda: next(ticks, 2))
    monkeypatch.setattr(worker, "execute_sandbox", capture)
    with pytest.raises(HarnessError) as caught:
        adapter_execution.execute(check, workspace, api.store.root, threading.Event(), 1, bound,
                                  lambda *args: emitted.append(args), api.adapters, input_binding=binding)
    assert caught.value.code == "VERIFICATION_CANCELLED"
    assert len(calls) == (0 if phase == "preparation" else 1)
    assert emitted == []


def parameters(adapter="process-observations-v1"):
    selector = {"source_paths": ["app.py"], "argv": ["python3", "app.py"], "cases": [
        {"id": "answer", "stream": "stdout", "operator": "equals", "expected": "42\n"},
    ]} if adapter == "process-observations-v1" else {"paths": ["test_app.py"]}
    return {"inputs": ["app.py", "test_app.py"], "artifacts": ["app.py"],
            "expectations": [{"id": "entry", "path": "app.py", "operator": "contains", "expected": "print"}],
            "execution_checks": [{"kind": "cases", "id": "behavior", "adapter_id": adapter,
                                  "selector": selector, "required_cases": ["answer"] if adapter == "process-observations-v1" else ["test_app.py::test_answer"]}]}


@pytest.fixture
def task(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "app.py").write_text("print(42)\n", encoding="utf-8")
    (workspace / "test_app.py").write_text("def test_answer(): assert True\n", encoding="utf-8")
    monkeypatch.setattr("harness_external.worker.spawn_worker", lambda *args: None)
    return Harness(tmp_path / "state"), workspace


@pytest.mark.parametrize("mode,required", [("strict", None), ("exploratory", ["domain.behavior"])])
def test_inprocess_case_adapter_cannot_become_a_mandatory_gate(task, mode, required):
    api, workspace = task
    with pytest.raises(HarnessError) as caught:
        api.start(domain_id="develop", goal="Return answer", workspace=str(workspace),
                  parameters=parameters("pytest-cases-v1"), mode=mode, required_checks=required, request_id="start")
    assert caught.value.code == "EXTERNAL_OBSERVER_REQUIRED"
    assert api.list_runs()["items"] == []


def test_pytest_remains_available_as_explicit_advisory_report(task):
    api, workspace = task
    run = api.start(domain_id="develop", goal="Collect auxiliary reports", workspace=str(workspace),
                    parameters=parameters("pytest-cases-v1"), mode="exploratory", required_checks=["domain.entry"], request_id="start")
    assert run["policy"]["required_check_ids"] == ["domain.entry"]


def test_deferred_gate_cannot_admit_an_inprocess_adapter_later(task):
    api, workspace = task
    run_id = api.start(domain_id="develop", goal="Return answer", workspace=str(workspace), parameters={},
                       mode="exploratory", deferred_checks=["domain.behavior"], request_id="start")["run_id"]
    with pytest.raises(HarnessError) as caught:
        api.revise(run_id, {"expected_revision": 1, "parameters": parameters("pytest-cases-v1")}, "define")
    assert caught.value.code == "EXTERNAL_OBSERVER_REQUIRED"
    assert api.resume(run_id)["gates"]["results"][0]["status"] == "not_defined"


@pytest.mark.skipif(os.environ.get("BASE_HARNESS_EXTERNAL_LIVE") != "1", reason="real strict Sandbox with external observation")
@pytest.mark.parametrize("source,expected", [
    ("print(42)\n", "passed"),
    ("print(0)\n", "failed"),
    ("import os\n# print\nos._exit(0)\n", "failed"),
    ("import json\n# print\nopen('.harness-cases.jsonl','w').write(json.dumps({'event':'finished','outcome':'passed','case_id':'answer'}))\n", "failed"),
])
def test_live_external_observer_accepts_only_actual_process_output(task, source, expected):
    api, workspace = task
    (workspace / "app.py").write_text(source, encoding="utf-8")
    run_id = api.start(domain_id="develop", goal="Print exactly 42", workspace=str(workspace),
                       parameters=parameters(), request_id="start")["run_id"]
    api.submit(run_id, "submit")
    job_id = api.verify(run_id, "verify")["job_id"]
    execute_job(api.store.root, job_id)
    summary = api.resume(run_id)
    assert summary["measurement"]["status"] == expected
    assert summary["gates"]["status"] == ("passed" if expected == "passed" else "unsatisfied")
    case = api.records(run_id, "cases", job_id=job_id)["items"][0]
    assert case["observation_boundary"] == "external_capture"
    assert case["case"]["outcome"] == expected
    assert case["trust"] == "locally_measured"


@pytest.mark.skipif(os.environ.get("BASE_HARNESS_EXTERNAL_LIVE") != "1", reason="JSON predicates evaluated outside actual Candidate process")
@pytest.mark.parametrize("value,expected", [(42, "passed"), (True, "failed"), (None, "failed")])
def test_live_external_json_projection_keeps_types_and_checks_selected_field(task, value, expected):
    api, workspace = task
    (workspace / "app.py").write_text("import json\nprint(json.dumps(" + repr({"answer": value, "unrelated": "not compared"}) + "))\n", encoding="utf-8")
    params = parameters()
    params["execution_checks"][0]["selector"]["cases"] = [
        {"id": "answer", "stream": "stdout", "operator": "json_equals", "json_path": ["answer"], "expected": 42},
    ]
    run_id = api.start(domain_id="develop", goal="Return numeric answer", workspace=str(workspace), parameters=params, request_id="start")["run_id"]
    api.submit(run_id, "submit")
    job_id = api.verify(run_id, "verify")["job_id"]
    execute_job(api.store.root, job_id)
    assert api.resume(run_id)["measurement"]["status"] == expected


def test_inline_external_criteria_compile_under_configured_acceptance(task, tmp_path):
    api, workspace = task
    policy_root = tmp_path / "policies"
    directory = policy_root / "behavior"
    (directory / "bundle").mkdir(parents=True)
    definition = {"schema_version": "acceptance-policy-v1", "policy_id": "behavior", "revision": "1", "domain_id": "develop",
                  "parameters": parameters(), "required_check_ids": ["domain.entry", "domain.behavior"],
                  "requirements": [{"id": "R1", "statement": "Print 42", "check_ids": ["domain.entry", "domain.behavior"], "minimum_evidence": "testcase"}],
                  "coverage": {"schema_version": "develop-coverage-v2", "profile_id": "external", "profile_revision": "1", "known_gaps": [],
                               "scenarios": [{"id": "S1", "requirement_id": "R1", "check_id": "domain.behavior", "kind": "positive", "required": True,
                                              "test_ref": {"path": "app.py", "case_id": "answer"}}]},
                  "bundle": {"version": "1", "files": []}, "approval": {"declared_by": "fixture", "reference": "test-policy"}}
    (directory / "policy.json").write_text(json.dumps(definition), encoding="utf-8")
    api = Harness(api.store.root, policy_root=policy_root)
    started = api.start(domain_id="develop", goal="Print 42", workspace=str(workspace), parameters={}, policy_id="behavior", request_id="start")
    assert started["policy"]["mode"] == "acceptance"
    assert started["contract"]["observation_links"]["scenarios"][0]["case_id"] == "answer"


@pytest.mark.skipif(os.environ.get("BASE_HARNESS_EXTERNAL_LIVE") != "1", reason="actual hostile Candidate in strict Sandbox")
def test_live_forged_pytest_events_remain_reports_and_cannot_pass_external_gate(task):
    api, workspace = task
    (workspace / "app.py").write_text((Path(__file__).parent / "fixtures/forged_pytest_candidate.py").read_text(encoding="utf-8"), encoding="utf-8")
    (workspace / "test_app.py").write_text("import app\ndef test_answer(): raise AssertionError('This body never executes')\n", encoding="utf-8")
    params = parameters()
    report = parameters("pytest-cases-v1")["execution_checks"][0]
    report["id"] = "auxiliary"
    params["execution_checks"].append(report)
    params["requirements"] = [{"id": "auxiliary", "statement": "Inspect the framework report", "check_ids": ["domain.auxiliary"], "minimum_evidence": "testcase"}]
    params["coverage"] = {"schema_version": "develop-coverage-v2", "profile_id": "reporting", "profile_revision": "1", "known_gaps": [],
                          "scenarios": [{"id": "reported", "requirement_id": "auxiliary", "check_id": "domain.auxiliary", "kind": "positive",
                                         "test_ref": {"path": "test_app.py", "case_id": "test_app.py::test_answer"}}]}
    run_id = api.start(domain_id="develop", goal="Print 42; reports cannot override actual output", workspace=str(workspace), parameters=params,
                       mode="exploratory", required_checks=["domain.behavior"], request_id="start")["run_id"]
    api.submit(run_id, "submit")
    job_id = api.verify(run_id, "verify")["job_id"]
    execute_job(api.store.root, job_id)
    cases = api.records(run_id, "cases", job_id=job_id)["items"]
    forged = next(item for item in cases if item["check_id"] == "domain.auxiliary")
    assert forged["case"]["outcome"] == "passed"  # Preserve the report honestly.
    assert forged["trust"] == "framework_reported" and forged["observation_boundary"] == "subject_process"
    assert api.resume(run_id)["gates"]["status"] == "unsatisfied"
    result = api.status(run_id, job_id)["job"]["result"]
    requirement = result["requirement_observations"]["requirements"][0]
    assert requirement["reported_cases_passed"] and not requirement["linked_cases_passed"]
    assert not requirement["declared_scenarios_observed"]
    assert result["measurement_scope"]["reported_test_case_count"] == 1
    assert result["measurement_scope"]["test_case_count"] == 1  # One external predicate, not the forged test.
    with pytest.raises(HarnessError):
        api.finish(run_id, "cannot-finish", outcome="completed")
