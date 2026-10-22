"""Core exception reads preserve evidence while reducing caller round trips."""
import base64
import copy
import io
import json
from time import perf_counter
from contextlib import redirect_stdout
from concurrent.futures import ThreadPoolExecutor

import pytest

from harness.common import canonical_hash
from harness_external import read_core, worker
from harness_external.errors import HarnessError
from harness_external.service import Harness
from test_skill_workflow import ROOT, helper
from test_client_cost import LIVE, observed


@pytest.fixture
def file_run(tmp_path, monkeypatch):
    api = Harness(tmp_path / "state")
    monkeypatch.setattr(worker, "spawn_worker", lambda *args: None)

    def create(name="main", *, checks=1, measured=True, requirements=None):
        workspace = tmp_path / name
        workspace.mkdir()
        paths = [f"result-{index}.txt" for index in range(checks)]
        for path in paths:
            (workspace / path).write_text("unfinished", encoding="utf-8")
        parameters = {
            "profile": "structural", "inputs": paths, "artifacts": paths,
            "expectations": [{"id": f"result-{index}", "path": path,
                              "operator": "equals", "expected": "ready"}
                             for index, path in enumerate(paths)],
        }
        if requirements is not None:
            parameters["requirements"] = requirements
        run = api.start(domain_id="develop", goal="Make the declared output ready",
                        workspace=str(workspace), parameters=parameters,
                        required_checks=["domain.result-0"], mode="exploratory",
                        request_id=f"start-{name}")["run_id"]
        job = None
        if measured:
            api.submit(run, "submit")
            job = api.verify(run, "verify")["job_id"]
            worker.execute_job(api.store.root, job)
            assert api.status(run, job)["job"]["result"]["status"] == "failed"
        return api, workspace, run, job

    return create


def test_attention_uses_actual_file_measurements_without_changing_the_run(file_run):
    api, _, run, job = file_run()
    before = api.status(run)["run"]
    result = api.resume(run, view="attention")
    assert result["schema_version"] == "harness-attention-v1"
    assert result["view"] == "attention" and result["ready"] is False
    assert result["selected_job"]["job_id"] == job
    assert result["selected_job"]["relevance"] == "current"
    assert result["selected_job"]["is_current_verification"] is True
    failures = [item for item in result["attention"] if item["kind"] == "check"]
    assert len(failures) == 1
    assert failures[0]["comparison_status"] == "failed"
    assert failures[0]["scope"] == "current"
    measured = api.records(run, "measurements", job_id=job)["items"]
    assert failures[0]["observation_id"] == measured[0]["observation_id"]
    raw = api.records(run, **failures[0]["detail"])
    assert raw["next_offset"] is None
    assert json.loads(base64.b64decode(raw["data"])) == measured[0]
    assert result["gates"]["unsatisfied_check_ids"] == ["domain.result-0"]
    assert result["operation_constraints"]["operations"]["finish_completed"]["blocked_by"] == ["VERIFICATION_REQUIRED"]
    assert result["budget_remaining"]["verifications"] == before["limits"]["max_verifications"] - 1
    assert api.status(run)["run"] == before


def test_current_job_is_loaded_directly_even_outside_recent_five(file_run, monkeypatch):
    api, _, run, job = file_run()
    with api.store.transaction() as connection:
        original = api.store.job(connection, job)
        for index in range(7):
            historical = copy.deepcopy(original)
            historical.update(job_id=f"job_{index:032x}", status="cancelled", result=None)
            api.store.save_job(connection, historical)
    assert job not in {item["job_id"] for item in api.resume(run)["recent_jobs"]}
    monkeypatch.setattr(read_core, "summary", lambda *args: pytest.fail("Attention must not scan recent Jobs"))
    result = api.resume(run, view="attention")
    assert result["selected_job"]["job_id"] == job
    assert result["selected_job"]["is_current_verification"] is True
    assert any(item.get("comparison_status") == "failed" for item in result["attention"])


def test_resubmission_does_not_present_previous_failure_as_current(file_run):
    api, workspace, run, previous = file_run()
    (workspace / "result-0.txt").write_text("ready", encoding="utf-8")
    api.submit(run, "new-candidate")
    current = api.resume(run, view="attention")
    assert current["measurement"]["status"] == "not_run"
    assert current["selected_job"] is None
    assert not any(item["kind"] in {"check", "case", "declared_scenario"} for item in current["attention"])
    historical = api.status(run, previous, view="attention")
    assert historical["selected_job"]["relevance"] == "historical"
    assert historical["selected_job"]["is_current_verification"] is False
    assert historical["measurement"]["status"] == "not_run"
    assert all(item["scope"] == "historical" for item in historical["attention"] if item["kind"] == "check")
    assert historical["gates"]["status"] != "passed"


