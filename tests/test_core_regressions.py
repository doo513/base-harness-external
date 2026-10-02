"""Lifecycle and concurrency boundaries discovered in the 0.2.0 review."""
from concurrent.futures import ThreadPoolExecutor
import copy
import threading
import time

import pytest

from harness.common import canonical_hash
from harness_external.errors import HarnessError
from harness_external.service import Harness
from harness_external import service, worker


@pytest.fixture
def case(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "result.txt").write_text("result")
    parameters = {"profile": "structural", "inputs": ["result.txt"], "artifacts": ["result.txt"],
                  "expectations": [{"path": "result.txt", "operator": "equals", "expected": "result"}]}
    api = Harness(tmp_path / "state")
    monkeypatch.setattr(worker, "spawn_worker", lambda *a: None)
    def start(key="start", **kwargs):
        return api.start(domain_id="develop", goal="Inspect result", workspace=str(workspace),
                         parameters=kwargs.pop("parameters", copy.deepcopy(parameters)),
                         mode="exploratory", request_id=key, **kwargs)["run_id"]
    return api, start, parameters


def test_dynamic_check_timeout_covers_new_and_revised_commands(case, monkeypatch):
    api, start, _ = case
    clock = [time.time()]
    monkeypatch.setattr(service.time, "time", lambda: clock[0])
    def controlled_capture(check, *args):
        clock[0] += 80  # Elapsed execution, without a slow real process.
        return {"status": "completed", "capture": {
            "argv": check["argv"], "cwd": check["cwd"], "startedAt": "2026-10-01T00:00:00Z",
            "finishedAt": "2026-10-01T00:01:20Z", "execution": "completed",
            "stdout": "", "stderr": "", "exitCode": 0}}
    monkeypatch.setattr(worker, "execute_sandbox", controlled_capture)
    for revised in (False, True):
        run = start(str(revised), parameters={"inputs": ["result.txt"]})
        spec = {"kind": "command", "argv": ["python3", "-c", "pass"], "timeout_seconds": 1 if revised else 120}
        api.register_check(run, {"check_id": "slow", "interpretation_revision": 1, "parameters": spec}, "new")
        if revised:
            api.register_check(run, {"check_id": "slow", "interpretation_revision": 1, "expected_revision": 1,
                                     "parameters": {**spec, "timeout_seconds": 120}}, "revised")
        api.submit(run, "submit")
        job = api.verify(run, "verify")["job_id"]
        worker.execute_job(api.store.root, job)
        state = api.status(run, job)
        assert state["job"]["status"] == "completed"
        assert state["measurements"][0]["comparison_status"] == "passed"


def test_dynamic_timeout_remains_capped_by_run_deadline(case):
    api, start, _ = case
    run = start(parameters={"inputs": ["result.txt"]}, budget={"timeout_seconds": 10})
    api.register_check(run, {"check_id": "slow", "interpretation_revision": 1,
                            "parameters": {"kind": "command", "argv": ["python3", "-c", "pass"], "timeout_seconds": 120}}, "new")
    api.submit(run, "submit")
    job = api.verify(run, "verify")["job_id"]
    assert api.status(run, job)["job"]["deadline_at"] == api.status(run)["run"]["deadline_at"]


def test_waiting_input_never_masks_blocked_or_closed_state(case):
    api, start, _ = case
    run = start(parameters={})
    assert api.status(run)["closeout"]["lifecycle"] == "waiting_input"
    with api.store.transaction() as connection:
        saved = api.store.run(connection, run)
        saved["deadline_at"] = time.time() - 1
        api.store.save_run(connection, saved)
    status = api.status(run)
    assert status["run"]["phase"] == "handoff"
    assert status["closeout"]["lifecycle"] == "blocked"
    assert status["closeout"]["termination_reason"] == "DEADLINE_EXCEEDED"
    api.finish(run, "finish", outcome="partial")
    assert api.status(run)["closeout"]["lifecycle"] == "closed"
    limited = start("limited", parameters={}, budget={"max_actions": 1})
    api.observe(limited, {"note": "Investigating"}, "one")
    with pytest.raises(HarnessError, match="limit"):
        api.observe(limited, {"note": "Investigating"}, "two")
    assert api.status(limited)["closeout"]["lifecycle"] == "blocked"


def test_waiting_input_reports_active_verification(case):
    api, start, _ = case
    run = start(parameters={"inputs": ["result.txt"]})
    api.register_check(run, {"check_id": "probe", "interpretation_revision": 1,
                            "parameters": {"kind": "file", "path": "result.txt", "operator": "equals", "expected": "result"}}, "probe")
    api.submit(run, "submit")
    job = api.verify(run, "verify")["job_id"]
    assert api.status(run)["closeout"]["lifecycle"] == "verifying"
    worker.execute_job(api.store.root, job)
    assert api.status(run)["closeout"]["lifecycle"] == "waiting_input"


