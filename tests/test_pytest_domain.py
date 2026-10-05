import copy
import json
import os
from pathlib import Path
import threading

import pytest

from harness_external.adapter_execution import bind, execute
from harness_external.develop_cases import normalize
from harness_external.errors import HarnessError
from harness_external.pytest_adapter import Collector
from harness_external.service import Harness
from harness_external.worker import execute_job


def policy_fixture(tmp_path, source, *, required="test_cases.py::test_ok", args=None):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "app.py").write_text("def value(): return 1\n")
    root = tmp_path / "policies"
    directory = root / "behavior"
    bundle = directory / "bundle"
    bundle.mkdir(parents=True)
    (bundle / "test_cases.py").write_text(source)
    definition = {"schema_version": "acceptance-policy-v1", "policy_id": "behavior", "domain_id": "develop", "revision": "1",
                  "parameters": {"profile": "execution", "inputs": ["app.py", "test_cases.py"], "artifacts": ["app.py"],
                      "expectations": [{"id": "entry", "path": "app.py", "operator": "contains", "expected": "def value"}],
                      "pytest_checks": [{"id": "tests", "paths": ["test_cases.py"], "args": args or [], "timeout_seconds": 10}]},
                  "required_check_ids": ["domain.entry", "domain.tests"],
                  "requirements": [{"id": "value", "statement": "Observe declared behavior", "check_ids": ["domain.tests"], "minimum_evidence": "testcase"},
                                   {"id": "artifact", "statement": "Keep app entry", "check_ids": ["domain.entry"]}],
                  "coverage": {"schema_version": "develop-coverage-v2", "profile_id": "cases", "profile_revision": "1", "known_gaps": [],
                      "scenarios": [{"id": "positive", "requirement_id": "value", "check_id": "domain.tests", "kind": "positive", "required": True,
                                     "test_ref": {"framework": "pytest", "path": "test_cases.py", "case_id": required}}]},
                  "bundle": {"version": "1", "files": ["test_cases.py"]},
                  "approval": {"declared_by": "test operator", "reference": "fixed test fixture"}}
    (directory / "policy.json").write_text(json.dumps(definition))
    (root / "registry.json").write_text(json.dumps({"defaults": {"develop": "behavior"}}))
    api = Harness(tmp_path / "state", policy_root=root)
    return api, workspace, definition


def emit(collector, event, **body):
    return collector.feed({"version": 1, "token": "session", "seq": collector.sequence + 1, "event": event, **body})


def bind_check(check, state):
    return bind([{"check_id": "test", "spec": {"parameters": check}}], state)["test"]


def test_collector_distinguishes_call_pass_teardown_error():
    collector = Collector("session")
    emit(collector, "session_start", framework="pytest", framework_version="test")
    emit(collector, "discovered", case_id="test_a.py::test_x[one]", path="test_a.py")
    emit(collector, "collection_finish", selected=["test_a.py::test_x[one]"], errors=0)
    emit(collector, "started", case_id="test_a.py::test_x[one]")
    for phase, outcome in (("setup", "passed"), ("call", "passed"), ("teardown", "failed")):
        emit(collector, "phase", case_id="test_a.py::test_x[one]", phase=phase, outcome=outcome, duration=.01, wasxfail=None, message=None)
    case = emit(collector, "finished", case_id="test_a.py::test_x[one]")
    assert case["executed"] and case["outcome"] == "error"


def test_stale_event_cannot_become_observation():
    collector = Collector("current")
    with pytest.raises(HarnessError):
        collector.feed({"version": 1, "token": "past", "seq": 1, "event": "session_start"})


def test_invalid_collection_finish_cannot_establish_missing_cases():
    collector = Collector("session")
    emit(collector, "session_start", framework="pytest", framework_version="test")
    with pytest.raises(HarnessError):
        emit(collector, "collection_finish", selected=["never-discovered"], errors=0)
    assert not collector.collection_complete
    assert not collector.collection_finished


def test_discovery_requires_session_start():
    collector = Collector("session")
    with pytest.raises(HarnessError):
        emit(collector, "discovered", case_id="test.py::test_fake", path="test.py")
    assert collector.cases == {}


def test_policy_compiles_required_scenario_without_running_candidate(tmp_path, monkeypatch):
    api, workspace, definition = policy_fixture(tmp_path, "def test_ok(): assert True\n")
    run_id = api.start(domain_id="develop", goal="Measure test cases", workspace=str(workspace), parameters={}, request_id="start")["run_id"]
    checks = api.records(run_id, "checks")["items"]
    rule = next(c for c in checks if c["check_id"] == "domain.tests")["spec"]["parameters"]["adapter"]["rules"]
    assert rule["required_case_ids"] == ["test_cases.py::test_ok"]
    assert api.resume(run_id)["measurement"]["status"] == "not_run"


