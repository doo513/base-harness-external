"""Behavior declarations use existing checks, revisions and observation joins."""
import copy
import json
import os

import pytest

from harness_external.domain import DevelopModule
from harness_external.errors import HarnessError
from harness_external.service import Harness
from harness_external.worker import execute_job
from test_acceptance_policy import configured


def parameters():
    return {"profile": "structural", "inputs": ["data.txt"], "artifacts": ["data.txt"],
            "expectations": [{"id": "content", "path": "data.txt", "operator": "equals", "expected": "preserved"}]}


def condition(**changes):
    return {"id": "R-preserve", "kind": "preservation", "when": "When processing existing data",
            "statement": "Preserve the declared value", "check_ids": ["domain.content"], **changes}


@pytest.fixture
def case(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "data.txt").write_text("preserved", encoding="utf-8")
    monkeypatch.setattr("harness_external.worker.spawn_worker", lambda *args: None)
    return Harness(tmp_path / "state"), workspace


def start(case, params, *, mode="exploratory", request_id="start"):
    api, workspace = case
    return api.start(domain_id="develop", goal="Preserve existing data", workspace=str(workspace),
                     parameters=params, mode=mode, request_id=request_id, provenance={"declared_author": "model"})


def measure(api, run_id, key="verify"):
    api.submit(run_id, key + "-submit")
    job_id = api.verify(run_id, key)["job_id"]
    execute_job(api.store.root, job_id)
    return api.status(run_id, job_id)["job"]


def test_conditions_reuse_check_identity_without_creating_gate_authority(case):
    api, _ = case
    supplied = {**parameters(), "requirements": [condition(), condition(id="R-failure", kind="failure", check_ids=[])]}
    untouched = copy.deepcopy(supplied)
    begun = start(case, supplied)
    assert supplied == untouched
    assert begun["contract"]["requirements"][0]["when"] == "When processing existing data"
    assert begun["contract"]["requirements"][0]["kind"] == "preservation"
    summary = api.resume(begun["run_id"])["domain_preparation"]["requirements"]
    assert summary["source"] == "caller_proposal"
    assert summary["unlinked_requirement_ids"] == ["R-failure"]
    assert summary["binding_status"] == "references_validated"
    assert summary["counts_by_kind"] == {"failure": 1, "preservation": 1}
    assert begun["policy"]["required_check_ids"] == []
    assert len(api.records(begun["run_id"], "checks")["items"]) == 1
    job = measure(api, begun["run_id"])
    report = job["result"]["requirement_observations"]["requirements"]
    assert report[0]["linked_checks_passed"] and not report[0]["linked_cases_passed"]
    assert not report[1]["linked_checks_passed"]
    assert api.resume(begun["run_id"])["gates"]["status"] == "not_required"


def test_partial_requirements_do_not_invent_checks_or_completion(case):
    api, _ = case
    begun = start(case, {"requirements": [condition()]})
    assert begun["phase"] == "waiting_input"
    assert begun["contract"]["requirements"][0]["id"] == "R-preserve"
    assert begun["domain_preparation"]["requirements"]["binding_status"] == "pending_preparation"
    assert begun["contract"]["checks"] == []
    assert "observation_links" not in begun["contract"]
    with pytest.raises(HarnessError):
        api.finish(begun["run_id"], "finish", outcome="completed")
    revised = api.revise(begun["run_id"], {"expected_revision": 1, "parameters": parameters()}, "complete-scope")
    assert revised["domain_preparation"]["requirements"]["binding_status"] == "references_validated"


@pytest.mark.parametrize("invalid", [
    None, {}, [condition(), condition()], [condition(id="")], [condition(kind="approved")],
    [condition(kind=[])], [condition(statement=" ")], [condition(when=[])], [condition(when="")],
    [condition(check_ids="domain.content")], [condition(check_ids=["domain.content", "domain.content"])],
    [condition(minimum_evidence="proven")], [condition(approval="authenticated")],
])
def test_invalid_condition_definitions_fail_as_structured_input_errors(invalid):
    with pytest.raises(HarnessError):
        DevelopModule().prepare("goal", {**parameters(), "requirements": invalid}, {})


