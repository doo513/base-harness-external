"""Real external observations of disposable CLI fixtures and failure behavior."""
import copy
import json
import os

import pytest

from harness_external.errors import HarnessError
from harness_external.process_adapter import ProcessObservationAdapter
from harness_external.service import Harness
from harness_external.worker import execute_job


APP = """import json, sys
from pathlib import Path
mode = sys.argv[1]
target = Path('fixtures/output.json')
if mode == 'write':
    target.write_text(json.dumps({'answer': 42}), encoding='utf-8')
elif mode == 'destroy':
    target.write_text('destroyed', encoding='utf-8')
elif mode == 'forge':
    Path('.harness-cases.jsonl').write_text('{\"event\":\"finished\",\"outcome\":\"passed\"}', encoding='utf-8')
    target.write_text('destroyed', encoding='utf-8')
elif mode == 'wrong-file':
    Path('fixtures/unrelated.json').write_text('changed', encoding='utf-8')
if mode != 'write':
    print('invalid input', file=sys.stderr)
    sys.exit(2)
"""


def parameters(mode="error"):
    cases = [{"id": "preserved", "kind": "file", "path": "fixtures/output.json", "operator": "unchanged"},
             {"id": "error", "stream": "stderr", "operator": "contains", "expected": "invalid input"}]
    if mode == "write":
        cases = [{"id": "output", "kind": "file", "path": "fixtures/output.json", "operator": "json_equals", "expected": {"answer": 42}}]
    return {"inputs": ["app.py"], "artifacts": ["app.py"],
            "expectations": [{"id": "entry", "path": "app.py", "operator": "contains", "expected": "import"}],
            "execution_checks": [{"kind": "cases", "id": "scenario", "adapter_id": "process-observations-v1",
                "required_cases": [case["id"] for case in cases], "selector": {"source_paths": ["app.py"],
                "argv": ["python3", "-B", "app.py", mode], "expected_exit_code": 0 if mode == "write" else 2,
                "fixtures": [{"path": "fixtures/output.json", "text": "ORIGINAL"}], "cases": cases}}],
            "requirements": [{"id": "R1", "kind": "preservation" if mode != "write" else "output",
                              "statement": "Observe the declared output", "check_ids": ["domain.scenario"], "minimum_evidence": "testcase",
                              "source_refs": [{"source_id": "goal", "start_line": 1, "end_line": 1}]}]}