def test_historical_attention_workspace_comparison_uses_the_selected_job(file_run):
    api, workspace, run, previous = file_run()
    old_hash = api.status(run, previous, view="summary")["job"]["candidate_hash"]
    (workspace / "result-0.txt").write_text("ready", encoding="utf-8")
    current = api.submit(run, "new-candidate")["candidate"]
    assert current["candidate_hash"] != old_hash
    attention = api.status(run, previous, view="attention", check_workspace=True)
    summary = api.status(run, previous, view="summary", check_workspace=True)
    assert attention["workspace_comparison_candidate_hash"] == summary["workspace_comparison_candidate_hash"] == old_hash
    assert attention["current_workspace_matches_submitted_files"] is False
    assert summary["current_workspace_matches_submitted_files"] is False
    assert api.status(run, view="attention", check_workspace=True)["current_workspace_matches_submitted_files"] is True


def test_client_reuses_only_an_attention_response_with_the_requested_limit(file_run, tmp_path, monkeypatch):
    api, _, run, _ = file_run(measured=False)
    result = api.resume(run, view="attention", attention_limit=20)
    assert result["attention_limit"] == 20
    config = helper.configure(tmp_path / "client.json", str(ROOT), str(api.store.root), ephemeral=True)["config"]
    client = helper.Client(config)
    monkeypatch.setattr(client, "call", lambda *args: pytest.fail("Existing attention is returned or rejected without another Core call"))
    binding = {"run_id": run}
    assert client.attention(result, binding, limit=20) is result
    with pytest.raises(helper.ClientError) as error:
        client.attention(result, binding, limit=1)
    assert error.value.code == "ATTENTION_ARGUMENTS"


def test_attention_joins_one_snapshot_while_another_caller_submits(file_run, monkeypatch):
    api, workspace, run, job = file_run()
    original_hash = api.resume(run)["candidate"]["candidate_hash"]
    (workspace / "result-0.txt").write_text("ready", encoding="utf-8")
    read_cases, submissions = api.store.cases, []

    def submit_between_reads(*args, **kwargs):
        if not submissions:
            with ThreadPoolExecutor(max_workers=1) as pool:
                submissions.append(pool.submit(api.submit, run, "concurrent-submit").result(timeout=20))
        return read_cases(*args, **kwargs)

    monkeypatch.setattr(api.store, "cases", submit_between_reads)
    result = api.resume(run, view="attention")
    assert submissions[0]["candidate"]["candidate_hash"] != original_hash
    assert result["candidate"]["candidate_hash"] == original_hash
    assert result["selected_job"]["job_id"] == job
    assert result["selected_job"]["is_current_verification"] is True
    assert api.resume(run, view="attention")["selected_job"] is None


def test_attention_is_bounded_and_its_full_job_reference_resolves(file_run):
    api, _, run, job = file_run(checks=4)
    result = api.resume(run, view="attention", attention_limit=1)
    assert len(result["attention"]) == result["listed_attention_count"] == 1
    assert result["attention"][0]["kind"] == "verification_gap"
    assert result["total_known_attention_count"] == 5  # Four failures and the undeclared-requirements gap.
    assert result["omitted_attention_count"] == 4
    assert result["attention_page_complete"] is False
    reference = result["job_detail"]
    detail = api.records(run, **{**reference, "detail": {**reference["detail"], "limit": 16384}})
    assert detail["next_offset"] is None
    restored = json.loads(base64.b64decode(detail["data"]))
    assert restored["job_id"] == job
    assert len(restored["result"]["measurement_scope"]["observations"]) == 4
    assert canonical_hash(restored) == reference["detail"]["record_digest"]


def test_no_job_keeps_current_declaration_gaps_and_completion_constraints(file_run):
    requirement = {"id": "preserve", "statement": "Existing outputs must remain intact", "check_ids": []}
    api, _, run, _ = file_run(measured=False, requirements=[requirement])
    result = api.resume(run, view="attention")
    assert result["selected_job"] is None
    assert result["measurement"]["status"] == "not_run"
    gaps = [item for item in result["attention"] if item["kind"] == "verification_gap"]
    assert gaps and all(item["scope"] == "current_declaration" for item in gaps)
    assert result["verification_scope"]["coverage_conclusion"] == "not_established"
    assert result["attention_page_complete"] is False
    assert "SUBMIT_REQUIRED" in result["operation_constraints"]["operations"]["verify"]["blocked_by"]


