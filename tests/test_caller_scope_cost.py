"""Evidence gaps remain visible; local cost reporting never grants authority."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from harness_external.service import Harness
from harness_external.worker import execute_job
from test_skill_workflow import helper, module_at, ROOT, HELPER

reporter = module_at("caller_cost_report", ROOT / "scripts/report-caller-cost.py")


def setup(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "data").write_text("ready", encoding="utf-8")
    config_path = tmp_path / "client.json"
    config = helper.configure(config_path, str(ROOT), str(tmp_path / "state"), ephemeral=True)["config"]
    return workspace, config_path, config


def arguments(workspace):
    return {"domain": "develop", "workspace": str(workspace), "goal": "Implement the specification",
            "mode": "exploratory", "provenance": {"declared_author": "model"},
            "parameters": {"profile": "structural", "inputs": ["data"], "artifacts": ["data"],
                           "expectations": [{"path": "data", "operator": "equals", "expected": "ready"}]}}


def test_passing_checks_do_not_hide_missing_requirements_or_gates(tmp_path, monkeypatch):
    workspace, _, config = setup(tmp_path)
    api = Harness(config["state_dir"])
    monkeypatch.setattr("harness_external.worker.spawn_worker", lambda *args: None)
    value = arguments(workspace)
    value["domain_id"] = value.pop("domain")
    begun = api.start(**value, request_id="start")
    api.submit(begun["run_id"], "submit")
    job_id = api.verify(begun["run_id"], "verify")["job_id"]
    execute_job(api.store.root, job_id)
    for view in ("summary", "compact"):
        current = {"ok": True, **api.resume(begun["run_id"], view=view)}
        scope = current["verification_scope"]
        assert scope["declared_requirement_count"] == 0 and scope["gate_status"] == "not_required"
        assert scope["coverage_conclusion"] == "not_established"
        job = api.status(begun["run_id"], job_id, view="summary")["job"]
        attention = helper.attention_report(current, job, {"items": [], "next_offset": None, "total": 0})
        assert any(item.get("code") == "REQUIREMENTS_NOT_DECLARED" for item in attention["attention"])
        assert job["result"]["status"] == "passed"
    closed = api.finish(begun["run_id"], "finish", outcome="completed")
    assert closed["verification_scope"]["coverage_conclusion"] == "not_established"
    assert closed["record"]["resolution"]["gate_status"] == "not_required"


def test_cli_source_and_schema_work_without_an_existing_run(tmp_path):
    workspace, config_path, config = setup(tmp_path)
    response = subprocess.run([sys.executable, str(HELPER), "--config", str(config_path), "describe", "--domain", "develop",
                               "--adapter", "process-observations-v1"], capture_output=True, text=True, encoding="utf-8", timeout=30)
    discovered = json.loads(response.stdout)
    assert response.returncode == 0 and discovered["ok"], discovered
    assert "fixtures" in discovered["adapters"][0]["selector_schema"]["properties"]
    source = tmp_path / "SPEC.md"
    source.write_bytes(b"R01. Return ready\r\nR02. Preserve inputs\r\n")
    request = tmp_path / "start.json"
    binding = tmp_path / "binding.json"
    trace = tmp_path / "cost.jsonl"
    response = subprocess.run([sys.executable, str(HELPER), "--config", str(config_path), "--trace-file", str(trace),
        "call", "--operation", "start", "--arguments", "json:" + json.dumps(arguments(workspace)), "--source", "spec=" + str(source),
        "--action-id", "original", "--output", str(request), "--binding-output", str(binding), "--format", "compact"],
        capture_output=True, text=True, encoding="utf-8", timeout=30)
    result = json.loads(response.stdout)
    assert response.returncode == 0 and result["ok"], result
    restored = Harness(config["state_dir"]).resume(result["run_id"])
    assert restored["intent"]["sources"][0]["text"] == source.read_bytes().decode("utf-8")
    assert result["client_cost"]["transport_calls"] == 1
    assert result["client_cost"]["model_tokens"] is None
    events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    assert all("R01" not in json.dumps(event) for event in events)  # No original text in telemetry.
    summary = reporter.summarize(events, run_id=result["run_id"])
    assert summary["helper_invocations"] == summary["transport_calls"] == 1
    assert summary["host_reported_model_usage"] is None and summary["billed_tokens"] is None


def test_unknown_outcomes_are_traced_without_retry_or_false_failure(tmp_path, monkeypatch):
    _, _, config = setup(tmp_path)
    trace = tmp_path / "cost.jsonl"
    client = helper.Client(config, trace_file=trace)
    calls = []
    def lost(*args):
        calls.append(args)
        raise helper.ClientError("CALL_OUTCOME_UNKNOWN", "lost")
    monkeypatch.setattr(client, "_execute", lost)
    with pytest.raises(helper.ClientError):
        client.execute("invoke", {"request": {"operation": "submit", "request_id": "same", "arguments": {"run_id": "run-test"}}})
    assert len(calls) == 1
    events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    summary = reporter.summarize(events)
    assert summary["unknown_outcomes"] == 1 and not summary["response_bytes_complete"]
    assert events[0]["ok"] is None and events[0]["response_bytes"] is None


def test_trace_failure_cannot_turn_success_into_a_failed_mutation(tmp_path, monkeypatch):
    workspace, _, config = setup(tmp_path)
    trace = tmp_path / "unrelated.txt"
    trace.write_text("user content", encoding="utf-8")
    client = helper.Client(config, trace_file=trace)
    monkeypatch.setattr(client, "_execute", lambda *args: {"ok": True, "run_id": "run-test"})
    result = client.execute("invoke", {"request": {"operation": "start", "arguments": {"workspace": str(workspace)}}})
    assert result["ok"] and client.trace_warnings == ["TRACE_WRITE_FAILED"]
    assert trace.read_text(encoding="utf-8") == "user content"


def test_trace_parent_segments_cannot_hide_a_workspace_write(tmp_path, monkeypatch):
    workspace, _, config = setup(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    client = helper.Client(config, trace_file=outside / ".." / workspace.name / "cost.jsonl")
    monkeypatch.setattr(client, "_execute", lambda *args: {"ok": True, "run_id": "run-test"})
    result = client.execute("invoke", {"request": {"operation": "start", "arguments": {"workspace": str(workspace)}}})
    assert result["ok"]
    assert not (workspace / "cost.jsonl").exists()
    assert client.trace_warnings == ["TRACE_INSIDE_WORKSPACE"]


def test_optional_trace_accepts_a_null_error_field_without_changing_success(tmp_path, monkeypatch):
    _, _, config = setup(tmp_path)
    client = helper.Client(config, trace_file=tmp_path / "cost.jsonl")
    reply = {"ok": True, "error": None, "run": None, "job": None}
    monkeypatch.setattr(client, "_execute", lambda *args: reply)
    assert client.execute("describe", {"domain_id": "develop"}) == reply
    measured = helper.with_cost(reply, client, client.trace_file, helper.time.monotonic(), "describe")
    assert measured["ok"] and measured["client_cost"]["error_code"] is None


def test_host_usage_must_match_a_measured_run_and_stays_separate():
    events = [{"schema_version": "harness-client-cost-v1", "kind": "transport", "operation": "status", "run_id": "one",
               "elapsed_ms": 1, "response_bytes": 10, "ok": True}]
    usage = {"source": "agent-export", "model": "test-model", "run_id": "one", "input_tokens": 100, "output_tokens": 20}
    result = reporter.summarize(events, model_usage=usage)
    assert result["host_reported_model_usage"] == usage and result["billed_tokens"] is None
    with pytest.raises(ValueError):
        reporter.summarize(events, model_usage={**usage, "run_id": "other"})