@pytest.mark.parametrize("path", ["app.py", "APP.PY", "app.py/nested", "../outside", "/absolute"])
def test_fixture_cannot_replace_or_escape_submitted_code(tmp_path, path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    params = parameters()
    params["execution_checks"][0]["selector"]["fixtures"][0]["path"] = path
    api = Harness(tmp_path / "state")
    with pytest.raises(HarnessError):
        api.start(domain_id="develop", goal="R1. Preserve", workspace=str(workspace), parameters=params, request_id="start")
    assert api.list_runs()["items"] == []


def test_domain_schema_discovery_exposes_registered_selectors_without_a_run(tmp_path):
    api = Harness(tmp_path / "state")
    description = api.describe()
    assert {"requirements", "execution_checks", "coverage"} <= description["input_schema"]["properties"].keys()
    adapters = {item["adapter_id"]: item for item in description["adapters"]}
    observer = adapters["process-observations-v1"]
    assert observer["observation_boundary"] == "external_capture"
    assert {"fixtures", "expected_exit_code"} <= observer["selector_schema"]["properties"].keys()
    assert adapters["pytest-cases-v1"]["observation_boundary"] == "subject_process"
    assert api.list_runs()["items"] == []


def test_domain_schema_cannot_override_core_assurance_fields(tmp_path):
    from harness_external.domain import DevelopModule
    from harness_external.registry import DomainRegistry
    class MisconfiguredDomain(DevelopModule):
        def describe_parameters(self):
            return {"schema_version": "domain-input-schema-v1", "input_schema": {"type": "object"}, "ready": True}
    api = Harness(tmp_path / "state", domains=DomainRegistry([MisconfiguredDomain()]))
    with pytest.raises(HarnessError):
        api.describe()


def test_incomplete_file_metadata_cannot_claim_unchanged():
    adapter = ProcessObservationAdapter()
    params = parameters()["execution_checks"][0]
    check = {"adapter": {"selector": params["selector"]}}
    capture = {"status": "completed", "capture": {"execution": "completed", "startedAt": "2026-10-01T00:00:00Z",
               "finishedAt": "2026-10-01T00:00:01Z", "stdout": "", "stderr": "invalid input"},
               "file_observations": [{"path": "fixtures/output.json", "before": {"kind": "file"}, "after": {"kind": "file"}}]}
    with pytest.raises(HarnessError, match="metadata"):
        adapter.observe_capture(capture, check)


def test_negative_exit_is_pinned_in_the_check_and_cannot_change_at_dispatch(tmp_path):
    from harness_external.adapter_execution import command, validate_command
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    api = Harness(tmp_path / "state")
    begun = api.start(domain_id="develop", goal="R1. Preserve output on invalid input", workspace=str(workspace), parameters=parameters(), request_id="start")
    check = begun["contract"]["checks"][1]
    assert check["expectedExitCode"] == 2
    execution = command(check, api.adapters)
    assert execution["expectedExitCode"] == check["expectedExitCode"]
    with pytest.raises(HarnessError):
        validate_command(check, {**execution, "expectedExitCode": 0})


@pytest.mark.skipif(os.environ.get("BASE_HARNESS_EXTERNAL_LIVE") != "1", reason="real post-execution observation bound")
def test_live_oversized_output_retains_execution_fact_without_claiming_content_pass(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "app.py").write_text("from pathlib import Path\nPath('fixtures/output.json').write_text('x'*70000, encoding='utf-8')\n", encoding="utf-8")
    monkeypatch.setattr("harness_external.worker.spawn_worker", lambda *args: None)
    api = Harness(tmp_path / "state")
    begun = api.start(domain_id="develop", goal="R1. Produce the selected JSON", workspace=str(workspace), parameters=parameters("write"), request_id="start")
    api.submit(begun["run_id"], "submit")
    job = api.verify(begun["run_id"], "verify")["job_id"]
    execute_job(api.store.root, job)
    case = api.records(begun["run_id"], "cases", job_id=job)["items"][0]["case"]
    assert case["executed"] and case["finished"] and case["outcome"] == "error"
    assert case["phases"][0]["file_state"]["after"]["size"] == 70000
    assert api.resume(begun["run_id"])["gates"]["status"] == "unsatisfied"


@pytest.mark.skipif(os.environ.get("BASE_HARNESS_EXTERNAL_LIVE") != "1", reason="real strict Sandbox CLI fixture/observation")
@pytest.mark.parametrize("mode,outcome", [("error", "passed"), ("destroy", "failed"), ("forge", "failed"), ("write", "passed")])
def test_live_cli_scenario_checks_actual_files_and_expected_failure(tmp_path, monkeypatch, mode, outcome):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "app.py").write_text(APP, encoding="utf-8")
    monkeypatch.setattr("harness_external.worker.spawn_worker", lambda *args: None)
    api = Harness(tmp_path / "state")
    begun = api.start(domain_id="develop", goal="R1. Observe the declared output", workspace=str(workspace), parameters=parameters(mode), request_id="start")
    diagnostics = begun["domain_preparation"]["verification_plan"]["diagnostics"]
    if mode != "write":
        assert not any(item["code"] == "PRESERVATION_NOT_OBSERVED" for item in diagnostics)
    api.submit(begun["run_id"], "submit")
    job = api.verify(begun["run_id"], "verify")["job_id"]
    execute_job(api.store.root, job)
    state = api.status(begun["run_id"], job)
    assert state["job"]["error"] is None, state["job"]["error"]
    assert state["job"]["result"]["status"] == outcome
    cases = api.records(begun["run_id"], "cases", job_id=job)["items"]
    file = next(item for item in cases if item["case"]["case_id"] in {"preserved", "output"})
    assert file["observation_boundary"] == "external_capture"
    assert file["case"]["outcome"] == outcome
    observed = file["case"]["phases"][0]["file_state"]
    assert observed["before"]["kind"] == observed["after"]["kind"] == "file"
    assert not (workspace / "fixtures").exists()
    assert (workspace / "app.py").read_text(encoding="utf-8") == APP
    assert state["job"]["timings"]["sandbox"]["observed_commands"] == 1
    assert state["job"]["timings"]["sandbox"]["execution_ms"] > 0


