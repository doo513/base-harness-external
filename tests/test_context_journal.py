import copy
import json

import pytest

from harness.common import canonical_hash
from harness_external import journal
from harness_external.context import apply_page, encode
from harness_external.dispatch import invoke
from harness_external.errors import HarnessError
from harness_external.service import Harness
from harness_external.worker import execute_job


@pytest.fixture
def task(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "value.txt").write_text("ready")
    api = Harness(tmp_path / "state")
    monkeypatch.setattr("harness_external.worker.spawn_worker", lambda *args: None)
    run_id = api.start(domain_id="develop", goal="Preserve the original goal. " * 500, workspace=str(workspace),
                       parameters={"profile": "structural", "inputs": ["value.txt"], "artifacts": ["value.txt"],
                                   "expectations": [{"id": "value", "path": "value.txt", "operator": "equals", "expected": "ready"}]},
                       request_id="start", mode="exploratory", required_checks=["domain.value"],
                       constraints=["Do not lose uncertainty"])["run_id"]
    return api, run_id, workspace


def transfer(api, run_id, *, after=None, document=None, limit=20):
    result = {} if document is None else copy.deepcopy(document)
    page = api.context(run_id, after=after, limit=limit)
    size = 0
    while True:
        size += len(json.dumps(page).encode())
        result = apply_page(result, page)
        if page["next_page"] is None:
            return result, page, size
        assert page["next_cursor"] is None
        page = api.context(run_id, page=page["next_page"], limit=limit)


def test_full_then_delta_is_lossless_and_uncertainty_is_not_hidden(task):
    api, run_id, _ = task
    original, first, full_size = transfer(api, run_id)
    delta, unchanged, size = transfer(api, run_id, after=first["next_cursor"], document=original)
    assert delta == original and unchanged["items"] == []
    assert size < full_size * .2
    assert unchanged["critical"]["gate_status"] == "unsatisfied"
    decision = api.observe(run_id, {"kind": "decision", "record_id": "choice", "note": "Try one method, not an established fact"}, "decision")
    api.revise(run_id, {"expected_revision": 1, "goal_summary": "A revisable interpretation", "open_questions": ["Unknown edge case"],
                        "activity_ids": [decision["activity"]["ref"]["id"]]}, "revise")
    api.assess(run_id, {"status": "partial", "summary": "Not complete", "uncertainties": ["Unmeasured behavior"],
                        "cited_observation_ids": [], "interpretation_revision": 2}, "assess")
    updated, last, _ = transfer(api, run_id, after=first["next_cursor"], document=original)
    current, _, _ = transfer(api, run_id)
    assert updated == current
    assert last["critical"]["open_question_count"] == 1 and last["critical"]["uncertainty_count"] == 1
    assert updated["field:intent"]["constraints"] == ["Do not lose uncertainty"]


def test_pages_pin_the_old_snapshot_while_new_records_arrive(task):
    api, run_id, _ = task
    before, _, _ = transfer(api, run_id)
    page = api.context(run_id, limit=2)
    fixed = page["snapshot"]
    document = apply_page({}, page)
    api.observe(run_id, {"note": "Arrived during pagination"}, "later")
    while page["next_page"]:
        page = api.context(run_id, page=page["next_page"], limit=2)
        assert page["snapshot"] == fixed
        document = apply_page(document, page)
    assert document == before
    advanced, _, _ = transfer(api, run_id, after=page["next_cursor"], document=document)
    assert advanced == transfer(api, run_id)[0]


def test_invalid_or_foreign_cursor_resets_to_requested_run(task):
    api, run_id, _ = task
    for token in ("garbage", encode({"version": "other", "run_id": run_id}),
                  encode({"version": "harness-context-v1", "run_id": "run_" + "0" * 32, "seq": 1, "hash": "0" * 64})):
        first = api.context(run_id, after=token)
        assert first["delivery"] == "full" and first["reset_reason"] == "invalid_cursor"
        assert first["run_id"] == run_id