def test_profile_is_advisory_and_node_ids_can_be_parameterized(tmp_path):
    api, workspace, _ = policy_fixture(tmp_path, "def test_ok(): assert True\n", required="test_cases.py::test_ok[one/two]")
    run_id = api.start(domain_id="develop", goal="Observe state", workspace=str(workspace), parameters={"validation_profile": "stateful-cli"}, request_id="start")["run_id"]
    perspectives = api.resume(run_id)["domain_preparation"]["validation_perspectives"]
    assert "defaults_equivalence" in perspectives["perspectives"]
    assert perspectives["meaning"] == "advisory_not_mandatory_tests"


@pytest.mark.skipif(os.environ.get("BASE_HARNESS_EXTERNAL_LIVE") != "1", reason="actual pytest in strict namespace Sandbox")
def test_live_adapter_collects_and_executes_without_host_pytest_in_sandbox(tmp_path):
    payload = tmp_path / "payload"
    payload.mkdir()
    (payload / "test_cases.py").write_text("import pytest\ndef test_ok(): assert True\ndef test_bad(): assert False\n@pytest.mark.skip(reason='not available')\ndef test_skip(): pass\n")
    state = tmp_path / "state"
    state.mkdir()
    check = normalize({"id": "tests", "paths": ["test_cases.py"]}, ["test_cases.py"])
    observed = []
    capture, summary = execute(check, payload, state, threading.Event(), 30, bind_check(check, state), lambda case, _: observed.append(case))
    assert capture["status"] == "completed", capture
    assert summary["collection_complete"] and summary["session_complete"], summary
    assert {case["outcome"] for case in observed} == {"passed", "failed", "skipped"}
    assert sum(case["executed"] for case in observed) == 2
    assert not (payload / ".harness-cases.jsonl").exists()


@pytest.mark.skipif(os.environ.get("BASE_HARNESS_EXTERNAL_LIVE") != "1", reason="actual pytest in strict namespace Sandbox")
def test_live_requirement_binding_and_gate_rejects_missing_case(tmp_path, monkeypatch):
    api, workspace, _ = policy_fixture(tmp_path, "from app import value\ndef test_ok(): assert value() == 1\n", required="test_cases.py::test_missing")
    monkeypatch.setattr("harness_external.worker.spawn_worker", lambda *args: None)
    run_id = api.start(domain_id="develop", goal="Observe declared cases", workspace=str(workspace), parameters={}, request_id="start")["run_id"]
    api.submit(run_id, "submit")
    job_id = api.verify(run_id, "verify")["job_id"]
    execute_job(api.store.root, job_id)
    result = api.status(run_id, job_id)
    assert result["job"]["status"] == "completed", result
    assert result["job"]["result"]["status"] == "failed", result
    cases = api.records(run_id, "cases", job_id=job_id)["items"]
    assert len(cases) == 1 and cases[0]["case"]["outcome"] == "passed"
    report = result["job"]["result"]["requirement_observations"]
    requirement = next(r for r in report["requirements"] if r["requirement_id"] == "value")
    assert requirement["scenarios"][0]["status"] == "missing"
    assert result["job"]["completed_checks"] == 2 and result["job"]["completed_cases"] == 1
    with pytest.raises(HarnessError):
        api.finish(run_id, "finish", outcome="completed")


def measure_fixture(tmp_path, monkeypatch, source, *, args=None, required="test_cases.py::test_ok", timeout=10):
    api, workspace, definition = policy_fixture(tmp_path, source, required=required, args=args)
    definition["parameters"]["pytest_checks"][0]["timeout_seconds"] = timeout
    (api.policies.root / "behavior/policy.json").write_text(json.dumps(definition))
    monkeypatch.setattr("harness_external.worker.spawn_worker", lambda *args: None)
    run_id = api.start(domain_id="develop", goal="Check observable behavior", workspace=str(workspace), parameters={}, request_id="start")["run_id"]
    api.submit(run_id, "submit")
    job_id = api.verify(run_id, "verify")["job_id"]
    execute_job(api.store.root, job_id)
    return api, run_id, job_id, workspace