@pytest.mark.skipif(os.environ.get("BASE_HARNESS_EXTERNAL_LIVE") != "1", reason="public caller, detached worker and real external acceptance loop")
def test_live_source_to_acceptance_to_failure_repair_and_closeout(tmp_path):
    from test_skill_workflow import helper, ROOT
    workspace = tmp_path / "project"
    workspace.mkdir()
    broken = APP.replace("mode = sys.argv[1]", "mode = 'destroy'")
    (workspace / "app.py").write_text(broken, encoding="utf-8")
    policy_root = tmp_path / "policies"
    directory = policy_root / "preservation"
    (directory / "bundle").mkdir(parents=True)
    params = parameters()
    requirements = params.pop("requirements")
    requirements[0]["check_ids"].append("domain.entry")
    requirements[0]["source_refs"][0]["source_id"] = "spec"
    policy = {"schema_version": "acceptance-policy-v1", "policy_id": "preservation", "revision": "1", "domain_id": "develop",
              "parameters": params, "required_check_ids": ["domain.entry", "domain.scenario"], "requirements": requirements,
              "coverage": {"schema_version": "develop-coverage-v2", "profile_id": "preservation", "profile_revision": "1", "known_gaps": [],
                           "scenarios": [{"id": "unchanged", "requirement_id": "R1", "check_id": "domain.scenario", "kind": "negative",
                                          "required": True, "test_ref": {"path": "app.py", "case_id": "preserved"}}]},
              "bundle": {"version": "1", "files": []}, "approval": {"declared_by": "independent test fixture", "reference": "fixed test policy"}}
    (directory / "policy.json").write_text(json.dumps(policy), encoding="utf-8")
    (policy_root / "registry.json").write_text(json.dumps({"defaults": {"develop": "preservation"}}), encoding="utf-8")
    config = helper.configure(tmp_path / "client.json", str(ROOT), str(tmp_path / "state"), ephemeral=True,
                              policy_root=str(policy_root))["config"]
    client = helper.Client(config, trace_file=tmp_path / "cost.jsonl")
    discovery = client.call({"operation": "describe", "arguments": {"domain_id": "develop"}})
    assert discovery["ok"] and discovery["configured_default_policy_id"] == "preservation"
    specification = "R1. Preserve fixtures/output.json when the command reports invalid input.\r\n"
    begun = client.call_operation("start", {"domain": "develop", "goal": "Implement the supplied specification", "workspace": str(workspace),
        "mode": "acceptance", "policy_id": "preservation", "parameters": {}, "provenance": {"declared_author": "model"},
        "sources": [{"id": "spec", "text": specification}]}, action_id="start", output=tmp_path / "start.json",
        binding_output=tmp_path / "binding.json")
    assert begun["ok"], begun
    binding = helper.read_json(str(tmp_path / "binding.json"))
    first = client.checkpoint(binding, "broken", wait_seconds=30)
    assert first["ok"] and first["wait_status"] == "terminal", first
    assert first["job"]["result"]["status"] == "failed"
    failed = client.attention(first, binding)
    assert any(item.get("case_id") == "preserved" and item["status"] == "failed" for item in failed["attention"])
    refused = client.call_operation("finish", {"outcome": "completed"}, binding=binding, action_id="refused", output=tmp_path / "refused.json")
    assert not refused["ok"] and refused["error"]["code"] == "VERIFICATION_REQUIRED"
    (workspace / "app.py").write_text(APP, encoding="utf-8")
    second = client.checkpoint(binding, "fixed", wait_seconds=30)
    assert second["ok"] and second["wait_status"] == "terminal", second
    assert second["job"]["result"]["status"] == "passed"
    assert second["job"]["result"]["requirement_observations"]["requirements"][0]["declared_scenarios_observed"]
    restored = client.call(client.make_request("resume", {}, binding=binding)["request"])
    assert restored["intent"]["sources"][0]["text"] == specification
    assert restored["gates"]["status"] == "passed"
    closed = client.call_operation("finish", {"outcome": "completed"}, binding=binding, action_id="finished", output=tmp_path / "finished.json")
    assert closed["ok"] and closed["verification_scope"]["required_check_count"] == 2
    assert closed["verification_scope"]["gap_count"] == 0
    assert closed["verification_scope"]["coverage_conclusion"] == "not_established"
    assert not (workspace / "fixtures").exists()