def test_attention_rejects_foreign_jobs(file_run):
    api, _, first, _ = file_run("first")
    _, _, _, foreign_job = file_run("other")
    with pytest.raises(HarnessError) as error:
        api.status(first, foreign_job, view="attention")
    assert error.value.code == "JOB_RUN_MISMATCH"


def test_explicit_diagnostic_job_never_becomes_current_verification(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "app.txt").write_text("candidate", encoding="utf-8")
    (workspace / "probe.py").write_text("print('probe')\n", encoding="utf-8")
    api = Harness(tmp_path / "state")
    monkeypatch.setattr(worker, "spawn_worker", lambda *args: None)
    run = api.start(domain_id="develop", goal="Inspect a comparison", workspace=str(workspace),
                    mode="exploratory", request_id="start", capture_baseline=True,
                    parameters={"profile": "execution", "inputs": ["app.txt", "probe.py"], "artifacts": ["app.txt"],
                                "expectations": [{"path": "app.txt", "operator": "equals", "expected": "candidate"}],
                                "execution_checks": [{"id": "probe", "kind": "command", "argv": ["python3", "probe.py"]}]})["run_id"]
    candidate = api.submit(run, "submit")["candidate"]
    job = api.compare(run, "diagnostic", expected_candidate_hash=candidate["candidate_hash"],
                      parameters={"check_ids": ["domain.probe"], "test_paths": ["probe.py"]})["job_id"]
    api.cancel(run, job, "cancel-diagnostic")
    result = api.status(run, job, view="attention")
    assert result["selected_job"]["relevance"] == "diagnostic"
    assert result["selected_job"]["is_current_verification"] is False
    assert result["measurement"]["status"] == "not_run"
    assert api.resume(run, view="attention")["selected_job"] is None


def test_attention_does_not_waive_completion_record_integrity(file_run):
    api, _, run, _ = file_run()
    api.finish(run, "finish", outcome="partial", summary="One output still fails")
    with api.store.transaction() as connection:
        state = api.store.run(connection, run)
        state["record"]["summary"] = "Unverified replacement"
        api.store.save_run(connection, state)
    with pytest.raises(HarnessError) as error:
        api.resume(run, view="attention")
    assert error.value.code == "RECORD_CORRUPT"


def test_attention_rejects_tampered_measurement_before_filtering(file_run):
    api, _, run, _ = file_run()
    with api.store.transaction() as connection:
        row = connection.execute("SELECT observation_id,data FROM measurements WHERE run_id=?", (run,)).fetchone()
        damaged = json.loads(row[1])
        damaged["comparison_status"] = "passed"
        connection.execute("UPDATE measurements SET data=? WHERE observation_id=?", (json.dumps(damaged), row[0]))
    with pytest.raises(HarnessError):
        api.resume(run, view="attention")


@pytest.mark.parametrize("limit", [0, 101, True, "1"])
def test_attention_limit_rejects_unbounded_or_ambiguous_inputs(file_run, limit):
    api, _, run, _ = file_run(measured=False)
    with pytest.raises(HarnessError):
        api.resume(run, view="attention", attention_limit=limit)


def test_helper_attention_cli_makes_one_core_call_and_transfers_fewer_bytes(file_run, tmp_path, monkeypatch):
    api, workspace, run, job = file_run(checks=4)
    config_path, binding_path = tmp_path / "client.json", tmp_path / "binding.json"
    config = helper.configure(config_path, str(ROOT), str(api.store.root), ephemeral=True)["config"]
    client = helper.Client(config)
    binding = client.bind(run, str(workspace), "develop", binding_path)["binding"]
    calls, responses = [], []
    original = helper.Client.execute

    def counted(self, operation, arguments, *args):
        calls.append((operation, copy.deepcopy(arguments)))
        result = original(self, operation, arguments, *args)
        responses.append(result)
        return result

    monkeypatch.setattr(helper.Client, "execute", counted)
    started = perf_counter()
    previous = [client.call_operation("resume", {"view": "compact"}, binding=binding),
                client.call_operation("status", {"job_id": job, "view": "summary"}, binding=binding)]
    old_ms = (perf_counter() - started) * 1000
    old_calls = len(calls)
    old_bytes = sum(len(helper.canonical(item)) for item in responses)
    assert [call[0] for call in calls] == ["invoke", "invoke"]
    calls.clear()
    responses.clear()
    started = perf_counter()
    with redirect_stdout(io.StringIO()) as output:
        exit_code = helper.main(["--config", str(config_path), "call", "--operation", "resume",
                                 "--binding", str(binding_path), "--format", "attention"])
    new_ms = (perf_counter() - started) * 1000
    result = json.loads(output.getvalue())
    assert exit_code == 0, result
    assert len(calls) == 1 and calls[0][0] == "invoke"
    assert calls[0][1]["request"]["arguments"]["view"] == "attention"
    assert result["schema_version"] == "harness-attention-v1"
    assert result["selected_job"]["job_id"] == job
    assert len([item for item in result["attention"] if item["kind"] == "check"]) == 4
    assert any(item["kind"] == "verification_gap" for item in result["attention"])
    old_failures = {(item["check_id"], item["observation_id"], item["comparison_status"])
                    for item in previous[1]["job"]["result"]["measurement_scope"]["observations"]}
    new_failures = {(item["check_id"], item["observation_id"], item["comparison_status"])
                    for item in result["attention"] if item["kind"] == "check"}
    assert new_failures == old_failures
    new_bytes = sum(len(helper.canonical(item)) for item in responses)
    assert old_calls == 2 and new_bytes < old_bytes
    print("TEST_METRIC " + json.dumps({"scenario": "file_check_attention", "old_calls": old_calls,
          "new_calls": len(calls), "old_response_bytes": old_bytes, "new_response_bytes": new_bytes,
          "old_elapsed_ms": round(old_ms, 3), "new_elapsed_ms": round(new_ms, 3),
          "matched_failure_observations": len(new_failures), "model_tokens_measured": False}, sort_keys=True))


