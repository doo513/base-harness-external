"""Regressions found while using Harness as an external agent tool."""
from pathlib import Path

import pytest

from harness_external.errors import HarnessError
from harness_external.service import Harness
from harness_external.worker import execute_job


@pytest.fixture
def case(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "value.txt").write_text("one")
    parameters = {"profile": "structural", "inputs": ["value.txt"], "artifacts": ["value.txt"],
                  "expectations": [{"id": "value", "path": "value.txt", "operator": "equals", "expected": "one"}]}
    api = Harness(tmp_path / "state")
    monkeypatch.setattr("harness_external.worker.spawn_worker", lambda *args: None)
    return api, workspace, parameters


def start(case, key, *, mode="strict", budget=None):
    api, workspace, parameters = case
    return api.start(domain_id="develop", goal="Exercise evidence lifecycle", workspace=str(workspace),
                     parameters=parameters, request_id=key, mode=mode, budget=budget)["run_id"]


def measure(api, run_id, key):
    api.submit(run_id, "submit-" + key)
    job_id = api.verify(run_id, "verify-" + key)["job_id"]
    execute_job(api.store.root, job_id)
    return job_id, api.records(run_id, "measurements", job_id=job_id)["items"]


def delete_measurement(api, observation_id):
    with api.store.transaction() as connection:
        connection.execute("DELETE FROM measurements WHERE observation_id=?", (observation_id,))


def test_default_job_summary_validates_observation_references(case):
    api, _, _ = case
    run_id = start(case, "summary")
    job_id, measurements = measure(api, run_id, "summary")
    delete_measurement(api, measurements[0]["observation_id"])
    with pytest.raises(HarnessError) as error:
        api.status(run_id, job_id, view="summary")
    assert error.value.code == "RESULT_BINDING"


def test_closed_resume_validates_referenced_measurements(case):
    api, _, _ = case
    run_id = start(case, "closed")
    job_id, measurements = measure(api, run_id, "closed")
    api.finish(run_id, "finish-closed", outcome="completed")
    delete_measurement(api, measurements[0]["observation_id"])
    with pytest.raises(HarnessError) as error:
        api.resume(run_id)
    assert error.value.code == "RESULT_BINDING"


def test_assessment_classifies_contextual_observation_bindings(case):
    api, workspace, _ = case
    run_id = start(case, "assessment", mode="exploratory")
    _, measurements = measure(api, run_id, "old")
    old = measurements[0]
    workspace.joinpath("value.txt").write_text("two")
    current = api.submit(run_id, "submit-current")["candidate"]["subject"]
    assessment = api.assess(run_id, {"interpretation_revision": 1, "status": "satisfied",
        "summary": "The caller intentionally cites an older Candidate", "uncertainties": [],
        "cited_observation_ids": [old["observation_id"]]}, "assessment-contextual")["assessment"]
    assert assessment["subject"] == current
    assert assessment["citation_summary"] == {"current": 0, "contextual": 1}
    assert assessment["citation_bindings"] == [{
        "observation_id": old["observation_id"], "subject": old["subject"],
        "interpretation_ref": old["interpretation_ref"], "check_set_hash": old["check_set_hash"],
        "current_subject": False, "current_interpretation": True,
        "current_check_set": True, "relevance": "contextual",
    }]


def test_persisted_budget_transition_replays_the_same_error(case):
    api, _, _ = case
    run_id = start(case, "budget", budget={"max_actions": 1})
    api.observe(run_id, {"note": "Consume the only action"}, "one")
    codes = []
    for attempt in range(2):
        caller = api if attempt == 0 else Harness(api.store.root)
        with pytest.raises(HarnessError) as error:
            caller.submit(run_id, "same-over-budget")
        codes.append(error.value.code)
    assert codes == ["ACTION_BUDGET_EXHAUSTED", "ACTION_BUDGET_EXHAUSTED"]
