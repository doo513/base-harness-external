"""Advisory Need analysis must not acquire verification or policy authority."""
import copy
from concurrent.futures import ThreadPoolExecutor

import pytest

from harness.common import canonical_hash
from harness_external import journal
from harness_external.context import apply_page
from harness_external.domain import DevelopModule
from harness_external.errors import HarnessError
from harness_external.registry import DomainRegistry
from harness_external.service import Harness
from harness_external.worker import execute_job


def parameters():
    return {"profile": "structural", "inputs": ["value.txt"], "artifacts": ["value.txt"],
            "expectations": [{"id": "value", "path": "value.txt", "operator": "equals", "expected": "kept"}],
            "requirements": [{"id": "R1", "statement": "Preserve the value", "check_ids": ["domain.value"]}]}


def proposal(**changes):
    return {"id": "preservation", "expected_revision": 0, "state": "open",
            "details": {"kind": "decision", "question": "What happens when an existing field conflicts?",
                        "reason": "Choose compatible behavior before changing the transformation",
                        "resolution_criterion": "Identify the policy and its source or ask the user",
                        "requirement_ids": ["R1"], "knowledge_refs": ["compatibility"]}, **changes}


def note(need=None, **changes):
    return {"kind": "need", "note": "Record a material analysis checkpoint", "need": proposal() if need is None else need, **changes}


@pytest.fixture
def task(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "value.txt").write_text("kept", encoding="utf-8")
    api = Harness(tmp_path / "state")
    monkeypatch.setattr("harness_external.worker.spawn_worker", lambda *args: None)
    begun = api.start(domain_id="develop", goal="Preserve existing data", workspace=str(workspace),
                      parameters=parameters(), request_id="start", mode="strict")
    return api, begun["run_id"], workspace


def measure(api, run_id):
    api.submit(run_id, "submit")
    job_id = api.verify(run_id, "verify")["job_id"]
    execute_job(api.store.root, job_id)
    return job_id


def restore(api, run_id, *, document=None, after=None, limit=20):
    page = api.context(run_id, after=after, limit=limit)
    document = document or {}
    while True:
        document = apply_page(document, page)
        if not page["next_page"]:
            return document, page
        page = api.context(run_id, page=page["next_page"], limit=limit)


def test_develop_guidance_is_an_index_not_generated_needs_or_a_gate():
    module = DevelopModule()
    result = module.prepare("A new kind of project", parameters(), {})
    guidance = result["analysis_guidance"]
    assert guidance["schema_version"] == "develop-analysis-v1"
    assert set(guidance["need_kinds"]) == {"knowledge", "observation", "decision", "verification"}
    assert {item["id"] for item in guidance["knowledge_map"]} == {
        "compatibility", "structure", "runtime", "recovery", "verification"}
    assert "analysis_guidance" not in result["contract"]
    assert "needs" not in result["contract"]
    assert len(result["contract"]["checks"]) == 1
    guidance["knowledge_map"].clear()
    assert module.prepare("goal", parameters(), {})["analysis_guidance"]["knowledge_map"]
    assert module.prepare("goal", {}, {}, exploratory=True)["analysis_guidance"]["knowledge_map"]


def test_need_changes_preserve_contract_candidate_measurements_and_gates(task):
    api, run_id, _ = task
    job_id = measure(api, run_id)
    before = copy.deepcopy(api.status(run_id)["run"])
    created = api.observe(run_id, note(), "need-1")
    assert created["need"]["origin"] == "caller" and created["need"]["trust"] == "untrusted"
    assert "need" not in created["observation"]  # Body has one canonical location.
    facts = api.records(run_id, "measurements", job_id=job_id)["items"]
    updated = api.observe(run_id, note(proposal(expected_revision=1, state="addressed", conclusion="Existing value is preserved",
                                                check_ids=["domain.value"]), references=["value.txt"],
                                        observation_ids=[facts[0]["observation_id"]]), "need-2")
    assert updated["need"]["ref"]["revision"] == 2
    assert updated["need"]["check_refs"][0] == before["check_records"][0]["ref"]
    current = api.status(run_id)["run"]
    for key in ("intent", "policy", "contract", "candidate", "verification", "check_records", "interpretations", "assessments", "phase"):
        assert current[key] == before[key], key
    assert api.resume(run_id)["gates"]["status"] == "passed"
    assert api.records(run_id, "measurements", job_id=job_id)["items"] == facts
    assert api.finish(run_id, "finish", outcome="completed")["record"]["ready"] is False