@pytest.mark.parametrize("minimum", ["command", "testcase"])
def test_linked_file_checks_cannot_claim_stronger_evidence(minimum):
    with pytest.raises(HarnessError) as error:
        DevelopModule().prepare("goal", {**parameters(), "requirements": [condition(minimum_evidence=minimum)]}, {})
    assert error.value.code == "REQUIREMENT_EVIDENCE"


def test_undefined_check_id_is_rejected_but_explicitly_unlinked_condition_is_visible(case):
    with pytest.raises(HarnessError) as error:
        start(case, {**parameters(), "requirements": [condition(check_ids=["domain.typo"])]})
    assert error.value.code == "REQUIREMENT_REFERENCE"
    begun = start(case, {**parameters(), "requirements": [condition(check_ids=[], minimum_evidence="testcase")]}, request_id="unlinked")
    summary = begun["domain_preparation"]["requirements"]
    assert summary["unlinked_requirement_ids"] == ["R-preserve"]
    assert summary["unlisted_scenario_requirement_ids"] == ["R-preserve"]
    assert begun["contract"]["observation_links"]["requirements"][0]["check_ids"] == []


def test_all_condition_kinds_are_declarations_with_default_behavior():
    definitions = [condition(id="R-" + kind, kind=kind) for kind in ("input", "output", "behavior", "preservation", "conflict", "failure")]
    legacy = {"id": "R-legacy", "statement": "Preserve the value", "check_ids": ["domain.content"]}
    contract = DevelopModule().prepare("goal", {**parameters(), "requirements": [*definitions, legacy]}, {})["contract"]
    assert contract["requirements"][-1]["kind"] == "behavior"
    assert contract["requirement_summary"]["requirement_count"] == 7
    assert len(contract["checks"]) == 1


def test_existing_plain_preparation_does_not_gain_declarations_implicitly():
    prepared = DevelopModule().prepare("goal", parameters(), {})
    assert "requirements" not in prepared["contract"]
    assert "observation_links" not in prepared["contract"]
    assert "requirements_summary" not in prepared


def test_contract_revision_preserves_intent_policy_and_separate_design_assumptions(case):
    api, _ = case
    begun = start(case, {**parameters(), "requirements": [condition()]}, mode="strict")
    run_id = begun["run_id"]
    measure(api, run_id)
    original = api.status(run_id)["run"]
    api.revise(run_id, {"expected_revision": 1, "goal_summary": "Current implementation interpretation",
                       "parameters": {"requirements": [condition(when="After a supported transformation")]},
                       "assumptions": [{"id": "design", "statement": "Use a temporary file then replace"}],
                       "open_questions": ["Confirm output formatting"]}, "revise")
    current = api.status(run_id)["run"]
    assert current["intent"] == original["intent"] and current["policy"] == original["policy"]
    assert current["contract"]["original_goal"] == original["contract"]["original_goal"]
    assert current["contract"]["contract_hash"] != original["contract"]["contract_hash"]
    assert current["check_records"] == original["check_records"]
    assert current["interpretations"][0] == original["interpretations"][0]
    assert current["interpretations"][-1]["assumptions"][0]["id"] == "design"
    assert "assumptions" not in current["contract"]
    assert current["verification"]["status"] == "not_run"
    with pytest.raises(HarnessError):
        api.finish(run_id, "cannot-reuse-old-result", outcome="completed")
    updated = measure(api, run_id, "verify-revision")
    assert updated["result"]["requirement_observations"]["scope"]["contract_hash"] == current["contract"]["contract_hash"]
    assert api.finish(run_id, "finished", outcome="completed")["record"]["ready"] is False


def test_requirement_metadata_cannot_weaken_pinned_check(case):
    api, _ = case
    begun = start(case, {**parameters(), "requirements": [condition()]}, mode="strict")
    with pytest.raises(HarnessError) as error:
        api.revise(begun["run_id"], {"expected_revision": 1, "parameters": {
            "requirements": [], "coverage": None,
            "expectations": [{"id": "content", "path": "data.txt", "operator": "contains", "expected": "p"}]}}, "weaken")
    assert error.value.code == "GATE_POLICY_CHANGED"


