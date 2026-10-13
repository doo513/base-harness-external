"""Host-selected advice is a view, not a contract or execution transition."""
import copy

import pytest

from harness.common import canonical_bytes, canonical_hash
from harness_external import develop_analysis, journal
from harness_external.context import apply_page
from harness_external.domain import DevelopModule
from harness_external.errors import HarnessError
from harness_external.registry import DomainRegistry
from harness_external.service import Harness
from harness_external.worker import execute_job


def parameters():
    return {"profile": "structural", "inputs": ["payload"], "artifacts": ["payload"],
            "expectations": [{"id": "payload", "path": "payload", "operator": "equals", "expected": "ready"}]}


@pytest.fixture
def task(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "payload").write_text("ready", encoding="utf-8")
    monkeypatch.setattr("harness_external.worker.spawn_worker", lambda *args: None)
    return Harness(tmp_path / "state"), workspace


def start(api, workspace, **options):
    return api.start(domain_id="develop", workspace=str(workspace), goal="Keep the requested behavior", parameters=parameters(),
                     mode="exploratory", required_checks=["domain.payload"], constraints=["Preserve evidence"],
                     request_id="start", **options)


@pytest.mark.parametrize("partial", [False, True])
def test_prepare_selects_advice_without_changing_contract_questions_or_operations(partial):
    module = DevelopModule()
    params = {} if partial else parameters()
    full = module.prepare("Keep intent intact", params, {}, exploratory=True)
    selected = module.prepare("Keep intent intact", params, {}, exploratory=True, analysis_selection={"perspectives": ["change"]})
    assert {key: value for key, value in selected.items() if key != "analysis_guidance"} == {
        key: value for key, value in full.items() if key != "analysis_guidance"}
    advice = selected["analysis_guidance"]
    assert [item["id"] for item in advice["analysis_perspectives"]] == ["change"]
    assert {item["id"] for item in advice["knowledge_map"]} == {"structure", "recovery"}
    assert advice["source_map_hash"] == full["analysis_guidance"]["map_hash"]
    assert advice["map_hash"] == canonical_hash({key: value for key, value in advice.items() if key != "map_hash"})


def test_acceptance_preparation_keeps_criteria_and_authority_when_selecting_advice():
    module = DevelopModule()
    policy = {"parameters": parameters(), "required_check_ids": ["domain.payload"], "bundle": {"files": []},
              "requirements": [{"id": "payload", "statement": "Preserve requested bytes", "check_ids": ["domain.payload"]}]}
    before = copy.deepcopy(policy)
    full = module.prepare_acceptance("Keep intent intact", {}, {}, policy=policy)
    selected = module.prepare_acceptance("Keep intent intact", {}, {}, policy=policy,
                                         analysis_selection={"perspectives": [], "knowledge": ["verification"]})
    assert selected["contract"] == full["contract"]
    assert selected["acceptance_check_ids"] == full["acceptance_check_ids"]
    assert policy == before
    assert selected["analysis_guidance"]["analysis_perspectives"] == []
    assert [item["id"] for item in selected["analysis_guidance"]["knowledge_map"]] == ["verification"]


def test_start_selected_response_replays_exactly_and_retains_full_context(task):
    api, workspace = task
    chosen = {"perspectives": ["change"]}
    first = start(api, workspace, analysis_selection=chosen)
    assert start(api, workspace, analysis_selection=copy.deepcopy(chosen)) == first
    with pytest.raises(HarnessError) as error:
        start(api, workspace, analysis_selection={"perspectives": ["design"]})
    assert error.value.code == "REQUEST_CONFLICT"
    advice = first["domain_preparation"]["analysis_guidance"]
    assert advice["selection"]["perspectives"] == ["change"]
    assert len(canonical_bytes(advice)) < len(canonical_bytes(develop_analysis.guidance()))
    page, document = api.context(first["run_id"]), {}
    while True:
        document = apply_page(document, page)
        if page["next_page"] is None:
            break
        page = api.context(first["run_id"], page=page["next_page"])
    assert document["field:domain_preparation"]["analysis_guidance"] == develop_analysis.guidance()
    assert document["field:intent"]["original_goal"] == "Keep the requested behavior"
    assert document["field:intent"]["constraints"] == ["Preserve evidence"]
    assert "analysis_selection" not in first["interpretation"]["parameters"]