def test_current_reads_do_not_hydrate_history_and_records_are_immutable(task, monkeypatch):
    api, run_id, _ = task
    with api.store.transaction(write=False) as connection:
        original = dict(connection.execute("SELECT ref,data FROM run_records WHERE run_id=?", (run_id,)))
    for index in range(12):
        api.observe(run_id, {"kind": "decision", "note": "Reason " + str(index)}, "note-" + str(index))
    with api.store.transaction(write=False) as connection:
        current = dict(connection.execute("SELECT ref,data FROM run_records WHERE run_id=?", (run_id,)))
        assert all(current[key] == data for key, data in original.items())
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
    _, complete, _ = transfer(api, run_id)
    def forbidden(*args, **kwargs):
        raise AssertionError("A current summary/unchanged delta must not scan history")
    monkeypatch.setattr(journal, "collection_refs", forbidden)
    assert api.resume(run_id)["history_counts"]["activity"] == 12
    assert api.context(run_id, after=complete["next_cursor"])["items"] == []


def test_guard_is_enforced_before_replay_without_consuming_an_action(task):
    api, run_id, workspace = task
    state = api.resume(run_id)
    request = {"operation": "observe", "arguments": {"run_id": run_id, "data": {"note": "One action"}},
               "request_id": "guarded-note", "context": {"workspace": str(workspace), "domain": "develop",
                   "intent_ref": state["intent"]["ref"], "policy_ref": state["policy"]["ref"]}}
    result = invoke(api, request)
    assert invoke(api, request) == result
    usage = api.resume(run_id)["usage"]
    bad = copy.deepcopy(request)
    bad["context"]["intent_ref"]["sha256"] = "0" * 64
    with pytest.raises(HarnessError, match="Pinned intent"):
        invoke(api, bad)
    assert api.resume(run_id)["usage"] == usage


def test_measurements_are_in_deltas_not_repeated_in_the_journal(task):
    api, run_id, _ = task
    document, initial, _ = transfer(api, run_id)
    api.submit(run_id, "submit")
    job_id = api.verify(run_id, "verify")["job_id"]
    execute_job(api.store.root, job_id)
    updated, measured, _ = transfer(api, run_id, after=initial["next_cursor"], document=document)
    assert updated == transfer(api, run_id)[0]
    assert measured["critical"]["measurement_status"] == "passed"
    assert updated["measurements:0"]["origin"] == "verifier"
    with api.store.transaction(write=False) as connection:
        row = connection.execute("SELECT data FROM run_records WHERE run_id=? AND kind='measurements'", (run_id,)).fetchone()
        assert set(json.loads(row[0])) == {"observation_id", "job_id", "record_hash"}


def test_partial_checkpoint_replay_after_loss_between_submit_and_verify(task, monkeypatch):
    api, run_id, workspace = task
    request = {"operation": "checkpoint", "arguments": {"run_id": run_id, "wait_seconds": 0}, "request_id": "change",
               "context": {"workspace": str(workspace), "domain": "develop"}}
    original = api.verify
    def lose(*args, **kwargs):
        raise OSError("response/process lost after submit")
    monkeypatch.setattr(api, "verify", lose)
    with pytest.raises(OSError):
        invoke(api, request)
    first = api.resume(run_id)["candidate"]["candidate_hash"]
    (workspace / "value.txt").write_text("later edit")
    monkeypatch.setattr(api, "verify", original)
    resumed = invoke(api, request)
    assert resumed["submitted_candidate_hash"] == first
    assert api.resume(run_id)["history_counts"]["jobs"] == 1


def test_context_corruption_is_not_hidden_by_a_cursor_reset(task):
    api, run_id, _ = task
    with api.store.transaction() as connection:
        connection.execute("UPDATE run_heads SET digest=? WHERE run_id=?", ("0" * 64, run_id))
    with pytest.raises(HarnessError):
        api.context(run_id, after="invalid")


def test_current_status_loads_run_once_and_checks_context_on_replay(task, monkeypatch):
    api, run_id, workspace = task
    original = api.store.run
    calls = []
    def counted(*args, **kwargs):
        calls.append(kwargs.get("current", False))
        return original(*args, **kwargs)
    monkeypatch.setattr(api.store, "run", counted)
    result = invoke(api, {"operation": "status", "arguments": {"run_id": run_id},
                          "context": {"workspace": str(workspace), "domain": "develop"}})
    assert result["run"]["run_id"] == run_id and calls == [True]