def test_question_patch_preserves_omitted_and_clears_explicit_questions(case):
    api, start, parameters = case
    run = start(parameters={})
    api.revise(run, {"expected_revision": 1, "open_questions": ["What remains uncertain?"]}, "question")
    api.revise(run, {"expected_revision": 2, "parameters": parameters}, "ready")
    status = api.status(run)["run"]
    assert not status["domain_questions"]
    assert status["interpretations"][-1]["open_questions"] == ["What remains uncertain?"]
    api.revise(run, {"expected_revision": 3, "open_questions": []}, "resolved")
    history = api.status(run)["run"]["interpretations"]
    assert history[-1]["open_questions"] == []
    assert history[-2]["open_questions"] == ["What remains uncertain?"]


def test_legacy_structured_domain_questions_do_not_break_revision(case):
    api, start, parameters = case
    run = start(parameters={})
    with api.store.transaction() as connection:
        saved = api.store.run(connection, run)
        interpretation = saved["interpretations"][-1]
        interpretation["open_questions"] = copy.deepcopy(saved["domain_questions"])
        interpretation["ref"]["sha256"] = canonical_hash({k: v for k, v in interpretation.items() if k != "ref"})
        api.store.save_run(connection, saved)
    api.revise(run, {"expected_revision": 1, "parameters": parameters}, "ready")
    status = api.status(run)["run"]
    assert not status["domain_questions"] and not status["interpretations"][-1]["open_questions"]
    assert status["interpretations"][0]["open_questions"]  # Historical record remains intact.


def test_exploratory_completion_honors_snapshot_without_forcing_assessment(case):
    api, start, _ = case
    run = start()
    with pytest.raises(HarnessError) as error:
        api.finish(run, "finish", outcome="completed")
    assert error.value.code == "SUBMIT_REQUIRED"
    candidate = api.submit(run, "submit")["candidate"]
    record = api.finish(run, "finish", outcome="completed")["record"]
    assert record["candidate_hash"] == candidate["candidate_hash"]
    assert record["measurement"]["status"] == "not_run"
    assert record["assessment"]["status"] == "not_assessed"
    assert record["gates"]["status"] == "not_required" and record["ready"] is False


def test_capture_allows_another_run_to_checkpoint(case, monkeypatch):
    api, start, _ = case
    slow, other = start("slow"), start("other")
    api.submit(other, "submit")
    job = api.verify(other, "verify")["job_id"]
    entered, release = threading.Event(), threading.Event()
    original = service.capture
    def blocked_capture(*args, **kwargs):
        entered.set()
        assert release.wait(8)
        return original(*args, **kwargs)
    monkeypatch.setattr(service, "capture", blocked_capture)
    with ThreadPoolExecutor(max_workers=2) as pool:
        submission = pool.submit(api.submit, slow, "submit")
        try:
            assert entered.wait(3)
            pool.submit(worker.execute_job, api.store.root, job).result(timeout=5)
            assert api.status(other, job)["measurements"][0]["comparison_status"] == "passed"
        finally:
            release.set()
        submission.result(timeout=5)
    assert api.status(slow)["run"]["actions"] == 1


@pytest.mark.parametrize("interference", ["revise", "finish", "deadline", "verify"])
def test_inflight_submit_cannot_overwrite_changed_run(case, monkeypatch, interference):
    api, start, _ = case
    run = start()
    initial = api.submit(run, "initial")["candidate"]
    original = service.capture
    def changed_capture(*args, **kwargs):
        result = original(*args, **kwargs)
        if interference == "revise":
            api.revise(run, {"expected_revision": 1, "goal_summary": "Changed interpretation"}, "revise")
        elif interference == "finish":
            api.finish(run, "finish", outcome="partial")
        elif interference == "verify":
            api.verify(run, "verify")
        else:
            with api.store.transaction() as connection:
                saved = api.store.run(connection, run)
                saved["deadline_at"] = time.time() - 1
                api.store.save_run(connection, saved)
        return result
    monkeypatch.setattr(service, "capture", changed_capture)
    with pytest.raises(HarnessError) as error:
        api.submit(run, "new")
    assert error.value.code in {"SUBMISSION_CONFLICT", "RUN_CLOSED", "DEADLINE_EXCEEDED"}
    status = api.status(run)["run"]
    assert status["candidate"] == initial and status["generation"] == 1
    assert sorted(p.name for p in api.store.directory(run).iterdir()) == [initial["candidate_id"]]


def test_concurrent_submit_replays_one_candidate_and_one_action(case, monkeypatch):
    api, start, _ = case
    run = start()
    barrier = threading.Barrier(2)
    original = service.capture
    def concurrent_capture(*args, **kwargs):
        result = original(*args, **kwargs)
        barrier.wait(timeout=5)
        return result
    monkeypatch.setattr(service, "capture", concurrent_capture)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: api.submit(run, "same-request"), range(2)))
    assert results[0] == results[1]
    state = api.status(run)["run"]
    assert state["generation"] == state["actions"] == 1
    assert sorted(p.name for p in api.store.directory(run).iterdir()) == [state["candidate"]["candidate_id"]]


def test_failed_submission_commit_cleans_unpublished_snapshot(case, monkeypatch):
    api, start, _ = case
    run = start()
    def fail(*args):
        raise RuntimeError("Controlled persistence failure")
    monkeypatch.setattr(api.store, "remember", fail)
    with pytest.raises(RuntimeError, match="persistence failure"):
        api.submit(run, "submit")
    assert api.status(run)["run"]["candidate"] is None
    assert list(api.store.directory(run).iterdir()) == []