@pytest.mark.parametrize("field,value", [("requirements", []), ("coverage", None)])
def test_configured_conditions_cannot_be_overridden_by_caller(configured, field, value):
    api, workspace, _, _ = configured
    begun = api.start(domain_id="develop", goal="Add integers", workspace=str(workspace), parameters={}, request_id="start")
    original = api.resume(begun["run_id"])
    with pytest.raises(HarnessError) as error:
        api.revise(begun["run_id"], {"expected_revision": 1, "parameters": {field: value}}, "override")
    assert error.value.code == "ACCEPTANCE_DECLARATION_PINNED"
    assert api.resume(begun["run_id"]) == original


def test_configured_requirements_share_schema_and_keep_channel_authority(configured):
    api, workspace, root, definition = configured
    definition["requirements"][0].update(kind="behavior", when="For integer operands")
    (root / "sum/policy.json").write_text(json.dumps(definition), encoding="utf-8")
    begun = api.start(domain_id="develop", goal="Add integers", workspace=str(workspace), parameters={}, request_id="start")
    assert begun["contract"]["requirements"][0]["when"] == "For integer operands"
    assert begun["contract"]["acceptance_requirements"] == definition["requirements"]
    assert begun["domain_preparation"]["requirements"]["source"] == "operator_configuration"
    assert begun["policy"]["provenance"]["approval"]["status"] == "configured_not_authenticated"
    assert begun["contract"]["observation_links"]["requirements"][0]["requirement_id"] == "addition"


def test_requirement_implementation_is_part_of_domain_identity(monkeypatch):
    from pathlib import Path
    from harness_external.registry import builtin_registry
    registry = builtin_registry()
    original_read = Path.read_bytes
    before = registry.identity("develop")
    def changed(path):
        result = original_read(path)
        return result + b"\n# changed condition compiler\n" if path.name == "develop_requirements.py" else result
    monkeypatch.setattr(Path, "read_bytes", changed)
    assert registry.identity("develop") != before


@pytest.mark.parametrize("alias", ["requirements", "coverage"])
def test_adapter_alias_cannot_consume_goal_declarations(alias):
    from harness_external.adapter_registry import AdapterRegistration, AdapterRegistry
    with pytest.raises(HarnessError) as error:
        AdapterRegistry([AdapterRegistration("probe", "example:Probe", parameter_alias=alias)])
    assert error.value.code == "ADAPTER_REGISTRATION_INVALID"


def case_parameters():
    return {"profile": "execution", "inputs": ["app.py", "test_app.py"], "artifacts": ["app.py"],
            "expectations": [{"id": "entry", "path": "app.py", "operator": "contains", "expected": "def value"}],
            "execution_checks": [{"kind": "cases", "id": "tests", "adapter_id": "pytest-cases-v1",
                                  "selector": {"paths": ["test_app.py"]}, "timeout_seconds": 10}],
            "requirements": [condition(id="R-value", kind="behavior", check_ids=["domain.tests"], minimum_evidence="testcase"),
                             condition(id="R-recovery", kind="failure", check_ids=[])],
            "coverage": {"schema_version": "develop-coverage-v2", "profile_id": "data", "profile_revision": "1",
                "scenarios": [{"id": "S-value", "requirement_id": "R-value", "check_id": "domain.tests", "kind": "positive", "required": True,
                               "test_ref": {"path": "test_app.py", "case_id": "test_app.py::test_value"}}],
                "known_gaps": [{"id": "recovery-gap", "requirement_id": "R-recovery", "reason": "Recovery remains to be investigated"}]}}


def test_caller_case_binding_reuses_adapter_and_does_not_assert_observation(case):
    api, _ = case
    begun = start(case, case_parameters())
    conditions = begun["contract"]["checks"][1]["adapter"]["rules"]
    assert conditions["required_case_ids"] == ["test_app.py::test_value"]
    summary = begun["domain_preparation"]["requirements"]
    assert summary["known_gap_ids"] == ["recovery-gap"]
    assert summary["unlisted_scenario_requirement_ids"] == ["R-recovery"]
    assert api.resume(begun["run_id"])["measurement"]["status"] == "not_run"
    assert api.resume(begun["run_id"])["gates"]["status"] == "not_required"