@pytest.mark.skipif(os.environ.get("BASE_HARNESS_EXTERNAL_LIVE") != "1", reason="real pytest state transitions")
@pytest.mark.parametrize("source,args,required,expected", [
    ("def test_ok(): assert True\ndef test_other(): assert True\n", ["-k", "other"], "test_cases.py::test_ok", "not_selected"),
    ("def test_first(): assert False\ndef test_ok(): assert True\n", ["-x"], "test_cases.py::test_ok", "not_run"),
    ("import pytest\n@pytest.mark.skip(reason='not ready')\ndef test_ok(): pass\n", [], "test_cases.py::test_ok", "skipped"),
    ("import pytest\n@pytest.fixture\ndef fixture():\n    yield\n    assert False\ndef test_ok(fixture): assert True\n", [], "test_cases.py::test_ok", "error"),
    ("import pytest\n@pytest.mark.xfail(reason='known')\ndef test_ok(): assert False\n", [], "test_cases.py::test_ok", "xfail"),
    ("import pytest\n@pytest.mark.xfail(reason='known')\ndef test_ok(): assert True\n", [], "test_cases.py::test_ok", "xpass"),
    ("import pytest\n@pytest.mark.xfail\ndef test_ok(): assert False\n", [], "test_cases.py::test_ok", "xfail"),
    ("import pytest\n@pytest.mark.xfail\ndef test_ok(): assert True\n", [], "test_cases.py::test_ok", "xpass"),
    ("import pytest\n@pytest.mark.xfail(strict=True)\ndef test_ok(): assert True\n", [], "test_cases.py::test_ok", "xpass"),
    ("def test_ok(): assert True\n", ["--collect-only"], "test_cases.py::test_ok", "not_run"),
])
def test_live_nonpassing_case_states_cannot_satisfy_default_gate(tmp_path, monkeypatch, source, args, required, expected):
    api, run_id, job_id, _ = measure_fixture(tmp_path, monkeypatch, source, args=args, required=required)
    cases = api.records(run_id, "cases", job_id=job_id)["items"]
    selected = next(item for item in cases if item["case"]["case_id"] == required)
    assert selected["case"]["outcome"] == expected
    assert api.resume(run_id)["gates"]["status"] == "unsatisfied"
    with pytest.raises(HarnessError):
        api.finish(run_id, "finish", outcome="completed")


@pytest.mark.skipif(os.environ.get("BASE_HARNESS_EXTERNAL_LIVE") != "1", reason="real pytest collection error")
def test_live_collection_error_is_unconfirmed_not_missing(tmp_path, monkeypatch):
    api, run_id, job_id, _ = measure_fixture(tmp_path, monkeypatch, "def broken(:\n", required="test_cases.py::test_absent")
    result = api.status(run_id, job_id)["job"]["result"]
    assert result["status"] == "incomplete"
    requirement = result["requirement_observations"]["requirements"][0]
    assert requirement["scenarios"][0]["status"] == "unconfirmed"
    assert not requirement["linked_checks_passed"]


@pytest.mark.skipif(os.environ.get("BASE_HARNESS_EXTERNAL_LIVE") != "1", reason="real partial observations on timeout")
def test_live_timeout_preserves_completed_cases(tmp_path, monkeypatch):
    api, run_id, job_id, _ = measure_fixture(tmp_path, monkeypatch,
        "import time\ndef test_ok(): assert True\ndef test_slow(): time.sleep(30)\n", timeout=2)
    cases = api.records(run_id, "cases", job_id=job_id)["items"]
    assert any(c["case"]["case_id"].endswith("::test_ok") and c["case"]["outcome"] == "passed" for c in cases)
    assert any(c["case"]["case_id"].endswith("::test_slow") and not c["case"]["finished"] for c in cases)
    assert next(c for c in cases if c["case"]["case_id"].endswith("::test_slow"))["case"]["executed"]
    assert api.records(run_id, "measurements", job_id=job_id)["items"][-1]["execution_status"] == "timed_out"
    assert api.resume(run_id)["measurement"]["status"] != "passed"
    assert api.resume(run_id)["gates"]["status"] == "unsatisfied"


@pytest.mark.skipif(os.environ.get("BASE_HARNESS_EXTERNAL_LIVE") != "1", reason="real pytest evidence and citations")
def test_live_passed_cases_can_be_cited_but_past_candidate_cannot_pass_current_gate(tmp_path, monkeypatch):
    api, run_id, job_id, workspace = measure_fixture(tmp_path, monkeypatch, "from app import value\ndef test_ok(): assert value() == 1\n")
    case = api.records(run_id, "cases", job_id=job_id)["items"][0]
    assessment = {"status": "satisfied", "summary": "Declared case observed", "uncertainties": [],
                  "cited_observation_ids": [case["observation_id"]], "interpretation_revision": 1}
    result = api.assess(run_id, assessment, "assessment")
    assert result["assessment"]["citation_summary"]["current"] == 1
    (workspace / "app.py").write_text("def value(): return 0\n")
    api.submit(run_id, "new-candidate")
    assert api.resume(run_id)["gates"]["status"] == "unsatisfied"
    result = api.assess(run_id, assessment, "past-assessment")
    assert result["assessment"]["citation_summary"]["contextual"] == 1


