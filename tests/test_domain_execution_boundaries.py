"""Responsibility boundaries, with an independent Domain and opaque case IDs."""
import ast
import copy
import json
import os
from pathlib import Path
import sys
import threading

import pytest

from harness.common import canonical_hash
from harness_external.errors import HarnessError
from harness_external import observation_links
from harness_external.registry import DomainRegistry
from harness_external.service import Harness
from harness_external.worker import execute_job


def link_spec(case_id="row 42 / opaque identity"):
    return {"schema_version": "requirement-case-links-v1",
            "requirements": [{"requirement_id": "R1", "check_ids": ["domain.probe"], "known_gap_ids": ["unmeasured"]}],
            "scenarios": [{"scenario_id": "S1", "requirement_id": "R1", "check_id": "domain.probe", "case_id": case_id,
                           "required": False, "test_ref": {"description": "display metadata, not a selector"}}]}


@pytest.fixture
def observations():
    job = {"run_id": "run", "job_id": "job", "candidate_hash": "candidate", "contract_hash": "contract",
           "check_set_hash": "checks", "attempt": 1, "candidate": {"subject": {"id": "candidate", "revision": 1}},
           "checks": [{"check_id": "domain.probe", "ref": {"id": "probe", "revision": 1}}],
           "policy_ref": {"id": "policy"}, "interpretation_ref": {"id": "interpretation"},
           "domain_identity": {"id": "independent"}, "adapter_bindings": {"domain.probe": {"id": "opaque-adapter"}}}
    common = {key: copy.deepcopy(value) for key, value in job.items() if key not in {"candidate", "checks", "adapter_bindings"}}
    common.update(subject_role="candidate", subject=job["candidate"]["subject"], check_id="domain.probe",
                  check_ref=job["checks"][0]["ref"], origin="verifier")
    check = {**common, "observation_id": "check-observation", "comparison_status": "passed", "case_summary": {"collection_complete": True}}
    case = {**common, "observation_id": "case-observation", "execution_observation_id": "check-observation",
            "adapter_identity": job["adapter_bindings"]["domain.probe"],
            "case": {"case_id": "row 42 / opaque identity", "selected": True, "finished": True, "executed": True, "outcome": "passed"}}
    return job, check, case


def test_common_join_does_not_interpret_framework_or_display_metadata(observations):
    job, check, case = observations
    links = link_spec()
    links["scenarios"][0]["test_ref"] = {"case_id": "not the actual selector", "framework": "unregistered display label"}
    result = observation_links.report(links, [check], [case], job)
    requirement = result["requirements"][0]
    assert requirement["linked_cases_passed"] and requirement["linked_checks_passed"]
    assert requirement["known_gap_ids"] == ["unmeasured"]
    assert requirement["scenarios"][0]["observation_id"] == "case-observation"
    assert result["links_hash"] == canonical_hash(links)
    assert result["scope"]["candidate_hash"] == job["candidate_hash"]
    assert result["meaning"].endswith("not_goal_proof")
    assert "gates" not in result


@pytest.mark.parametrize("field,wrong", [
    ("run_id", "other"), ("job_id", "other"), ("candidate_hash", "old"), ("contract_hash", "other"),
    ("check_set_hash", "old"), ("attempt", 0), ("subject", {"id": "old"}), ("subject_role", "baseline"),
    ("check_ref", {"revision": 0}), ("check_id", "other"), ("policy_ref", {}), ("interpretation_ref", {}),
    ("domain_identity", {}), ("acceptance_binding_hash", "other"), ("origin", "caller"),
])
def test_foreign_or_historical_observations_cannot_satisfy_links(observations, field, wrong):
    job, check, case = observations
    check[field] = case[field] = wrong
    result = observation_links.report(link_spec(), [check], [case], job)["requirements"][0]
    assert not result["linked_checks_passed"] and not result["linked_cases_passed"]
    assert result["scenarios"][0]["status"] == "unconfirmed"


@pytest.mark.parametrize("field,wrong", [("adapter_identity", {"id": "other"}), ("execution_observation_id", "other")])
def test_case_must_match_its_adapter_and_check_receipt(observations, field, wrong):
    job, check, case = observations
    case[field] = wrong
    result = observation_links.report(link_spec(), [check], [case], job)["requirements"][0]
    assert not result["linked_cases_passed"]
    assert result["scenarios"][0]["observation_id"] is None


