"""Checkpoint projections and optional caller context never become authority."""
import copy
import json

import pytest

from harness_external import worker
from harness_external.dispatch import invoke, waiting
from harness_external.errors import HarnessError
from test_core_attention import file_run


def envelope(run, workspace, *, key="checkpoint", **arguments):
    return {"operation": "checkpoint", "request_id": key,
            "context": {"workspace": str(workspace), "domain": "develop"},
            "arguments": {"run_id": run, "wait_seconds": 0, **arguments}}


def synchronous(monkeypatch):
    monkeypatch.setattr(worker, "spawn_worker", lambda state, job: worker.execute_job(state, job))


def test_brief_checkpoint_is_bounded_and_keeps_failure_scope(file_run, monkeypatch):
    api, workspace, run, _ = file_run(checks=24, measured=False, requirements=[
        {"id": "R01", "statement": "The output is ready", "check_ids": ["domain.result-0"]}])
    synchronous(monkeypatch)
    result = invoke(api, envelope(run, workspace, view="brief", attention_limit=8))
    assert result["schema_version"] == "harness-checkpoint-v1"
    assert result["wait_status"] == "terminal"
    assert result["selected_job"]["result_status"] == "failed"
    assert result["selected_job"]["relevance"] == "current"
    assert result["gates"]["status"] == "unsatisfied"
    assert result["omitted"]["issues"] > 0
    assert len(json.dumps(result, ensure_ascii=False).encode("utf-8")) <= 8192
    assert any("R01" in item.get("requirement_ids", []) for item in result["issues"])
    details = result["details"]["job"]
    assert api.records(run, **details)["encoding"] == "base64-canonical-json"
    assert api.resume(run)["assessment"]["status"] == "not_assessed"
    assert api.resume(run)["run"]["status"] == "active"


def test_legacy_checkpoint_shape_and_empty_context_remain_unchanged(file_run, monkeypatch):
    api, workspace, run, _ = file_run(measured=False)
    synchronous(monkeypatch)
    result = invoke(api, envelope(run, workspace))
    assert result["job"]["result"]["status"] == "failed"
    assert api.resume(run)["history_counts"]["notes"] == 0
    assert api.resume(run)["usage"]["actions"] == 2


def test_payload_values_survive_but_cannot_claim_pass_or_ready(file_run, monkeypatch):
    api, workspace, run, _ = file_run(measured=False)
    synchronous(monkeypatch)
    payload = {"fields": {"decision": "keep behavior", "unknown": None, "empty": ""},
               "extensions": {"platform_note": {"observed": False, "count": 0, "items": []}, "passed": True, "ready": True}}
    request = envelope(run, workspace, view="brief", payload=payload)
    result = invoke(api, request)
    notes = api.records(run, "notes")["items"]
    assert len(notes) == 1
    assert notes[0]["payload"] == {**payload, "parents": []}
    # Python equality alone accepts False == 0; compare JSON to retain value types.
    assert json.dumps(notes[0]["payload"], sort_keys=True) == json.dumps({**payload, "parents": []}, sort_keys=True)
    assert notes[0]["origin"] == "caller" and notes[0]["trust"] == "untrusted"
    assert result["caller_record"]["record_ref"]["run_id"] == run
    assert result["gates"]["status"] == "unsatisfied" and result["ready"] is False
    replay = invoke(api, {**request, "arguments": {**request["arguments"], "wait_seconds": 1}})
    assert replay["caller_record"] == result["caller_record"]
    assert replay["selected_job"]["job_id"] == result["selected_job"]["job_id"]
    assert api.resume(run)["history_counts"]["notes"] == 1


def test_loss_after_note_does_not_duplicate_or_change_it(file_run, monkeypatch):
    api, workspace, run, _ = file_run(measured=False)
    synchronous(monkeypatch)
    request = envelope(run, workspace, view="brief", payload={"extensions": {"exception": "CRLF fixture"}})
    original = api.submit
    monkeypatch.setattr(api, "submit", lambda *a, **k: (_ for _ in ()).throw(OSError("controlled loss after note")))
    with pytest.raises(OSError) as caught:
        invoke(api, request)
    assert caught.value.checkpoint_progress["stage"] == "submit"
    assert caught.value.checkpoint_progress["caller_record"]["record_ref"]["run_id"] == run
    assert api.resume(run)["history_counts"]["notes"] == 1
    assert api.resume(run)["candidate"] is None
    monkeypatch.setattr(api, "submit", original)
    result = invoke(api, request)
    assert result["selected_job"]["status"] == "completed"
    state = api.resume(run)
    assert state["history_counts"]["notes"] == 1 and state["history_counts"]["jobs"] == 1
    assert state["usage"]["actions"] == 3


@pytest.mark.parametrize("first", [None, {"extensions": {"value": "first"}}])
def test_same_intent_cannot_later_add_or_replace_context(file_run, monkeypatch, first):
    api, workspace, run, _ = file_run(measured=False)
    synchronous(monkeypatch)
    request = envelope(run, workspace, **({"payload": first} if first is not None else {}))
    invoke(api, request)
    before = api.resume(run)["history_counts"]
    changed = copy.deepcopy(request)
    changed["arguments"]["payload"] = {"extensions": {"value": "different"}}
    with pytest.raises(HarnessError) as error:
        invoke(api, changed)
    assert error.value.code == "REQUEST_CONFLICT"
    assert api.resume(run)["history_counts"] == before


def test_invalid_payload_fails_before_note_or_snapshot(file_run):
    api, workspace, run, _ = file_run(measured=False)
    with pytest.raises(HarnessError):
        invoke(api, envelope(run, workspace, payload={"extensions": [], "fields": {}}))
    state = api.resume(run)
    assert state["candidate"] is None and state["history_counts"]["notes"] == 0
    assert state["usage"]["actions"] == 0


def test_brief_wait_does_not_erase_pending_or_historical_scope(file_run, monkeypatch):
    api, workspace, run, _ = file_run(measured=False)
    pending = invoke(api, envelope(run, workspace, view="brief"))
    assert pending["wait_status"] == "pending"
    job = pending["selected_job"]["job_id"]
    worker.execute_job(api.store.root, job)
    (workspace / "result-0.txt").write_text("ready", encoding="utf-8")
    api.submit(run, "new-subject")
    result = waiting(api, run, job, 0, view="brief")
    assert result["selected_job"]["relevance"] == "historical"
    assert result["measurement"]["status"] == "not_run"
    assert result["gates"]["status"] != "passed"
