"""Review-driven identity, provenance, storage and tool-interface guarantees."""
import copy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from harness.common import canonical_bytes, canonical_hash
from harness_external.domain import DevelopModule
from harness_external.errors import HarnessError
from harness_external.registry import DomainRegistry
from harness_external.service import Harness
from harness_external.worker import execute_job


@pytest.fixture
def case(tmp_path, monkeypatch):
    ws = tmp_path / "workspace"
    ws.mkdir()
    for name in ("a", "b", "c"):
        (ws / (name + ".txt")).write_text(name)
    parameters = {"profile": "structural", "inputs": ["a.txt", "b.txt", "c.txt"], "artifacts": ["a.txt", "b.txt"],
                  "expectations": [{"path": name + ".txt", "operator": "equals", "expected": name} for name in ("a", "b")]}
    api = Harness(tmp_path / "state")
    monkeypatch.setattr("harness_external.worker.spawn_worker", lambda *a: None)
    def start(key="start", **args):
        return api.start(domain_id="develop", goal="Compare files", workspace=str(ws), request_id=key,
                         parameters=args.pop("parameters", parameters), mode=args.pop("mode", "exploratory"), **args)["run_id"]
    return api, start, parameters, ws


def measure(api, run, key="verify"):
    job = api.verify(run, key)["job_id"]
    execute_job(api.store.root, job)
    return api.status(run, job)


def test_reordering_and_insertion_preserve_check_identity_and_gate(case):
    api, start, params, _ = case
    run = start(required_checks=["file-0"])
    original = {c["check_id"]: c for c in api.records(run, "checks")["items"]}
    reordered = list(reversed(params["expectations"]))
    api.revise(run, {"expected_revision": 1, "parameters": {"expectations": reordered}}, "reorder")
    assert api.records(run, "checks")["items"] == list(original.values())
    inserted = [{"path": "c.txt", "operator": "equals", "expected": "c"}, *reordered]
    api.revise(run, {"expected_revision": 2, "parameters": {"expectations": inserted}}, "insert")
    current = api.records(run, "checks")["items"]
    assert current[:2] == list(original.values())
    assert current[2]["check_id"] == "file-2" and current[2]["spec"]["parameters"]["path"] == "c.txt"
    assert api.resume(run)["gates"]["results"][0]["check_ref"] == original["file-0"]["ref"]


def test_explicit_check_names_and_ambiguous_defaults(case):
    api, start, params, _ = case
    ambiguous = copy.deepcopy(params)
    ambiguous["expectations"].append({"path": "a.txt", "operator": "equals", "expected": "alternative"})
    with pytest.raises(HarnessError) as error:
        start("ambiguous", parameters=ambiguous)
    assert error.value.code == "CHECK_ID_AMBIGUOUS"
    for index, expectation in enumerate(ambiguous["expectations"]):
        expectation["id"] = "criterion-" + str(index)
    run = start(parameters=ambiguous, required_checks=["domain.criterion-0"])
    changed = copy.deepcopy(ambiguous["expectations"])
    changed[0]["expected"] = "changed criterion"
    with pytest.raises(HarnessError) as error:
        api.revise(run, {"expected_revision": 1, "parameters": {"expectations": changed}}, "change-required")
    assert error.value.code == "GATE_POLICY_CHANGED"


def test_deferred_gate_is_explicit_and_typos_are_rejected(case):
    api, start, _, _ = case
    with pytest.raises(HarnessError) as error:
        start("typo", required_checks=["file-typo"])
    assert error.value.code == "UNKNOWN_GATE_CHECK"
    run = start(deferred_checks=["future"])
    gate = api.resume(run)["gates"]["results"][0]
    assert gate["status"] == "not_defined" and gate["definition_status"] == "deferred"
    api.register_check(run, {"check_id": "future", "interpretation_revision": 1,
                            "parameters": {"kind": "file", "path": "a.txt", "operator": "equals", "expected": "a"}}, "define")
    api.submit(run, "submit")
    measure(api, run)
    assert api.finish(run, "finish", outcome="completed")["record"]["gates"]["status"] == "passed"