@pytest.mark.parametrize("outcome,executed,finished,seen", [
    ("passed", True, True, True), ("failed", True, True, True), ("error", False, True, True),
    ("skipped", False, True, True), ("xfail", True, True, True), ("xpass", True, True, True),
    ("not_run", False, False, False), ("not_selected", False, False, False), ("incomplete", True, False, False),
    ("passed", True, False, False),
])
def test_link_reports_preserve_normalized_facts_not_goal_judgments(observations, outcome, executed, finished, seen):
    job, check, case = observations
    case["case"].update(outcome=outcome, executed=executed, finished=finished)
    result = observation_links.report(link_spec(), [check], [case], job)["requirements"][0]
    assert result["scenarios"][0]["status"] == outcome
    assert result["declared_scenarios_observed"] is seen
    assert result["linked_cases_passed"] is (outcome == "passed" and finished)


def test_only_complete_collection_establishes_missing_case(observations):
    job, check, _ = observations
    for complete, expected in ((True, "missing"), (False, "unconfirmed")):
        check["case_summary"]["collection_complete"] = complete
        result = observation_links.report(link_spec(), [check], [], job)["requirements"][0]
        assert result["scenarios"][0]["status"] == expected
        assert not result["linked_cases_passed"]


def test_partial_case_does_not_complete_unmeasured_check(observations):
    job, _, case = observations
    result = observation_links.report(link_spec(), [], [case], job)["requirements"][0]
    assert result["linked_cases_passed"]
    assert not result["linked_checks_passed"]
    assert result["status"] != "linked_checks_passed"


@pytest.mark.parametrize("change", ["check", "requirement", "duplicate", "case", "schema"])
def test_common_link_contract_rejects_undefined_or_ambiguous_references(change):
    links = link_spec()
    if change == "check":
        links["scenarios"][0]["check_id"] = "undefined"
    elif change == "requirement":
        links["scenarios"][0]["requirement_id"] = "undefined"
    elif change == "duplicate":
        links["scenarios"].append(copy.deepcopy(links["scenarios"][0]))
    elif change == "case":
        links["scenarios"][0]["case_id"] = None
    else:
        links["schema_version"] = "develop-coverage-v2"
    with pytest.raises(HarnessError):
        observation_links.validate(links, {"domain.probe"})


def test_domain_defines_conditions_but_adapter_constructs_runtime_command(monkeypatch):
    from harness_external.develop_cases import normalize
    from harness_external import pytest_adapter
    from harness_external.domain import DevelopModule
    check = normalize({"id": "probe", "paths": ["test_probe.py"], "required_cases": ["test_probe.py::test_value"]}, ["test_probe.py"])
    before = copy.deepcopy(check)
    assert "argv" not in check and "cwd" not in check
    command = pytest_adapter.build_command(check)
    assert command["argv"][:2] == ["python3", "-I"]
    assert json.loads(command["argv"][-1]) == {"paths": ["test_probe.py"], "args": []}
    monkeypatch.setattr(pytest_adapter, "RESOURCE_MOUNT", "/different-adapter-layout")
    assert pytest_adapter.build_command(check)["argv"] != command["argv"]
    assert check == before
    for override in ({"argv": ["custom-runner"]}, {"cwd": "other"}):
        parameters = {key: value for key, value in check.items() if key not in {"check_id", "check_key"}}
        with pytest.raises(HarnessError):
            DevelopModule().normalize_check({**parameters, **override}, {"profile": "execution", "inputs": ["test_probe.py"]})


def test_reserved_observation_artifact_is_adapter_owned(tmp_path):
    from harness_external.adapter_execution import bind, execute
    from harness_external.develop_cases import normalize
    from harness_external.pytest_adapter import REPORT_PATH
    (tmp_path / REPORT_PATH).write_text("caller data")
    check = normalize({"id": "probe", "paths": ["test_probe.py"]}, ["test_probe.py", REPORT_PATH])
    binding = bind([{"check_id": "test", "spec": {"parameters": check}}], tmp_path)["test"]
    with pytest.raises(HarnessError) as error:
        execute(check, tmp_path, tmp_path, threading.Event(), 1, binding, lambda *args: None)
    assert error.value.code == "ADAPTER_RESOURCE_CONFLICT"
    assert (tmp_path / REPORT_PATH).read_text() == "caller data"