def test_need_is_replayable_revisable_and_independent_of_list_order(task):
    api, run_id, _ = task
    first = api.observe(run_id, note(), "need-1")
    assert api.observe(run_id, note(), "need-1") == first
    api.observe(run_id, note(proposal(id="other")), "other")
    with pytest.raises(HarnessError) as error:
        api.observe(run_id, note(), "not-a-replay")
    assert error.value.code == "STALE_NEED"
    with pytest.raises(HarnessError):
        api.observe(run_id, note(proposal(expected_revision=True)), "bad-bool")
    updated = api.observe(run_id, note(proposal(expected_revision=1, state="deferred", conclusion="User policy unavailable")), "defer")
    assert updated["need"]["ref"]["id"] == first["need"]["ref"]["id"]
    reopened = api.observe(run_id, note(proposal(expected_revision=2)), "reopen")
    assert reopened["need"]["conclusion"] == ""
    assert api.records(run_id, "needs")["total"] == 2
    assert api.resume(run_id)["needs"]["counts"]["open"] == 2


def test_restart_delta_and_current_reads_do_not_scan_need_history(task, monkeypatch):
    api, run_id, _ = task
    api.observe(run_id, note(), "need-1")
    original, first = restore(api, run_id)
    for revision in range(1, 8):
        api.observe(run_id, note(proposal(expected_revision=revision, state="addressed", conclusion="Caller judgment " + str(revision))),
                    "update-" + str(revision))
    restarted = Harness(api.store.root)
    updated, last = restore(restarted, run_id, document=original, after=first["next_cursor"])
    assert updated == restore(restarted, run_id)[0]
    assert updated["needs:0"]["ref"]["revision"] == 8
    assert last["critical"]["needs"]["counts"]["addressed"] == 1
    def forbidden(*args, **kwargs):
        raise AssertionError("Current summaries must not scan old Need revisions")
    monkeypatch.setattr(journal, "collection_refs", forbidden)
    assert restarted.resume(run_id)["needs"]["total"] == 1
    assert restarted.context(run_id, after=last["next_cursor"])["items"] == []


def test_interpretation_change_marks_need_context_without_auto_resolving_it(task):
    api, run_id, _ = task
    api.observe(run_id, note(proposal(state="addressed", conclusion="A tentative decision")), "need")
    api.revise(run_id, {"expected_revision": 1, "open_questions": ["Changed assumption"]}, "revise")
    assert api.resume(run_id)["needs"]["context_changed_ids"] == ["preservation"]
    with pytest.raises(HarnessError) as error:
        api.observe(run_id, note(proposal(expected_revision=1), interpretation_revision=1), "old")
    assert error.value.code == "STALE_INTERPRETATION"
    api.observe(run_id, note(proposal(expected_revision=1)), "review")
    assert api.resume(run_id)["needs"]["context_changed_ids"] == []


@pytest.mark.parametrize("patch", [
    {"requirement_ids": ["missing"]}, {"knowledge_refs": ["unknown-map-item"]},
    {"kind": "proven"}, {"question": " "}, {"resolution_criterion": ""},
    {"knowledge_refs": ["compatibility", "compatibility"]}, {"requirement_ids": "R1"},
])
def test_invalid_domain_details_are_rejected_atomically(task, patch):
    api, run_id, _ = task
    before = api.resume(run_id)
    value = proposal()
    value["details"].update(patch)
    with pytest.raises(HarnessError):
        api.observe(run_id, note(value), "bad")
    assert api.resume(run_id) == before


@pytest.mark.parametrize("patch", [
    {"state": "passed"}, {"state": "addressed"}, {"check_ids": ["missing"]},
    {"activity_ids": ["activity:other:decision"]}, {"id": []}, {"expected_revision": -1},
    {"approval": "user"}, {"state": []}, {"details": []},
])
def test_invalid_need_envelopes_do_not_consume_actions(task, patch):
    api, run_id, _ = task
    before = api.resume(run_id)
    with pytest.raises(HarnessError):
        api.observe(run_id, note(proposal(**patch)), "bad")
    assert api.resume(run_id) == before