def test_resume_selection_preserves_measured_state_and_current_citations(task):
    api, workspace = task
    run = start(api, workspace)["run_id"]
    api.submit(run, "submit")
    job = api.verify(run, "verify")["job_id"]
    execute_job(api.store.root, job)
    before = api.status(run)["run"]
    with api.store.transaction(write=False) as connection:
        seq = journal.head(connection, run)["seq"]
        requests = connection.execute("SELECT count(*) FROM requests").fetchone()[0]
    for view in ("compact", "summary"):
        result = api.resume(run, view=view, analysis_selection={"perspectives": [], "knowledge": ["runtime"]})
        assert result["gates"]["status"] == "passed"
        assert result["measurement"]["status"] == "passed"
        assert result["domain_preparation"]["analysis_guidance"]["selection"] == {"perspectives": [], "knowledge": ["runtime"]}
    assert api.status(run)["run"] == before
    with api.store.transaction(write=False) as connection:
        assert journal.head(connection, run)["seq"] == seq
        assert connection.execute("SELECT count(*) FROM requests").fetchone()[0] == requests
    observed = api.records(run, "measurements")["items"][0]["observation_id"]
    assessment = api.assess(run, {"interpretation_revision": 1, "status": "satisfied", "summary": "Measured original check",
                                 "uncertainties": [], "cited_observation_ids": [observed]}, "assessment")
    assert assessment["assessment"]["citation_summary"] == {"current": 1, "contextual": 0}


def test_resume_projects_the_stored_map_without_reloading_or_rerunning_prepare(task, monkeypatch):
    api, workspace = task
    run = start(api, workspace)["run_id"]
    original = api.resume(run)["domain_preparation"]["analysis_guidance"]
    def forbidden(*args, **kwargs):
        pytest.fail("A read must use recorded advice, not prepare the task or reload the current map")
    monkeypatch.setattr(develop_analysis, "guidance", forbidden)
    monkeypatch.setattr(DevelopModule, "prepare", forbidden)
    changed = api.resume(run, analysis_selection={"perspectives": ["verification"]})
    advice = changed["domain_preparation"]["analysis_guidance"]
    assert advice["source_map_hash"] == original["map_hash"]
    assert [item["id"] for item in advice["analysis_perspectives"]] == ["verification"]
    assert api.resume(run, analysis_selection={})["domain_preparation"]["analysis_guidance"] == original


@pytest.mark.parametrize("selection", [[], {"perspectives": ["missing"]}, {"perspectives": ["change", "change"]},
                                        {"knowledge": "runtime"}, {"knowledge": [{}]}, {"unknown": []}])
def test_bad_initial_selection_never_publishes_a_run(task, selection):
    api, workspace = task
    with pytest.raises(HarnessError):
        start(api, workspace, analysis_selection=selection)
    assert api.list_runs()["items"] == []


def test_old_domain_without_selection_capability_still_supports_normal_calls(task):
    class ExistingDomain(DevelopModule):
        select_analysis_guidance = None
    api, workspace = task
    api = Harness(api.store.root, domains=DomainRegistry([ExistingDomain()]))
    run = start(api, workspace)["run_id"]
    before = api.resume(run)
    with pytest.raises(HarnessError) as error:
        api.resume(run, analysis_selection={"perspectives": ["change"]})
    assert error.value.code == "DOMAIN_ANALYSIS_UNSUPPORTED"
    assert api.resume(run) == before


def test_core_forwards_opaque_selection_without_adopting_develop_semantics(task):
    class OtherSelection(DevelopModule):
        def select_analysis_guidance(self, guidance, selection):
            assert selection == {"section": "chosen-by-host"}
            guidance["opaque_view"] = selection["section"]
            return guidance
    api, workspace = task
    api = Harness(api.store.root, domains=DomainRegistry([OtherSelection()]))
    run = start(api, workspace)["run_id"]
    selected = api.resume(run, analysis_selection={"section": "chosen-by-host"})
    assert selected["domain_preparation"]["analysis_guidance"]["opaque_view"] == "chosen-by-host"
    assert "opaque_view" not in api.resume(run)["domain_preparation"]["analysis_guidance"]