@pytest.mark.parametrize("bad", [[{}], None, ["x", "x"]])
def test_invalid_selectors_are_structured_errors(bad):
    with pytest.raises(HarnessError):
        normalize({"id": "check", "paths": bad}, ["test_a.py"])


def test_coverage_is_validated_before_promoting_required_cases(tmp_path):
    api, workspace, definition = policy_fixture(tmp_path, "def test_ok(): pass\n")
    definition["coverage"]["scenarios"] = None
    (api.policies.root / "behavior/policy.json").write_text(json.dumps(definition))
    with pytest.raises(HarnessError):
        api.start(domain_id="develop", goal="Check", workspace=str(workspace), parameters={}, request_id="bad")


@pytest.mark.skipif(os.environ.get("BASE_HARNESS_EXTERNAL_LIVE") != "1", reason="queue failure regression in real pytest")
def test_live_previous_queue_defaults_defect_then_baseline_improvement(tmp_path, monkeypatch):
    import shutil
    root = Path(__file__).resolve().parents[1]
    policy_root = tmp_path / "policies"
    shutil.copytree(root / "examples/acceptance/policies/pytest-queue", policy_root / "pytest-queue")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    correct = (root / "evaluation/hard_queue/reference/leasequeue.py").read_text()
    needle = 'return self.mutate("add", [job_id, payload, deps, priority, max_attempts], request_id, apply)'
    assert correct.count(needle) == 1
    broken = correct.replace(needle, 'return self.mutate("add", [job_id, payload, depends_on, priority, max_attempts], request_id, apply)')
    (workspace / "leasequeue.py").write_text(broken)
    api = Harness(tmp_path / "state", policy_root=policy_root)
    monkeypatch.setattr("harness_external.worker.spawn_worker", lambda *args: None)
    run_id = api.start(domain_id="develop", goal="Preserve dependency defaults on replay", workspace=str(workspace), parameters={},
                       policy_id="pytest-queue", request_id="start", capture_baseline=True)["run_id"]
    api.submit(run_id, "broken")
    first = api.verify(run_id, "check-broken")["job_id"]
    execute_job(api.store.root, first)
    cases = api.records(run_id, "cases", job_id=first)["items"]
    assert next(c for c in cases if c["case"]["case_id"].endswith("[empty]"))["case"]["outcome"] == "failed"
    (workspace / "leasequeue.py").write_text(correct)
    api.submit(run_id, "fixed")
    second = api.verify(run_id, "check-fixed", compare_baseline=True)["job_id"]
    execute_job(api.store.root, second)
    result = api.status(run_id, second)["job"]["result"]
    assert result["status"] == "passed", result
    assert next(c for c in result["baseline_comparison"]["changes"] if c["check_id"] == "domain.behavior")["status"] == "improved"
    assert result["measurement_scope"]["test_case_count"] == 3
    assert len(api.records(run_id, "cases", job_id=second)["items"]) == 6
    requirement = result["requirement_observations"]["requirements"][0]
    assert requirement["linked_checks_passed"] and requirement["known_gap_ids"] == ["concurrency-not-measured"]
    assert api.finish(run_id, "finish", outcome="completed")["record"]["ready"] is False


@pytest.mark.skipif(os.environ.get("BASE_HARNESS_EXTERNAL_LIVE") != "1", reason="real pinned runner and readonly runtime")
def test_live_runtime_is_readonly_and_candidate_cannot_shadow_pytest(tmp_path):
    payload = tmp_path / "payload"
    payload.mkdir()
    (payload / "pytest.py").write_text("raise AssertionError('Candidate shadow must not load')\n")
    (payload / "test_runtime.py").write_text(
        "import pytest\nfrom pathlib import Path\ndef test_runtime():\n"
        "    assert 'packages.zip' in pytest.__file__\n"
        "    with pytest.raises(OSError):\n        Path('/opt/harness-runtime/runner.py').write_text('changed')\n")
    state = tmp_path / "state"
    state.mkdir()
    check = normalize({"id": "runtime", "paths": ["test_runtime.py"]}, ["test_runtime.py", "pytest.py"])
    capture, summary = execute(check, payload, state, threading.Event(), 30, bind_check(check, state), lambda *args: None)
    assert capture["capture"]["exitCode"] == 0, capture
    assert summary["cases"][0]["outcome"] == "passed"


