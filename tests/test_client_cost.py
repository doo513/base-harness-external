"""Lossless reads and fewer caller steps, without changing verification policy."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from harness_external import context, worker
from harness_external.errors import HarnessError
from harness_external.attention import attention_report
from test_skill_workflow import helper, caller, bound_run, start_request, ROOT, HELPER
from test_pytest_domain import advisory_fixture, start_advisory


def test_direct_call_saves_normalized_request_once_and_replays_after_loss(caller, tmp_path, monkeypatch):
    client, _, _ = caller
    binding = bound_run(caller)
    target = tmp_path / "saved.json"
    original, calls = client.execute, []
    def lost(*args, **kwargs):
        result = original(*args, **kwargs)
        calls.append(args[0])
        raise helper.ClientError("CALL_OUTCOME_UNKNOWN", "Controlled response loss after commit")
    monkeypatch.setattr(client, "execute", lost)
    with pytest.raises(helper.ClientError) as error:
        client.call_operation("observe", {"data": {"note": "Only one intent"}}, binding=binding, action_id="note", output=target)
    assert calls == ["invoke"] and error.value.saved_request["request_file"] == str(target)
    envelope = helper.read_json(str(target))
    assert envelope["context"] == client.binding_context(binding)
    monkeypatch.setattr(client, "execute", original)
    before = target.read_bytes()
    replay = client.call_operation("observe", {"data": {"note": "Only one intent"}}, binding=binding, action_id="note", output=target)
    assert replay["ok"] and target.read_bytes() == before
    state = client.validate_binding(binding)
    assert state["usage"]["actions"] == 1
    with pytest.raises(helper.ClientError) as conflict:
        client.call_operation("observe", {"data": {"note": "different"}}, binding=binding, action_id="note", output=target)
    assert conflict.value.code == "SAVED_REQUEST_CONFLICT" and target.read_bytes() == before
    assert client.validate_binding(binding)["usage"]["actions"] == 1


def test_direct_read_does_not_create_request_files_or_extra_core_calls(caller, monkeypatch):
    client, _, _ = caller
    binding = bound_run(caller)
    original, calls = client.execute, []
    def counted(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)
    monkeypatch.setattr(client, "execute", counted)
    result = client.call_operation("resume", {"view": "compact"}, binding=binding)
    assert result["ok"] and calls == ["invoke"] and "caller_request" not in result
    assert client.validate_binding(binding)["usage"]["actions"] == 0


def test_direct_mutation_requires_a_saved_request_before_dispatch(caller, monkeypatch):
    client, _, _ = caller
    binding = bound_run(caller)
    monkeypatch.setattr(client, "execute", lambda *a, **k: pytest.fail("No dispatch before request preservation"))
    with pytest.raises(helper.ClientError) as error:
        client.call_operation("observe", {"data": {"note": "decision"}}, binding=binding, action_id="note")
    assert error.value.code == "REQUEST_FILE_REQUIRED"


def test_start_can_save_binding_from_its_receipt_without_an_extra_resume(caller, tmp_path, monkeypatch):
    client, workspace, _ = caller
    calls = []
    original = client.execute
    def counted(operation, arguments, *args):
        calls.append(arguments["request"]["operation"])
        return original(operation, arguments, *args)
    monkeypatch.setattr(client, "execute", counted)
    arguments = start_request(workspace)["arguments"]
    result = client.call_operation("start", arguments, action_id="begin", output=tmp_path / "request.json",
                                   binding_output=tmp_path / "binding.json")
    assert calls == ["start"]
    binding = helper.read_json(str(tmp_path / "binding.json"))
    assert binding["run_id"] == result["run_id"]
    assert binding["intent_ref"] == result["context"]["intent_ref"]
    assert client.validate_binding(binding)["run"]["run_id"] == result["run_id"]
    replay = client.call_operation("start", arguments, action_id="begin", output=tmp_path / "request.json",
                                   binding_output=tmp_path / "binding.json")
    assert replay["run_id"] == result["run_id"]


def test_compact_start_keeps_goal_policy_guidance_and_recovery_reference(caller, tmp_path):
    client, workspace, _ = caller
    result = client.call_operation("start", start_request(workspace)["arguments"], action_id="begin",
                                   output=tmp_path / "request.json", binding_output=tmp_path / "binding.json")
    compact = helper.compact_call_result("start", result)
    assert compact["goal"] == result["contract"]["original_goal"]
    assert compact["policy"] == result["policy"]
    assert compact["domain_preparation"] == result["domain_preparation"]
    assert compact["caller_request"] == result["caller_request"]
    assert compact["binding_file"] == str(tmp_path / "binding.json")
    assert compact["details"]["operation"] == "context"
    assert compact["details"]["contract_hash"] == result["contract"]["contract_hash"]
    assert len(helper.canonical(compact)) < len(helper.canonical(result))


def test_saved_intent_detaches_caller_owned_nested_values(caller, tmp_path, monkeypatch):
    client, _, _ = caller
    binding = bound_run(caller)
    arguments = {"data": {"note": "original"}}
    write = helper.write_new_json
    def change_after_save(path, value):
        write(path, value)
        arguments["data"]["note"] = "changed by another caller"
    monkeypatch.setattr(helper, "write_new_json", change_after_save)
    saved = tmp_path / "detached.json"
    result = client.call_operation("observe", arguments, binding=binding, action_id="detached", output=saved)
    assert result["observation"]["note"] == "original" and helper.read_json(str(saved))["arguments"]["data"]["note"] == "original"


def test_combined_cli_reuses_existing_operation_surface(caller, tmp_path):
    client, _, config = caller
    binding = bound_run(caller)
    binding_path = tmp_path / "binding.json"
    assert helper.read_json(str(binding_path)) == binding
    saved = tmp_path / "saved.json"
    argv = [sys.executable, str(HELPER), "--config", str(config), "call", "--binding", str(binding_path),
            "--operation", "observe", "--arguments", 'json:{"data":{"note":"한 번 전송"}}',
            "--action-id", "note", "--output", str(saved)]
    process = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    result = json.loads(process.stdout)
    assert process.returncode == 0 and result["caller_request"]["request_file"] == str(saved)
    replay = subprocess.run([sys.executable, str(HELPER), "--config", str(config), "call", "--request", str(saved)],
                            capture_output=True, text=True, timeout=30)
    assert json.loads(replay.stdout)["observation"] == result["observation"]
    assert client.validate_binding(binding)["usage"]["actions"] == 1


def test_bad_saved_envelope_is_a_structured_error(caller):
    _, _, config = caller
    process = subprocess.run([sys.executable, str(HELPER), "--config", str(config), "call", "--request", "json:{}"],
                            capture_output=True, text=True, timeout=10)
    assert process.returncode == 2 and json.loads(process.stdout)["error"]["code"] == "REQUEST_INVALID"
    assert "Traceback" not in process.stderr


def populate(client, binding):
    for index in range(7):
        client.call_operation("observe", {"data": {"note": f"{index}: " + "한글😀 근거 " * 30}},
                              action_id=f"note-{index}", binding=binding, output=Path(client.config["state_dir"]) / f"note-{index}.json")


def test_complete_context_is_lossless_pinned_and_smaller_than_raw_pages(caller, monkeypatch):
    client, _, _ = caller
    binding = bound_run(caller)
    populate(client, binding)
    original, pages = client.call, []
    def captured(request):
        result = original(request)
        if request["operation"] == "context":
            pages.append(result)
            if len(pages) == 1:
                original(client.make_request("observe", {"data": {"note": "arrived during transfer"}},
                                             action_id="late", binding=binding)["request"])
        return result
    monkeypatch.setattr(client, "call", captured)
    packet = client.context(binding, complete=True, limit=20)
    expected = {}
    for page in pages:
        expected = context.apply_page(expected, page)
    assert len(pages) > 1 and helper.expand_context(packet) == expected
    assert packet["snapshot"] == pages[0]["snapshot"] == pages[-1]["snapshot"]
    assert packet["critical"] == pages[-1]["critical"] and packet["snapshot_critical"] == pages[-1]["snapshot_critical"]
    assert packet["next_cursor"] == pages[-1]["next_cursor"]
    assert "arrived during transfer" not in helper.canonical(expected).decode("utf-8")
    assert len(helper.canonical(packet)) < sum(len(helper.canonical(page)) for page in pages)
    assert "next_cursor" not in binding
    delta = client.context(binding, after=packet["next_cursor"], complete=True)
    assert delta["delivery"] == "delta" and "arrived during transfer" in str(helper.expand_context(delta))


@pytest.mark.parametrize("damage", ["text", "digest", "snapshot", "offset"])
def test_complete_context_rejects_bad_pages_without_delivering_cursor(caller, monkeypatch, damage):
    client, _, _ = caller
    binding = bound_run(caller)
    populate(client, binding)
    original, calls = client.call, 0
    def corrupted(request):
        nonlocal calls
        result = copy.deepcopy(original(request))
        calls += 1
        if damage == "text" and calls == 1:
            key = next(iter(result["texts"]))
            result["texts"][key] += "changed"
        elif damage == "digest" and calls == 1:
            result["items"][0]["digest"] = "0" * 64
        elif damage == "snapshot" and calls == 2:
            result["snapshot"]["hash"] = "0" * 64
        elif damage == "offset" and calls == 2:
            result["offset"] = 0
        return result
    monkeypatch.setattr(client, "call", corrupted)
    with pytest.raises(helper.ClientError):
        client.context(binding, complete=True, limit=20)
    assert "next_cursor" not in binding


def test_complete_context_page_limit_and_empty_delta(caller):
    client, _, _ = caller
    binding = bound_run(caller)
    with pytest.raises(helper.ClientError) as error:
        client.context(binding, complete=True, limit=1, max_pages=1)
    assert error.value.code == "CONTEXT_PAGE_LIMIT"
    restored = client.context(binding, complete=True)
    delta = client.context(binding, complete=True, after=restored["next_cursor"])
    assert delta["delivery"] == "delta" and helper.expand_context(delta) == {} and delta["critical"]


def test_resolve_detail_verifies_multibyte_chunks_and_changed_references(caller, monkeypatch):
    client, _, _ = caller
    binding = bound_run(caller)
    note = "한글😀" * 120
    client.call_operation("observe", {"data": {"note": note}}, binding=binding, action_id="unicode",
                          output=Path(client.config["state_dir"]) / "unicode.json")
    projected = client.call_operation("records", {"kind": "notes", "view": "references"}, binding=binding)["items"][0]
    ref = projected["detail_refs"][0]
    arguments = {"kind": "notes", "detail": {**{key: projected["source"][key] for key in ("record_id", "record_digest")},
                 "path": ref["path"], "sha256": ref["sha256"], "limit": 333}}
    value = client.resolve_detail(binding, arguments)
    assert value["value"] == note and value["sha256"] == helper.digest(note)
    original, count = client.call, 0
    def wrong(request):
        nonlocal count
        result = original(request)
        count += 1
        if count == 2:
            result["sha256"] = "0" * 64
        return result
    monkeypatch.setattr(client, "call", wrong)
    with pytest.raises(helper.ClientError) as error:
        client.resolve_detail(binding, arguments)
    assert error.value.code == "DETAIL_PROTOCOL"


@pytest.fixture(scope="module")
def observed(tmp_path_factory):
    root = tmp_path_factory.mktemp("client-actual-cases")
    source = "import pytest\ndef test_ok(): assert True\ndef test_bad(): assert 'a' * 2000 == 'b' * 2000\n@pytest.mark.skip(reason='not available')\ndef test_skip(): pass\n"
    source += "\n".join(f"def test_normal_{index}(): assert {index} == {index}" for index in range(14))
    api, workspace, definition = advisory_fixture(root, source, required="test_cases.py::test_bad")
    patch = pytest.MonkeyPatch()
    patch.setattr(worker, "spawn_worker", lambda *a: None)
    try:
        run = start_advisory(api, workspace, definition, capture_baseline=True)["run_id"]
        api.submit(run, "submit")
        job = api.verify(run, "verify", compare_baseline=True)["job_id"]
        worker.execute_job(api.store.root, job)
    finally:
        patch.undo()
    config_path = root / "client.json"
    config = helper.configure(config_path, str(ROOT), str(api.store.root), ephemeral=True)["config"]
    client = helper.Client(config)
    binding_path = root / "binding.json"
    client.bind(run, str(workspace), "develop", binding_path)
    return api, client, helper.read_json(str(binding_path)), job


LIVE = pytest.mark.skipif(os.environ.get("BASE_HARNESS_EXTERNAL_LIVE") != "1", reason="actual pytest measurements and indexed case reads")


@LIVE
def test_case_filter_is_indexed_paginated_and_preserves_subject_binding(observed):
    api, _, binding, job = observed
    run = binding["run_id"]
    all_items = api.records(run, "cases", job_id=job, limit=100)["items"]
    expected = [item for item in all_items if item["subject_role"] == "candidate" and item["case"]["outcome"] in {"failed", "skipped"}]
    first = api.records(run, "cases", job_id=job, outcomes=["failed", "skipped"], subject_role="candidate", limit=1)
    second = api.records(run, "cases", job_id=job, outcomes=["failed", "skipped"], subject_role="candidate", limit=1, offset=1)
    assert first["total"] == 2 and first["next_offset"] == 1 and second["next_offset"] is None
    assert first["items"] + second["items"] == expected
    assert api.records(run, "cases", job_id=job, outcomes=["'); DROP TABLE jobs;--"])["total"] == 0
    with api.store.transaction(write=False) as connection:
        plan = list(connection.execute("EXPLAIN QUERY PLAN SELECT data FROM case_observations WHERE run_id=? AND job_id=? AND json_extract(data,'$.subject_role')=? AND json_extract(data,'$.case.outcome') IN (?) ORDER BY rowid LIMIT 20", (run, job, "candidate", "failed")))
    assert any("case_observations_outcome" in row[-1] for row in plan)


@LIVE
def test_attention_preserves_nonpassing_states_and_resolves_actual_error(observed):
    api, client, binding, job = observed
    before = api.status(binding["run_id"])["run"]
    summary = client.call_operation("status", {"job_id": job}, binding=binding)
    attention = client.attention(summary, binding)
    states = {item.get("status") for item in attention["attention"]}
    assert {"failed", "skipped"} <= states and attention["selected_job"]["result_status"] == "failed"
    assert attention["not_queried"] == []  # Core joins current state and selected facts in one read.
    assert attention["case_query"]["total"] == 2 and attention["attention_page_complete"]
    bad = next(item for item in attention["attention"] if item.get("case_id") == "test_cases.py::test_bad")
    assert bad["requirement_ids"] == ["value"]
    phase = next(item for item in bad["errors"] if "excerpt" in item)
    assert phase["truncated"]
    decoded = client.resolve_detail(binding, phase["detail"])
    assert decoded["value"].startswith(phase["excerpt"]) and len(decoded["value"]) > len(phase["excerpt"])
    after = api.status(binding["run_id"])["run"]
    for key in ("contract", "policy", "candidate", "verification", "actions", "assessments"):
        assert after[key] == before[key]
    # The Core view now also carries current Run constraints and actual case
    # excerpts. Compare the complete prior input set, not the Job-only reply.
    prior_state = api.resume(binding["run_id"], view="compact")
    prior_cases = api.records(binding["run_id"], "cases", job_id=job,
                              outcomes=["failed", "skipped"], subject_role="candidate")
    assert len(helper.canonical(attention)) < sum(len(helper.canonical(item))
                                                for item in (summary, prior_state, prior_cases))


@LIVE
def test_filtered_query_still_checks_corrupt_records_that_would_be_filtered_out(observed):
    api, _, binding, job = observed
    run = binding["run_id"]
    failed = api.records(run, "cases", job_id=job, outcomes=["failed"], subject_role="candidate")["items"][0]
    with api.store.transaction() as connection:
        original = connection.execute("SELECT data FROM case_observations WHERE observation_id=?", (failed["observation_id"],)).fetchone()[0]
        damaged = json.loads(original)
        damaged["case"]["outcome"] = "passed"
        connection.execute("UPDATE case_observations SET data=? WHERE observation_id=?", (json.dumps(damaged), failed["observation_id"]))
    try:
        with pytest.raises(HarnessError):
            api.records(run, "cases", job_id=job, outcomes=["failed"], subject_role="candidate")
    finally:
        with api.store.transaction() as connection:
            connection.execute("UPDATE case_observations SET data=? WHERE observation_id=?", (original, failed["observation_id"]))


@pytest.mark.parametrize("arguments", [{"outcomes": []}, {"outcomes": [True]}, {"outcomes": ["failed", "failed"]}, {"subject_role": []}, {"subject_role": "wrong"}])
def test_invalid_case_filters_are_structured_errors(caller, arguments):
    client, _, _ = caller
    binding = bound_run(caller)
    if isinstance(arguments.get("subject_role"), list):
        with pytest.raises(helper.ClientError) as error:
            client.call_operation("records", {"kind": "cases", **arguments}, binding=binding)
        assert error.value.code == "ARGUMENTS_INVALID"
        from harness_external.service import Harness
        with pytest.raises(HarnessError) as core_error:
            Harness(client.config["state_dir"]).records(binding["run_id"], "cases", **arguments)
        assert core_error.value.code == "INVALID_FILTER"
    else:
        result = client.call_operation("records", {"kind": "cases", **arguments}, binding=binding)
        assert result["ok"] is False and result["error"]["code"] == "INVALID_FILTER"


def test_attention_keeps_unobserved_scenarios_known_gaps_and_pending_distinct():
    current = {"ok": True, "api_version": helper.API_VERSION, "assurance": "local-advisory", "ready": False,
               "run_id": "run", "view": "summary", "wait_status": "pending"}
    scenarios = [{"check_id": "c", "case_id": str(i), "status": state, "executed": None, "observation_id": None}
                 for i, state in enumerate(("missing", "unconfirmed", "not_selected", "not_run"))]
    job = {"job_id": "j", "status": "running", "result": {"status": "incomplete", "requirement_observations": {
        "requirements": [{"requirement_id": "r", "known_gap_ids": ["gap"], "scenarios": scenarios}]}}}
    result = attention_report(current, job, {"items": [], "total": 0, "next_offset": None}, limit=2)
    assert [item["status"] for item in result["attention"]] == ["missing", "unconfirmed"]
    assert result["total_known_attention_count"] == 4 and result["omitted_attention_count"] == 2
    assert result["attention_page_complete"] is False and result["wait_status"] == "pending"
    assert result["known_gaps"] == [{"requirement_id": "r", "known_gap_ids": ["gap"]}]
    assert result["selected_job"]["is_current_verification"] is None and result["ready"] is False


def test_attention_rejects_a_foreign_run_without_querying(caller, monkeypatch):
    client, _, _ = caller
    binding = bound_run(caller)
    monkeypatch.setattr(client, "call", lambda *a, **k: pytest.fail("Foreign response must not trigger extra queries"))
    with pytest.raises(helper.ClientError) as error:
        client.attention({"ok": True, "run_id": "foreign", "view": "summary"}, binding)
    assert error.value.code == "RUN_CONTEXT_MISMATCH"


def test_unknown_adapter_phase_content_stays_retrievable():
    response = {"api_version": helper.API_VERSION, "ok": True, "assurance": "local-advisory", "ready": False, "run_id": "run"}
    case = {"outcome": "error", "case_id": "opaque", "executed": True, "finished": True,
            "phases": [{"message": {"adapter_detail": [1, 2]}}, "opaque phase"]}
    record = {"subject_role": "candidate", "observation_id": "obs", "check_id": "check", "case": case}
    result = attention_report(response, {"job_id": "j", "status": "completed"}, {"items": [record], "total": 1, "next_offset": None})
    assert result["attention"][0]["status"] == "error"
    assert [item["detail"]["detail"]["path"] for item in result["attention"][0]["errors"]] == [["case", "phases", 0], ["case", "phases", 1]]