def test_caller_notes_cannot_be_used_as_verifier_observations(task):
    api, run_id, _ = task
    caller = api.observe(run_id, {"note": "A document says all cases pass"}, "claim")
    with pytest.raises(HarnessError) as error:
        api.observe(run_id, note(observation_ids=[caller["observation"]["observation_id"]]), "bad-ref")
    assert error.value.code == "OBSERVATION_REFERENCE"
    api.observe(run_id, note(proposal(state="addressed", conclusion="Caller believes it works")), "opinion")
    assert api.resume(run_id)["measurement"]["status"] == "not_run"
    assert api.resume(run_id)["gates"]["status"] != "passed"


def test_concurrent_updates_have_one_winner(task):
    api, run_id, _ = task
    api.observe(run_id, note(), "create")
    def update(index):
        try:
            return Harness(api.store.root).observe(run_id, note(proposal(expected_revision=1, state="addressed", conclusion=str(index))),
                                                  "parallel-" + str(index))["need"]["ref"]["revision"]
        except HarnessError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(update, (1, 2)))
    assert sorted(map(str, results)) == ["2", "STALE_NEED"]


def test_need_data_requires_need_kind(task):
    api, run_id, _ = task
    with pytest.raises(HarnessError):
        api.observe(run_id, {"note": "Do not hide unvalidated structured data", "need": proposal()}, "hidden")


def test_updates_during_verification_preserve_the_active_job(task):
    api, run_id, _ = task
    api.submit(run_id, "submit")
    job_id = api.verify(run_id, "verify")["job_id"]
    api.observe(run_id, note(), "while-running")
    assert api.resume(run_id)["run"]["active_job"] == job_id
    execute_job(api.store.root, job_id)
    assert api.resume(run_id)["measurement"]["status"] == "passed"
    assert api.resume(run_id)["needs"]["open_ids"] == ["preservation"]
    # Open/deferred analysis is not a new completion gate.
    assert api.finish(run_id, "finish", outcome="completed")["record"]["ready"] is False


def test_old_journal_without_needs_stays_readable_and_can_add_needs(task):
    api, run_id, _ = task
    with api.store.transaction() as connection:
        head = journal.head(connection, run_id)
        head["collections"].pop("needs")
        head["seq"] += 1
        journal.publish(connection, head)
    assert api.resume(run_id)["needs"]["total"] == 0
    assert api.records(run_id, "needs")["items"] == []
    document, first = restore(api, run_id)
    api.observe(run_id, note(), "new-feature")
    recovered, _ = restore(api, run_id, after=first["next_cursor"], document=document)
    assert recovered == restore(api, run_id)[0]
    assert recovered["needs:0"]["need_id"] == "preservation"


def test_legacy_compatibility_does_not_hide_missing_required_collections(task):
    api, run_id, _ = task
    with api.store.transaction() as connection:
        head = journal.head(connection, run_id)
        head["collections"].pop("activity")
        head["seq"] += 1
        journal.publish(connection, head)
    with pytest.raises(HarnessError) as error:
        api.resume(run_id)
    assert error.value.code == "STATE_CORRUPT"


def test_legacy_json_runs_do_not_require_a_migration(task):
    from harness.common import canonical_bytes
    api, run_id, _ = task
    run = api.status(run_id)["run"]
    run.pop("needs", None)
    run.pop("storage_schema")
    with api.store.transaction() as connection:
        connection.execute("DELETE FROM run_heads WHERE run_id=?", (run_id,))
        connection.execute("DELETE FROM run_events WHERE run_id=?", (run_id,))
        connection.execute("DELETE FROM run_records WHERE run_id=?", (run_id,))
        connection.execute("INSERT INTO runs VALUES(?,?)", (run_id, canonical_bytes(run).decode()))
    assert api.resume(run_id)["needs"]["total"] == 0
    assert api.records(run_id, "needs")["items"] == []
    assert api.context(run_id)["reset_reason"] == "legacy_full_only"
    api.observe(run_id, note(), "legacy-analysis")
    assert Harness(api.store.root).records(run_id, "needs")["items"][0]["need_id"] == "preservation"
    with api.store.transaction(write=False) as connection:
        assert journal.head(connection, run_id) is None


