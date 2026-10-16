"""Original documents survive the caller/Core boundary and inform Domain review."""
import hashlib

import pytest

from harness_external.errors import HarnessError
from harness_external.service import Harness
from test_skill_workflow import helper


def scope():
    return {"profile": "structural", "inputs": ["result.txt"], "artifacts": ["result.txt"],
            "expectations": [{"id": "file", "path": "result.txt", "operator": "equals", "expected": "ok"}],
            "requirements": [{"id": "R01", "statement": "Preserve original bytes", "kind": "preservation", "check_ids": ["domain.file"],
                              "source_refs": [{"source_id": "spec", "start_line": 1, "end_line": 1}]}]}


def start(tmp_path, *, parameters=None, sources=None):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    api = Harness(tmp_path / "state")
    arguments = dict(domain_id="develop", goal="Implement the supplied specification", workspace=str(workspace),
                     parameters=scope() if parameters is None else parameters,
                     sources=sources if sources is not None else [{"id": "spec", "text": "R01. Preserve bytes\r\nR02. Handle errors\r\n"}],
                     mode="exploratory", request_id="start")
    return api, arguments, api.start(**arguments)


def test_sources_are_pinned_without_creating_approval_or_gates(tmp_path):
    api, arguments, begun = start(tmp_path)
    resumed = Harness(api.store.root).resume(begun["run_id"])
    source = resumed["intent"]["sources"][0]
    assert source["text"] == arguments["sources"][0]["text"]
    assert source["sha256"] == hashlib.sha256(source["text"].encode("utf-8")).hexdigest()
    assert resumed["policy"]["required_check_ids"] == []
    plan = resumed["domain_preparation"]["verification_plan"]
    codes = {item["code"] for item in plan["diagnostics"]}
    assert {"SOURCE_REQUIREMENTS_UNMAPPED", "BEHAVIOR_HAS_FILE_ONLY_CHECKS", "PRESERVATION_NOT_OBSERVED"} <= codes
    assert plan["unmapped_source_requirement_count"] == 1
    assert plan["referenced_requirement_count"] == 1
    assert api.start(**arguments)["run_id"] == begun["run_id"]
    arguments["sources"][0]["text"] += "R03. New requirement"
    with pytest.raises(HarnessError):
        api.start(**arguments)


@pytest.mark.parametrize("reference", [
    {"source_id": "unknown", "start_line": 1, "end_line": 1},
    {"source_id": "spec", "start_line": 2, "end_line": 1},
    {"source_id": "spec", "start_line": True, "end_line": 1},
    {"source_id": "spec", "start_line": 1, "end_line": 999},
])
def test_invalid_source_reference_cannot_be_registered(tmp_path, reference):
    parameters = scope()
    parameters["requirements"][0]["source_refs"] = [reference]
    with pytest.raises(HarnessError):
        start(tmp_path, parameters=parameters)
    assert Harness(tmp_path / "state").list_runs()["items"] == []


def test_document_hash_and_line_binding_survive_interpretation_revision(tmp_path):
    api, _, begun = start(tmp_path)
    original = api.resume(begun["run_id"])["intent"]
    parameters = scope()
    parameters["requirements"].append({"id": "R02", "kind": "failure", "statement": "Handle errors", "check_ids": [],
                                       "source_refs": [{"source_id": "spec", "start_line": 2, "end_line": 2}]})
    revised = api.revise(begun["run_id"], {"expected_revision": 1, "parameters": parameters}, "revise")
    assert revised["domain_preparation"]["verification_plan"]["unmapped_source_requirement_count"] == 0
    assert api.resume(begun["run_id"])["intent"] == original


def test_caller_reads_exact_originals_and_saved_payload_needs_no_file_again(tmp_path):
    goal, specification = tmp_path / "goal.txt", tmp_path / "SPEC.md"
    goal.write_bytes("개발해 주세요\r\n".encode("utf-8"))
    specification.write_bytes(b"R01. Preserve bytes\r\n")
    arguments = helper.source_arguments("start", {}, goal_file=goal, source_files=["spec=" + str(specification)])
    assert arguments["goal"] == "개발해 주세요\r\n"
    assert arguments["sources"][0]["text"] == "R01. Preserve bytes\r\n"
    saved = tmp_path / "saved.json"
    helper.write_new_json(saved, arguments)
    specification.write_text("changed after submission", encoding="utf-8")
    assert helper.read_json(str(saved)) == arguments
    with pytest.raises(helper.ClientError):
        helper.source_arguments("start", {"goal": "summary"}, goal_file=goal)
    with pytest.raises(helper.ClientError):
        helper.source_arguments("revise", {}, source_files=["spec=" + str(specification)])


@pytest.mark.parametrize("sources", [[{"id": "goal", "text": "reserved"}], [{"id": "spec", "text": "x"}] * 2,
                                     [{"id": "spec", "text": "x" * 64001}], [{"id": "spec", "text": "\ud800"}]])
def test_invalid_original_documents_fail_before_publication(tmp_path, sources):
    with pytest.raises(HarnessError):
        start(tmp_path, sources=sources)
