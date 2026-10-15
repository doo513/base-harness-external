"""Read views explain known preconditions without authorizing or executing work."""
from harness_external.worker import execute_job
from test_closeout_semantics import setup, begin


def test_operation_constraints_follow_state_without_mutating_it(setup):
    api, _, _ = setup
    run_id = begin(setup, mode="strict")
    before = api.status(run_id)["run"]
    view = api.resume(run_id, view="compact")
    conditions = view["operation_constraints"]
    assert conditions["meaning"] == "known_preconditions_not_authorization"
    assert conditions["operations"]["submit"]["blocked_by"] == []
    assert "SUBMIT_REQUIRED" in conditions["operations"]["verify"]["blocked_by"]
    assert "VERIFICATION_REQUIRED" in conditions["operations"]["finish_completed"]["blocked_by"]
    assert api.status(run_id)["run"] == before
    api.submit(run_id, "submit")
    job_id = api.verify(run_id, "verify")["job_id"]
    assert "VERIFICATION_ACTIVE" in api.resume(run_id)["operation_constraints"]["operations"]["verify"]["blocked_by"]
    execute_job(api.store.root, job_id)
    assert api.resume(run_id)["operation_constraints"]["operations"]["finish_completed"]["blocked_by"] == []
    api.finish(run_id, "finish", outcome="completed")
    closed = api.resume(run_id)["operation_constraints"]
    assert all("RUN_CLOSED" in item["blocked_by"] for item in closed["operations"].values())