def test_policy_approval_claim_and_check_generator_stay_distinct(case):
    api, start, _, _ = case
    run = start(provenance={"declared_author": "user", "approval_reference": "host-message-123"})
    state = api.resume(run)
    source = state["policy"]["provenance"]
    assert source["declared_author"] == "user"
    assert source["approval"]["status"] == "unverified" and source["approval"]["authenticated_by"] is None
    generated = api.records(run, "checks")["items"][0]
    assert generated["origin"] == "domain_preparation" and generated["spec"]["author"] == "user"
    assert generated["generated_by"]["kind"] == "domain" and generated["trust"] == "untrusted"
    proposed = api.register_check(run, {"check_id": "probe", "interpretation_revision": 1, "provenance": {"declared_author": "model"},
                    "parameters": {"kind": "file", "path": "c.txt", "operator": "equals", "expected": "c"}}, "probe")["check"]
    assert proposed["origin"] == "caller_proposal" and proposed["spec"]["author"] == "model"
    assert proposed["generated_by"]["kind"] == "domain_normalization"
    with pytest.raises(HarnessError):
        start("forged", provenance={"declared_author": "user", "approved": True})


def test_unverified_closure_has_unambiguous_resolution(case):
    api, start, _, _ = case
    run = start()
    api.submit(run, "submit")
    closed = api.finish(run, "finish", outcome="completed")
    assert closed["record"]["outcome"] == "completed"
    assert closed["resolution"]["display_status"] == "closed_unverified"
    assert closed["resolution"]["measurement_status"] == "not_run"
    assert closed["resolution"]["certification"] == "not_issued"
    assert api.resume(run)["resolution"] == closed["resolution"]


def test_dynamic_check_set_and_single_copy_measurements(case):
    api, start, _, _ = case
    run = start()
    api.submit(run, "submit")
    original = measure(api, run)
    contract_hash = original["job"]["contract_hash"]
    first_hash = original["job"]["check_set_hash"]
    api.register_check(run, {"check_id": "probe", "interpretation_revision": 1,
                            "parameters": {"kind": "file", "path": "c.txt", "operator": "equals", "expected": "c"}}, "add")
    latest = measure(api, run, "second")
    job = latest["job"]
    assert job["check_set_hash"] != first_hash and job["contract_hash"] == contract_hash
    assert "observations" not in job["result"]
    assert len(job["result"]["observation_refs"]) == len(latest["measurements"]) == 3
    assert all(m["check_set_hash"] == job["check_set_hash"] for m in latest["measurements"])
    with api.store.transaction(write=False) as connection:
        stored = json.loads(connection.execute("SELECT data FROM jobs WHERE job_id=?", (job["job_id"],)).fetchone()[0])
    assert "observations" not in stored["result"]
    record = api.finish(run, "finish", outcome="completed")["record"]
    assert record["check_set_hash"] == job["check_set_hash"]
    assert record["resolution"]["display_status"] == "closed_checks_passed"


def test_missing_measurement_cannot_satisfy_result_reference(case):
    api, start, _, _ = case
    run = start(mode="strict")
    api.submit(run, "submit")
    state = measure(api, run)
    with api.store.transaction() as connection:
        connection.execute("DELETE FROM measurements WHERE observation_id=?", (state["measurements"][0]["observation_id"],))
    with pytest.raises(HarnessError):
        api.finish(run, "finish", outcome="completed")
    with pytest.raises(HarnessError) as error:
        api.status(run, state["job"]["job_id"])
    assert error.value.code == "RESULT_BINDING"


class ExternalDomain(DevelopModule):
    def __init__(self):
        self.identity_config = {"variant": "original"}


def test_external_domain_changes_are_detected_with_same_revision(case, monkeypatch):
    _, _, params, ws = case
    module = ExternalDomain()
    api = Harness(ws.parent / "external-state", domains=DomainRegistry([module]))
    run = api.start(domain_id="develop", goal="inspect", workspace=str(ws), parameters=params, request_id="start")["run_id"]
    api.submit(run, "submit")
    pinned = api.status(run)["run"]["domain_module"]
    for _ in range(5):
        assert api.domains.identity("develop") == pinned
    module.identity_config["variant"] = "changed"
    with pytest.raises(HarnessError) as error:
        api.verify(run, "changed-config")
    assert error.value.code == "DOMAIN_MODULE_CHANGED"
    module.identity_config["variant"] = "original"
    def replacement(*args, **kwargs):
        raise AssertionError("Changed implementation must not run")
    monkeypatch.setattr(module, "normalize_check", replacement)
    with pytest.raises(HarnessError) as error:
        api.verify(run, "changed-code")
    assert error.value.code == "DOMAIN_MODULE_CHANGED"


