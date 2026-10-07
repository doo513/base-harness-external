"""Develop reasoning guidance is data, not another Core workflow."""
import ast
import copy
from pathlib import Path

import pytest

from harness.common import canonical_bytes, canonical_hash
from harness_external import develop_analysis
from harness_external.domain import DevelopModule
from harness_external.errors import HarnessError
from harness_external.service import Harness
from harness_external.worker import execute_job


def parameters():
    return {"profile": "structural", "inputs": ["result.txt"], "artifacts": ["result.txt"],
            "expectations": [{"id": "result", "path": "result.txt", "operator": "equals", "expected": "done"}]}


def test_perspectives_connect_task_questions_to_relevant_knowledge_and_action():
    value = develop_analysis.guidance()
    assert value["schema_version"] == "develop-analysis-v2"
    assert value["persistence_required"] is False
    assert value["minimum_need_count"] == 0
    assert set(value["need_quality"]) == {"unknown", "decision_relevant", "investigable", "sufficient"}
    perspectives = value["analysis_perspectives"]
    assert {item["id"] for item in perspectives} == {"outcome", "existing_behavior", "design", "change", "verification"}
    knowledge = {item["id"]: item for item in value["knowledge_map"]}
    for perspective in perspectives:
        assert perspective["ask"] and perspective["knowledge_refs"]
        assert set(perspective["knowledge_refs"]) <= knowledge.keys()
    for item in knowledge.values():
        assert item["source_hints"] and item["enables"] and item["limitation"]
    assert set(value["discovery"]) == {"select", "inspect", "apply", "revisit"}
    assert value["map_hash"] == canonical_hash({k: v for k, v in value.items() if k != "map_hash"})
    assert len(canonical_bytes(value)) <= 8192


def test_guidance_has_no_io_model_or_run_state_dependency(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Reasoning guidance must not perform I/O")
    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(Path, "write_text", forbidden)
    monkeypatch.setattr(Path, "write_bytes", forbidden)
    monkeypatch.setattr("builtins.open", forbidden)
    value = develop_analysis.guidance()
    assert value["analysis_perspectives"]


def test_discovery_module_does_not_import_core_or_the_legacy_record_validator():
    source = Path(develop_analysis.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    imports.update(alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names)
    assert imports <= {"copy", "harness.common"}
    assert {node.name for node in tree.body if isinstance(node, ast.FunctionDef)} == {"guidance"}


def test_mutating_advice_does_not_change_preparation_contract_or_operations(monkeypatch):
    module = DevelopModule()
    original = module.prepare("Implement a feature", parameters(), {})
    advice = copy.deepcopy(original["analysis_guidance"])
    advice["analysis_perspectives"][0]["ask"] = "An alternative advisory question"
    monkeypatch.setattr(develop_analysis, "guidance", lambda: copy.deepcopy(advice))
    changed = module.prepare("Implement a feature", parameters(), {})
    assert changed["analysis_guidance"] != original["analysis_guidance"]
    for key in ("contract", "status", "questions", "available_operations"):
        assert changed[key] == original[key]


def test_goal_keywords_do_not_choose_a_prescribed_solution_or_need_inventory():
    module = DevelopModule()
    values = [module.prepare(goal, parameters(), {}) for goal in (
        "Add caching to an analyzer", "Support cancellation in a queue", "Add a preview option to a CLI")]
    assert values[0]["analysis_guidance"] == values[1]["analysis_guidance"] == values[2]["analysis_guidance"]
    assert all("needs" not in item["contract"] and "analysis_guidance" not in item["contract"] for item in values)
    values[0]["analysis_guidance"]["analysis_perspectives"].clear()
    assert module.prepare("A different feature", parameters(), {})["analysis_guidance"]["analysis_perspectives"]


def test_normal_workflow_executes_and_verifies_without_any_need_records(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    result = workspace / "result.txt"
    result.write_text("not done", encoding="utf-8")
    api = Harness(tmp_path / "state")
    monkeypatch.setattr("harness_external.worker.spawn_worker", lambda *args: None)
    def forbidden(*args, **kwargs):
        raise AssertionError("Advice must not require recording or normalizing Needs")
    monkeypatch.setattr("harness_external.need_records.record", forbidden)
    # A hostile legacy callback is never invoked by ordinary preparation/execution.
    class NoNeedDomain(DevelopModule):
        def normalize_need(self, details, contract):
            return forbidden()
    from harness_external.registry import DomainRegistry
    api.domains = DomainRegistry([NoNeedDomain()])
    started = api.start(domain_id="develop", goal="Produce the requested value", workspace=str(workspace),
                        parameters=parameters(), mode="strict", request_id="start")
    run = started["run_id"]
    assert started["domain_preparation"]["analysis_guidance"]["persistence_required"] is False
    api.submit(run, "before")
    job = api.verify(run, "measure-before")["job_id"]
    execute_job(api.store.root, job)
    assert api.resume(run)["measurement"]["status"] == "failed"
    with pytest.raises(HarnessError):
        api.finish(run, "cannot-finish", outcome="completed")
    result.write_text("done", encoding="utf-8")
    api.submit(run, "after")
    job = api.verify(run, "measure-after")["job_id"]
    execute_job(api.store.root, job)
    assert api.resume(run)["measurement"]["status"] == "passed"
    assert api.records(run, "needs")["total"] == 0
    assert api.records(run, "activity")["total"] == 0
    assert api.finish(run, "finish", outcome="completed")["record"]["ready"] is False


def test_guidance_available_before_scope_is_known_without_creating_scope():
    result = DevelopModule().prepare("Add a feature to an unfamiliar project", {}, {}, exploratory=True)
    assert result["analysis_guidance"]["persistence_required"] is False
    assert result["status"] == "needs_input"
    assert result["contract"]["inputs"] == result["contract"]["checks"] == []


def test_legacy_normalization_remains_optional_and_compatible():
    details = {"kind": "decision", "question": "Which policy applies?", "reason": "Choose a compatible implementation",
               "resolution_criterion": "Find the policy", "knowledge_refs": ["compatibility"]}
    result = DevelopModule().normalize_need(details, {"requirements": []})
    assert result == {**details, "requirement_ids": []}