def test_import_boundaries_and_private_runtime_paths():
    root = Path(__file__).resolve().parents[1] / "src/harness_external"
    for name in ("worker.py", "observation_links.py", "pytest_adapter.py", "adapter_execution.py"):
        for node in ast.walk(ast.parse((root / name).read_text())):
            if isinstance(node, ast.ImportFrom):
                assert (node.module or "").split(".")[-1] not in {"domain", "develop_cases"}, (name, node.lineno)
                assert not (node.module in (None, "harness_external") and any(a.name in {"domain", "develop_cases"} for a in node.names)), (name, node.lineno)
            if isinstance(node, ast.Import):
                assert all(a.name not in {"harness_external.domain", "harness_external.develop_cases"} for a in node.names)
    for name in ("domain.py", "develop_cases.py"):
        source = (root / name).read_text()
        assert all(text not in source for text in ("/opt/harness-runtime", ".harness-cases.jsonl", "runner.py", "packages.zip"))
        assert all(text not in source for text in (".worker import", ".pytest_adapter import", ".adapter_execution import"))


class IndependentDomain:
    """Not a Develop subclass/wrapper; the same existing pytest adapter is reused."""
    domain_id = "independent-review"
    revision = "1"

    def prepare(self, goal, parameters, verifier, *, exploratory=False, intent=None):
        check = {"kind": "command", "expectedExitCode": 0, "timeout_seconds": 10,
                 "check_id": "domain.probe", "check_key": "probe",
                 "adapter": {"id": "pytest-cases-v1", "paths": ["test_probe.py"], "args": [],
                             "rules": {"required_case_ids": ["test_probe.py::test_value"], "allowed_outcomes": ["passed"],
                                       "minimum_selected": 1, "require_complete_session": True}}}
        contract = {"original_goal": goal, "domain_id": self.domain_id, "domain_revision": self.revision,
                    "inputs": ["test_probe.py"], "artifacts": ["test_probe.py"], "profile": "execution", "checks": [check],
                    "verifier": verifier, "rules": {"all_checks_required": True, "snapshot_required": True}, "limitations": [],
                    "observation_links": link_spec("test_probe.py::test_value")}
        contract["contract_hash"] = canonical_hash(contract)
        return {"contract": contract, "questions": [], "status": "proceed", "available_operations": ["submit", "verify", "finish_completed"]}

    def normalize_check(self, parameters, contract):
        return copy.deepcopy(parameters)


def independent_run(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "test_probe.py").write_text("def test_value(): assert 1 + 1 == 2\n")
    monkeypatch.setattr("harness_external.worker.spawn_worker", lambda *args: None)
    api = Harness(tmp_path / "state", domains=DomainRegistry([IndependentDomain()]))
    run_id = api.start(domain_id="independent-review", goal="Observe the declared case", workspace=str(workspace), parameters={}, request_id="start")["run_id"]
    api.submit(run_id, "submit")
    return api, run_id


def test_materialized_command_receipt_cannot_be_replaced(tmp_path, monkeypatch):
    api, run_id = independent_run(tmp_path, monkeypatch)
    monkeypatch.setattr("harness_external.adapter_execution.execute", lambda *args:
                        ({"status": "completed", "capture": {"argv": ["wrong-command"], "cwd": "."}}, None))
    job_id = api.verify(run_id, "verify")["job_id"]
    execute_job(api.store.root, job_id)
    assert api.status(run_id, job_id)["job"]["error"]["code"] == "RECEIPT_BINDING"
    assert api.resume(run_id)["gates"]["status"] == "unsatisfied"


@pytest.mark.skipif(os.environ.get("BASE_HARNESS_EXTERNAL_LIVE") != "1", reason="independent Domain through the real pytest Sandbox")
def test_live_independent_domain_completes_without_loading_develop(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "harness_external.domain", None)
    monkeypatch.setitem(sys.modules, "harness_external.develop_cases", None)
    api, run_id = independent_run(tmp_path, monkeypatch)
    job_id = api.verify(run_id, "verify")["job_id"]
    execute_job(api.store.root, job_id)
    result = api.status(run_id, job_id)["job"]["result"]
    assert result["status"] == "passed", result
    assert result["requirement_observations"]["requirements"][0]["linked_cases_passed"]
    assert result["requirement_observations"]["schema_version"] == "requirement-observations-v1"
    assert len(api.records(run_id, "cases", job_id=job_id)["items"]) == 1
    assert api.finish(run_id, "finish", outcome="completed")["record"]["ready"] is False