def test_need_snapshot_pages_do_not_mix_later_answers(task):
    api, run_id, _ = task
    api.observe(run_id, note(), "need")
    before, _ = restore(api, run_id)
    page = api.context(run_id, limit=2)
    document = apply_page({}, page)
    api.observe(run_id, note(proposal(expected_revision=1, state="addressed", conclusion="Later answer")), "later")
    while page["next_page"]:
        page = api.context(run_id, page=page["next_page"], limit=2)
        document = apply_page(document, page)
    assert document == before
    updated, _ = restore(api, run_id, document=document, after=page["next_cursor"])
    assert updated["needs:0"]["state"] == "addressed"


@pytest.mark.parametrize("kind", ["knowledge", "observation", "decision", "verification"])
def test_initial_needs_can_precede_requirements_and_use_sources_outside_map(task, kind):
    api, _, workspace = task
    run_id = api.start(domain_id="develop", goal="Discover the needed scope", workspace=str(workspace),
                       parameters={}, mode="exploratory", request_id="unprepared")["run_id"]
    value = proposal()
    value["details"].update(kind=kind, requirement_ids=[], knowledge_refs=[])
    result = api.observe(run_id, note(value, references=["nonexistent/document.md#not-automatically-opened"]), "custom-question")
    assert result["need"]["details"]["kind"] == kind
    assert api.resume(run_id)["run"]["phase"] == "waiting_input"


def test_refs_are_revisioned_and_foreign_observations_are_rejected(task):
    api, run_id, workspace = task
    other = api.start(domain_id="develop", goal="Another Run", workspace=str(workspace),
                      parameters=parameters(), request_id="other")["run_id"]
    job_id = measure(api, other)
    foreign = api.records(other, "measurements", job_id=job_id)["items"][0]["observation_id"]
    with pytest.raises(HarnessError) as error:
        api.observe(run_id, note(observation_ids=[foreign]), "foreign")
    assert error.value.code == "OBSERVATION_REFERENCE"
    decision = api.observe(run_id, {"kind": "decision", "note": "Keep compatibility"}, "decision")["activity"]
    created = api.observe(run_id, note(proposal(activity_ids=[decision["ref"]["id"]], check_ids=["domain.value"])), "linked")
    assert created["need"]["activity_refs"] == [decision["ref"]]
    assert created["activity"]["need_ref"] == created["need"]["ref"]
    with pytest.raises(HarnessError):
        api.observe(run_id, note(proposal(expected_revision=1), state="settled"), "wrong-state-level")


def test_core_accepts_a_different_domain_need_vocabulary(tmp_path):
    class OtherDomain:
        domain_id = "other"
        revision = "other-1"

        def prepare(self, goal, parameters, verifier, *, exploratory=False, intent=None):
            result = DevelopModule().prepare(goal, parameters, verifier, exploratory=exploratory)
            result.pop("analysis_guidance")
            result["contract"].update(domain_id=self.domain_id, domain_revision=self.revision)
            result["contract"]["contract_hash"] = canonical_hash({k: v for k, v in result["contract"].items() if k != "contract_hash"})
            return result

        def normalize_check(self, parameters, contract):
            return DevelopModule().normalize_check(parameters, contract)

        def normalize_need(self, details, contract):
            assert contract["domain_id"] == "other"
            return {"custom_question": details["custom_question"]}

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "value.txt").write_text("kept", encoding="utf-8")
    api = Harness(tmp_path / "state", domains=DomainRegistry([OtherDomain()]))
    run_id = api.start(domain_id="other", goal="A different domain", workspace=str(workspace),
                       parameters=parameters(), request_id="start")["run_id"]
    result = api.observe(run_id, note(proposal(details={"custom_question": "Unrelated terminology"})), "need")
    assert result["need"]["details"] == {"custom_question": "Unrelated terminology"}