def test_pure_reader_does_not_block_writer_and_sees_committed_snapshot(case):
    api, start, _, _ = case
    run = start()
    with api.store.transaction() as writer:
        saved = api.store.run(writer, run)
        saved["phase"] = "uncommitted-marker"
        api.store.save_run(writer, saved)
        with ThreadPoolExecutor(max_workers=1) as pool:
            read = pool.submit(lambda: Harness(api.store.root).records(run, "interpretations"))
            assert read.result(timeout=3)["total"] == 1
    with api.store.transaction(write=False) as reader:
        assert reader.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            reader.execute("DELETE FROM runs")


def test_summary_size_does_not_scale_with_measurement_bodies(case):
    api, start, params, ws = case
    (ws / "a.txt").write_text("a" * 12000)
    expanded = copy.deepcopy(params)
    expanded["expectations"][0].update(operator="contains", expected="a")
    run = start(parameters=expanded, budget={"max_actions": 100})
    for i in range(10):
        api.register_check(run, {"check_id": "probe" + str(i), "interpretation_revision": 1,
            "parameters": {"kind": "file", "path": "a.txt", "operator": "contains", "expected": "a"}}, "p" + str(i))
    api.submit(run, "submit")
    for i in range(3):
        measure(api, run, "verify" + str(i))
    summary = api.status(run, view="summary")
    full = api.status(run, view="full")
    assert len(canonical_bytes(summary)) < len(canonical_bytes(full)) / 5
    assert summary["history_counts"]["measurements"] == 36
    assert "measurements" not in summary


def test_historical_aggregate_results_remain_readable(case):
    api, start, _, _ = case
    run = start()
    api.submit(run, "submit")
    state = measure(api, run)
    with api.store.transaction() as connection:
        job = api.store.job(connection, state["job"]["job_id"])
        legacy = {"status": "passed", "candidate_hash": job["candidate_hash"], "contract_hash": job["contract_hash"],
                  "environment": {}, "finished_at": 1, "observations": [m["report"] for m in state["measurements"]]}
        legacy["result_hash"] = canonical_hash(legacy)
        job["result"] = legacy
        api.store.save_job(connection, job)
    assert Harness(api.store.root).status(run, job["job_id"])["job"]["result"] == legacy


def test_cli_json_sources_and_default_summary(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "a.txt").write_text("a")
    environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    def cli(*args, data=None, code=0):
        execution = subprocess.run([sys.executable, "-m", "harness_external", "--state-dir", str(tmp_path / "state"), *args],
                                   input=data, text=True, capture_output=True, env=environment, timeout=20)
        assert execution.returncode == code, execution.stdout + execution.stderr
        return json.loads(execution.stdout)
    params = {"profile": "structural", "inputs": ["a.txt"], "artifacts": ["a.txt"],
              "expectations": [{"id": "content", "path": "a.txt", "operator": "equals", "expected": "a"}]}
    run = cli("start", "--workspace", str(ws), "--goal", "inspect", "--parameters", "-", "--request-id", "start", data=json.dumps(params))["run_id"]
    note = cli("observe", "--run-id", run, "--data", 'json:{"note":"inline caller note"}', "--request-id", "note")
    assert note["observation"]["note"] == "inline caller note"
    assert cli("status", "--run-id", run)["view"] == "summary"
    assert "measurements" not in cli("status", "--run-id", run)
    assert cli("status", "--run-id", run, "--view", "full")["run"]["observations"]
    for value, expected in [("{\"note\":\"x\"}", "JSON_INPUT_SOURCE"), ("json:{", "JSON_INVALID"), (str(tmp_path / "absent"), "JSON_FILE_ERROR")]:
        assert cli("observe", "--run-id", run, "--data", value, "--request-id", "bad", code=2)["error"]["code"] == expected
    assert cli("start", "--workspace", str(ws), "--goal", "inspect", "--parameters", "-", "--budget", "-", "--request-id", "bad", code=2)["error"]["code"] == "JSON_STDIN_REUSED"