def test_task_changes_and_decision_relations_survive_restart(task):
    api, run_id, _ = task
    api.observe(run_id, {"kind": "task", "task_id": "inspect", "note": "Inspect uncertainty"}, "task")
    old, first, _ = transfer(api, run_id)
    hypothesis = api.observe(run_id, {"kind": "hypothesis", "note": "Hypothesis, not a fact"}, "hypothesis")["activity"]
    api.observe(run_id, {"kind": "decision", "note": "Contrary observation needs review", "relation": "refutes",
                         "related_activity_id": hypothesis["ref"]["id"]}, "refutes")
    api.observe(run_id, {"kind": "task", "task_id": "inspect", "task_revision": 1, "state": "blocked", "note": "Still uncertain"}, "blocked")
    restarted = Harness(api.store.root)
    advanced, page, _ = transfer(restarted, run_id, after=first["next_cursor"], document=old)
    assert advanced == transfer(restarted, run_id)[0]
    assert advanced["logical_tasks:0"]["revision"] == 2 and advanced["logical_tasks:0"]["state"] == "blocked"
    assert "inspect" in page["critical"]["unresolved_task_ids"]
    assert any(value.get("relation") == "refutes" for key, value in advanced.items() if key.startswith("activity:"))


def test_explicit_runtime_transition_preserves_predecessor_but_not_evidence(task):
    api, run_id, workspace = task
    api.submit(run_id, "submit")
    job_id = api.verify(run_id, "verify")["job_id"]
    execute_job(api.store.root, job_id)
    old = api.status(run_id)["run"]
    record = api.finish(run_id, "partial", outcome="partial")["record"]
    next_run = api.start(domain_id="develop", goal=old["goal"], workspace=str(workspace),
                         parameters=old["interpretations"][-1]["parameters"], mode="exploratory",
                         constraints=old["intent"]["constraints"], request_id="transition", predecessor_run_id=run_id,
                         continuation_reason="Use the new execution version")["run_id"]
    state = api.status(next_run)["run"]
    assert state["predecessor"]["run_id"] == run_id and state["predecessor"]["kind"] == "runtime_transition"
    assert state["verification"]["status"] == "not_run" and state["candidate"] is None
    assert api.status(run_id)["run"]["record"] == record


def test_bad_page_position_requests_a_full_restore(task):
    api, run_id, _ = task
    first = api.context(run_id)
    token = encode({"target": first["snapshot"], "base": None, "offset": 999999})
    restored = api.context(run_id, page=token)
    assert restored["delivery"] == "full" and restored["offset"] == 0
    assert restored["reset_reason"] == "invalid_page"


def test_heartbeat_does_not_advance_context_cursor(task):
    api, run_id, _ = task
    api.submit(run_id, "submit")
    job_id = api.verify(run_id, "verify")["job_id"]
    _, first, _ = transfer(api, run_id)
    with api.store.transaction() as connection:
        job = api.store.job(connection, job_id)
        job["heartbeat_at"] += 1
        api.store.save_job(connection, job)
    delta = api.context(run_id, after=first["next_cursor"])
    assert delta["items"] == [] and delta["next_cursor"] == first["next_cursor"]


def test_transaction_current_cache_invalidates_after_any_write(task):
    api, run_id, _ = task
    with api.store.transaction() as connection:
        before = api.store.current(connection, run_id)
        changed = api.store.run(connection, run_id)
        changed["phase"] = "review"
        api.store.save_run(connection, changed)
        after = api.store.current(connection, run_id)
        assert after["phase"] == "review" and after["revision"] > before["revision"]


def test_two_authoritative_layouts_for_one_run_are_rejected(task):
    from harness.common import canonical_bytes
    api, run_id, _ = task
    with api.store.transaction() as connection:
        legacy = api.store.run(connection, run_id)
        connection.execute("INSERT INTO runs VALUES(?,?)", (run_id, canonical_bytes(legacy).decode()))
    with pytest.raises(HarnessError, match="conflicting"):
        api.resume(run_id)