@LIVE
def test_case_attention_preserves_real_error_references_and_reduces_three_reads(observed, monkeypatch):
    api, client, binding, job = observed
    run = binding["run_id"]
    before = api.status(run)["run"]
    calls = []
    original = client.execute

    def counted(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(client, "execute", counted)
    started = perf_counter()
    prior = [client.call_operation("resume", {"view": "compact"}, binding=binding),
             client.call_operation("status", {"job_id": job, "view": "summary"}, binding=binding),
             client.call_operation("records", {"kind": "cases", "job_id": job, "subject_role": "candidate",
                                               "outcomes": ["failed", "skipped"], "limit": 20}, binding=binding)]
    old_ms = (perf_counter() - started) * 1000
    old_calls = len(calls)
    old_bytes = sum(len(helper.canonical(item)) for item in prior)
    assert calls == ["invoke", "invoke", "invoke"]
    calls.clear()
    started = perf_counter()
    complete = client.call_operation("resume", {"view": "attention", "attention_limit": 20}, binding=binding)
    new_ms = (perf_counter() - started) * 1000
    assert calls == ["invoke"] and complete["ok"], complete
    old_cases = {(item["check_id"], item["observation_id"], item["case"]["case_id"], item["case"]["outcome"],
                  item["case"]["executed"], item["case"]["finished"]) for item in prior[2]["items"]}
    new_cases = {(item["check_id"], item["observation_id"], item["case_id"], item["status"], item["executed"], item["finished"])
                 for item in complete["attention"] if item["kind"] == "case"}
    assert new_cases == old_cases and {item[3] for item in new_cases} == {"failed", "skipped"}
    assert complete["attention_page_complete"] is True
    assert complete["case_query"]["next_offset"] is None
    new_bytes = len(helper.canonical(complete))
    assert old_calls == 3 and new_bytes < old_bytes
    print("TEST_METRIC " + json.dumps({"scenario": "testcase_attention", "old_calls": old_calls,
          "new_calls": len(calls), "old_response_bytes": old_bytes, "new_response_bytes": new_bytes,
          "old_elapsed_ms": round(old_ms, 3), "new_elapsed_ms": round(new_ms, 3),
          "matched_case_observations": len(new_cases), "model_tokens_measured": False}, sort_keys=True))

    # Pagination is an independent assertion, not part of the equal-evidence cost comparison.
    calls.clear()
    gap_count = len(api.resume(run)["verification_scope"]["gaps"])
    result = client.call_operation("resume", {"view": "attention", "attention_limit": gap_count + 1}, binding=binding)
    assert calls == ["invoke"] and result["ok"], result
    assert result["selected_job"]["is_current_verification"] is True
    assert result["case_query"]["next_offset"] == 1
    failed = next(item for item in result["attention"] if item["kind"] == "case")
    assert failed["kind"] == "case" and failed["status"] == "failed"
    assert failed["requirement_ids"] == ["value"]
    error = next(item for item in failed["errors"] if item.get("truncated"))
    restored = client.resolve_detail(binding, error["detail"])
    assert restored["value"].startswith(error["excerpt"])
    assert len(restored["value"]) > len(error["excerpt"])
    remaining = api.records(run, "cases", job_id=job, subject_role="candidate",
                            outcomes=result["case_query"]["outcomes"], offset=result["case_query"]["next_offset"])
    assert [item["case"]["outcome"] for item in remaining["items"]] == ["skipped"]
    assert api.status(run)["run"] == before