def test_advisory_declarations_do_not_normalize_adapter_selection_twice(case, monkeypatch):
    api, _ = case
    supplied = case_parameters()
    supplied["coverage"]["scenarios"][0]["required"] = False
    adapter = api.adapters.resolve("pytest-cases-v1")
    calls = []
    original = adapter.normalize_selection
    def counted(*args):
        calls.append(True)
        return original(*args)
    monkeypatch.setattr(adapter, "normalize_selection", counted)
    prepared = api.domains.resolve("develop").prepare("goal", supplied, {})
    assert len(calls) == 1
    assert prepared["contract"]["checks"][1]["adapter"]["rules"]["required_case_ids"] == []


def test_coverage_requires_an_explicit_requirement_inventory(case):
    supplied = case_parameters()
    supplied.pop("requirements")
    with pytest.raises(HarnessError) as error:
        start(case, supplied)
    assert error.value.code == "REQUIREMENTS_REQUIRED"


def test_legacy_scenario_names_remain_declarations_not_case_observations(case):
    api, _ = case
    supplied = {**parameters(), "requirements": [condition()], "coverage": {
        "schema_version": "develop-coverage-v1", "profile_id": "legacy", "profile_revision": "1", "known_gaps": [],
        "scenarios": [{"id": "S-file", "requirement_id": "R-preserve", "check_id": "domain.content", "kind": "positive",
                       "test_ref": {"path": "data.txt", "case_id": "logical_case_name"}}]}}
    begun = start(case, supplied)
    assert begun["contract"]["coverage_inventory"]["scenarios"][0]["test_ref"]["case_id"] == "logical_case_name"
    assert begun["contract"]["observation_links"]["scenarios"] == []
    job = measure(api, begun["run_id"])
    observed = job["result"]["requirement_observations"]["requirements"][0]
    assert observed["linked_checks_passed"]
    assert not observed["declared_scenarios_observed"] and not observed["linked_cases_passed"]


@pytest.mark.parametrize("change,code", [("check", "COVERAGE_CHECK"), ("path", "COVERAGE_TEST_REFERENCE"), ("requirement", "COVERAGE_REQUIREMENT")])
def test_scenarios_must_match_declared_requirements_checks_and_input_scope(case, change, code):
    params = case_parameters()
    scenario = params["coverage"]["scenarios"][0]
    if change == "check": scenario["check_id"] = "domain.entry"
    elif change == "path": scenario["test_ref"]["path"] = "outside.py"
    else: scenario["requirement_id"] = "R-typo"
    with pytest.raises(HarnessError) as error:
        start(case, params)
    assert error.value.code == code


@pytest.mark.skipif(os.environ.get("BASE_HARNESS_EXTERNAL_LIVE") != "1", reason="actual case collection/execution through strict Sandbox")
@pytest.mark.parametrize("source,outcome", [
    ("from app import value\ndef test_value(): assert value() == 1\n", "passed"),
    ("def test_value(): assert False\n", "failed"),
    ("import pytest\n@pytest.mark.skip(reason='not implemented')\ndef test_value(): pass\n", "skipped"),
    ("def test_other(): assert True\n", "missing"),
])
def test_live_declared_conditions_join_current_cases_and_preserve_completion_policy(case, source, outcome):
    api, workspace = case
    (workspace / "app.py").write_text("def value(): return 1\n", encoding="utf-8")
    (workspace / "test_app.py").write_text(source, encoding="utf-8")
    begun = start(case, case_parameters(), mode="strict")
    run_id = begun["run_id"]
    job = measure(api, run_id)
    assert job["status"] == "completed", job
    observed = job["result"]["requirement_observations"]
    requirement = observed["requirements"][0]
    assert requirement["scenarios"][0]["status"] == outcome
    assert requirement["linked_cases_passed"] is (outcome == "passed")
    assert observed["requirements"][1]["known_gap_ids"] == ["recovery-gap"]
    assert not observed["requirements"][1]["linked_checks_passed"]
    assert job["result"]["status"] == ("passed" if outcome == "passed" else "failed")
    if outcome == "passed":
        completed = api.finish(run_id, "finish", outcome="completed")
        assert completed["resolution"]["requirements"]["unlinked_requirement_ids"] == ["R-recovery"]
        assert completed["resolution"]["certification"] == "not_issued"
        assert completed["record"]["ready"] is False
    else:
        with pytest.raises(HarnessError) as error:
            api.finish(run_id, "finish", outcome="completed")
        assert error.value.code == "VERIFICATION_REQUIRED"