def test_changed_observed_case_scope_is_not_baseline_improvement():
    from harness_external.evidence import compare
    check = {"check_id": "tests", "ref": {}, "spec": {"parameters": {"kind": "command"}}}
    subject = {"manifest": {"files": [{"path": "test.py"}]}, "candidate_hash": "baseline"}
    job = {"checks": [check], "baseline": subject, "candidate": subject, "candidate_hash": "candidate", "check_set_hash": "checks"}
    items = [{"check_id": "tests", "subject_role": role, "observation_id": role, "comparison_status": result,
              "execution_status": "completed", "environment_hash": "same", "case_summary": {"case_scope_hash": scope}}
             for role, result, scope in (("baseline", "failed", "old"), ("candidate", "passed", "changed"))]
    result = compare(items, job, {"acceptance": {"definition": {"required_check_ids": ["tests"]}}})
    assert result["changes"][0]["status"] == "inconclusive"
    assert "observed_case_scope_changed" in result["changes"][0]["incomparable_reasons"]


@pytest.mark.skipif(os.environ.get("BASE_HARNESS_EXTERNAL_LIVE") != "1", reason="real case binding and corruption detection")
def test_live_case_identity_and_late_write_rejection(tmp_path, monkeypatch):
    api, run_id, job_id, _ = measure_fixture(tmp_path, monkeypatch, "def test_ok(): assert True\n")
    case = api.records(run_id, "cases", job_id=job_id)["items"][0]
    with api.store.transaction() as connection:
        job = api.store.job(connection, job_id)
        for field, wrong in (("job_id", "job_" + "0" * 32), ("candidate_hash", "0" * 64),
                             ("check_id", "other"), ("attempt", 999), ("check_set_hash", "0" * 64),
                             ("acceptance_binding_hash", "0" * 64), ("domain_identity", {})):
            changed = {**case, field: wrong}
            with pytest.raises(HarnessError):
                api.store.validate_case_binding(changed, job)
        with pytest.raises(HarnessError) as error:
            api.store.checkpoint_case(connection, job_id, job["owner"], case)
        assert error.value.code == "STALE_CHECKPOINT"
    with api.store.transaction() as connection:
        connection.execute("DELETE FROM case_observations WHERE observation_id=?", (case["observation_id"],))
    with pytest.raises(HarnessError):
        api.finish(run_id, "finish", outcome="completed")


def test_runtime_preparation_does_not_hold_the_writer_transaction(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    api, workspace, _ = policy_fixture(tmp_path, "def test_ok(): assert True\n")
    run_id = api.start(domain_id="develop", goal="one", workspace=str(workspace), parameters={}, request_id="one")["run_id"]
    other = api.start(domain_id="develop", goal="two", workspace=str(workspace), parameters={}, request_id="two")["run_id"]
    api.submit(run_id, "submit")
    entered, release = threading.Event(), threading.Event()
    original_bind = bind
    def slow(*args):
        entered.set()
        assert release.wait(10)
        return original_bind(*args)
    monkeypatch.setattr("harness_external.adapter_execution.bind", slow)
    monkeypatch.setattr("harness_external.worker.spawn_worker", lambda *args: None)
    with ThreadPoolExecutor() as pool:
        future = pool.submit(api.verify, run_id, "verify")
        assert entered.wait(5)
        try:
            api.observe(other, {"note": "other Run can progress"}, "note")
            assert not future.done()
        finally:
            release.set()
        assert future.result()["status"] == "queued"


def test_unavailable_adapter_is_not_missing_or_passing(tmp_path, monkeypatch):
    api, workspace, _ = policy_fixture(tmp_path, "def test_ok(): pass\n")
    def missing(*args):
        raise HarnessError("PYTEST_RUNTIME_UNAVAILABLE", "fixture missing packages")
    monkeypatch.setattr("harness_external.pytest_adapter.prepare_runtime", missing)
    monkeypatch.setattr("harness_external.worker.spawn_worker", lambda *args: None)
    run_id = api.start(domain_id="develop", goal="check", workspace=str(workspace), parameters={}, request_id="start")["run_id"]
    api.submit(run_id, "submit")
    job_id = api.verify(run_id, "verify")["job_id"]
    execute_job(api.store.root, job_id)
    measured = api.records(run_id, "measurements", job_id=job_id)["items"][-1]
    assert measured["execution_status"] == "unavailable"
    report = api.status(run_id, job_id)["job"]["result"]["requirement_observations"]
    assert report["requirements"][0]["scenarios"][0]["status"] == "unconfirmed"
    assert api.resume(run_id)["gates"]["status"] == "unsatisfied"
