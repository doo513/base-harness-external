"""Caller packaging and lifecycle tests; no new Core/domain semantics."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "harness-workflow"
HELPER = SKILL / "scripts" / "harness_client.py"


def module_at(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


helper = module_at("harness_skill_client", HELPER)
installer = module_at("harness_skill_installer", ROOT / "scripts" / "install-skill.py")


@pytest.fixture
def caller(tmp_path):
    workspace = tmp_path / "작업 space"
    workspace.mkdir()
    (workspace / "payload.txt").write_text("before", encoding="utf-8")
    config_path = tmp_path / "config.json"
    config = helper.configure(config_path, str(ROOT), str(tmp_path / "state"), ephemeral=True)["config"]
    return helper.Client(config), workspace, config_path


def start_request(workspace, key="start-1", goal="Make payload ready"):
    return {"operation": "start", "request_id": key, "arguments": {
        "domain": "develop", "workspace": str(workspace), "goal": goal, "mode": "exploratory",
        "provenance": {"declared_author": "model"}, "required_check": ["domain.payload"],
        "parameters": {"profile": "structural", "inputs": ["payload.txt"], "artifacts": ["payload.txt"],
                       "expectations": [{"id": "payload", "path": "payload.txt", "operator": "equals", "expected": "ready"}]}}}


def request(workspace, run, operation, key=None, **arguments):
    value = {"operation": operation, "context": {"workspace": str(workspace), "domain": "develop"},
             "arguments": {"run_id": run, **arguments}}
    if key is not None:
        value["request_id"] = key
    return value


def wait_job(client, workspace, run, job):
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        result = client.call(request(workspace, run, "status", job_id=job))
        assert result["ok"], result
        if result["job"]["status"] not in {"queued", "running"}:
            return result["job"]
        time.sleep(0.1)
    pytest.fail("Worker did not reach a terminal state within the test deadline")


def invoke_cli(path, config, payload, cwd):
    process = subprocess.run([sys.executable, str(path), "--config", str(config), "call", "--request", "-"],
                             input=json.dumps(payload, ensure_ascii=False), encoding="utf-8",
                             capture_output=True, cwd=cwd, timeout=30)
    return process.returncode, json.loads(process.stdout)


def test_config_is_exclusive_and_temporary_state_is_explicit(tmp_path):
    config = tmp_path / "config.json"
    with pytest.raises(helper.ClientError) as error:
        helper.configure(config, str(ROOT), str(tmp_path / "state"))
    assert error.value.code == "EPHEMERAL_STATE"
    assert not config.exists()
    helper.configure(config, str(ROOT), str(tmp_path / "state"), ephemeral=True)
    before = config.read_bytes()
    with pytest.raises(FileExistsError):
        helper.configure(config, str(ROOT), str(tmp_path / "other"), ephemeral=True)
    assert config.read_bytes() == before
    assert not (tmp_path / "state").exists()  # configure did not initialize Core


def test_default_config_is_independent_of_working_directory(tmp_path, monkeypatch):
    monkeypatch.delenv("BASE_HARNESS_CLIENT_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "caller-config"))
    monkeypatch.chdir(tmp_path)
    first = helper.default_config_path()
    monkeypatch.chdir(ROOT)
    assert helper.default_config_path() == first
    monkeypatch.setenv("BASE_HARNESS_CLIENT_CONFIG", str(tmp_path / "explicit.json"))
    assert helper.default_config_path() == tmp_path / "explicit.json"


@pytest.mark.parametrize("change", [
    {"schema_version": True}, {"state_lifetime": "guess"}, {"state_dir": "relative"},
    {"harness_repo": "/this-checkout-does-not-exist"}, {"unknown": "field"},
])
def test_invalid_config_fails_before_invoking_core(caller, change):
    client, _, _ = caller
    with pytest.raises(helper.ClientError):
        helper.Client({**client.config, **change})


@pytest.mark.parametrize("raw", ['{"key":1,"key":2}', '{"value":NaN}', '{"value":Infinity}'])
def test_ambiguous_json_is_rejected(raw):
    with pytest.raises(helper.ClientError) as error:
        helper.read_json("json:" + raw)
    assert error.value.code == "JSON_INVALID"


def test_unknown_options_and_stringified_json_are_not_forwarded(caller):
    client, workspace, _ = caller
    for change in ({"state_dir": "/other-store"}, {"parameters": "json:{}"}, {"required_check": "domain.payload"}):
        payload = start_request(workspace)
        payload["arguments"].update(change)
        with pytest.raises(helper.ClientError):
            client.call(payload)
    assert not Path(client.config["state_dir"]).exists()


def test_request_ids_and_explicit_initial_policy_are_required(caller):
    client, workspace, _ = caller
    for omitted in ("mode", "provenance"):
        payload = start_request(workspace)
        del payload["arguments"][omitted]
        with pytest.raises(helper.ClientError):
            client.call(payload)
    payload = start_request(workspace)
    del payload["request_id"]
    with pytest.raises(helper.ClientError) as error:
        client.call(payload)
    assert error.value.code == "REQUEST_ID_REQUIRED"
    with pytest.raises(helper.ClientError):
        client.call({"operation": "doctor", "arguments": {}, "request_id": "not-a-mutation"})


def test_missing_state_is_not_reinitialized_for_resume_or_discovery(caller):
    client, workspace, _ = caller
    with pytest.raises(helper.ClientError) as error:
        client.find(str(workspace), "develop")
    assert error.value.code == "STATE_NOT_FOUND"
    with pytest.raises(helper.ClientError):
        client.call(request(workspace, "run_" + "0" * 32, "resume"))
    assert not Path(client.config["state_dir"]).exists()


def test_same_request_is_replayed_and_changed_payload_conflicts(caller):
    client, workspace, _ = caller
    payload = start_request(workspace, goal="--literal $(do-not-execute) '한글'\nsecond line")
    first = client.call(payload)
    assert first["ok"] and client.call(payload) == first
    changed = copy.deepcopy(payload)
    changed["arguments"]["goal"] = "Different task"
    assert client.call(changed)["error"]["code"] == "REQUEST_CONFLICT"
    state = client.call(request(workspace, first["run_id"], "resume"))
    assert state["intent"]["original_goal"] == payload["arguments"]["goal"]
    assert state["policy"]["provenance"]["declared_author"] == "model"
    assert state["policy"]["provenance"]["approval"]["status"] == "not_provided"
    assert len(client.find(str(workspace), "develop")["matches"]) == 1


def test_context_mismatch_never_applies_mutation(caller):
    client, workspace, _ = caller
    run = client.call(start_request(workspace))["run_id"]
    before = client.call(request(workspace, run, "resume"))["usage"]
    for context in ({"workspace": str(workspace.parent), "domain": "develop"},
                    {"workspace": str(workspace), "domain": "different"}):
        payload = request(workspace, run, "submit", "wrong-context")
        payload["context"] = context
        with pytest.raises(helper.ClientError) as error:
            client.call(payload)
        assert error.value.code == "RUN_CONTEXT_MISMATCH"
    assert client.call(request(workspace, run, "resume"))["usage"] == before


def test_large_json_uses_transport_files_without_single_argv_limit(caller):
    client, workspace, _ = caller
    payload = start_request(workspace)
    payload["arguments"]["parameters"]["expectations"] = [
        {"id": name, "path": "payload.txt", "operator": "contains", "expected": "한" * 18000}
        for name in ("payload", "second", "third")]
    result = client.call(payload)
    assert result["ok"]
    assert len(result["contract"]["checks"]) == 3


def bound_run(caller, *, baseline=False):
    client, workspace, config = caller
    payload = start_request(workspace)
    payload["arguments"]["capture_baseline"] = baseline
    run = client.call(payload)["run_id"]
    path = config.parent / "binding.json"
    binding = client.bind(run, str(workspace), "develop", path)["binding"]
    assert json.loads(path.read_text()) == binding
    return binding


def test_binding_reduces_context_input_but_rejects_changed_store_and_intent(caller):
    client, _, config = caller
    binding = bound_run(caller)
    changed = copy.deepcopy(binding)
    changed["config"]["state_dir"] += "-wrong"
    with pytest.raises(helper.ClientError) as error:
        client.validate_binding(changed)
    assert error.value.code == "BINDING_CONFIG_MISMATCH"
    changed = copy.deepcopy(binding)
    changed["intent_ref"]["sha256"] = "0" * 64
    with pytest.raises(helper.ClientError) as error:
        client.validate_binding(changed)
    assert error.value.code == "BINDING_IDENTITY_CHANGED"
    assert helper.Client(client.config).validate_binding(binding)["ok"]


def test_serialized_request_roundtrip_and_payload_conflict(caller, tmp_path):
    client, _, _ = caller
    binding = bound_run(caller)
    result = client.make_request("observe", {"data": {"note": "decision"}}, action_id="note-1", binding=binding)
    envelope = result["request"]
    path = tmp_path / "exact-request.json"
    helper.write_new_json(path, envelope)
    first = client.call(helper.read_json(str(path)))
    assert first["ok"] and client.call(helper.read_json(str(path))) == first
    changed = client.make_request("observe", {"data": {"note": "another decision"}}, action_id="note-1", binding=binding)["request"]
    assert changed["request_id"] == envelope["request_id"]
    assert client.call(changed)["error"]["code"] == "REQUEST_CONFLICT"
    with pytest.raises(FileExistsError):
        helper.write_new_json(path, changed)


def test_request_creation_is_local_and_execution_uses_one_guarded_process(caller, monkeypatch):
    client, _, _ = caller
    binding = bound_run(caller)
    original = client.execute
    calls = []
    def counted(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)
    monkeypatch.setattr(client, "execute", counted)
    envelope = client.make_request("status", {}, binding=binding)["request"]
    assert calls == []
    assert envelope["context"]["intent_ref"] == binding["intent_ref"]
    assert client.call(envelope)["ok"] and calls == ["invoke"]


def test_helper_context_first_restore_then_empty_delta(caller):
    client, _, _ = caller
    binding = bound_run(caller)
    response = client.context(binding)
    assert response["delivery"] == "full"
    while response["next_page"]:
        response = client.context(binding, page=response["next_page"])
    delta = client.context(binding, after=response["next_cursor"])
    assert delta["delivery"] == "delta" and delta["items"] == []
    assert "next_cursor" not in binding


def test_checkpoint_is_mechanical_idempotent_and_does_not_finish(caller):
    client, workspace, _ = caller
    binding = bound_run(caller, baseline=True)
    (workspace / "payload.txt").write_text("ready")
    first = client.checkpoint(binding, "change-1", compare_baseline=True, wait_seconds=15)
    assert first["ok"] and first["wait_status"] == "terminal"
    assert first["job"]["result"]["status"] == "passed"
    assert first["job"]["result"]["baseline_comparison"]["changes"][0]["status"] == "improved"
    (workspace / "payload.txt").write_text("later edit")
    replay = client.checkpoint(binding, "change-1", compare_baseline=True, wait_seconds=15)
    assert replay["job"]["job_id"] == first["job"]["job_id"]
    state = client.validate_binding(binding)
    assert state["history_counts"]["jobs"] == 1 and state["usage"]["verification_attempts"] == 1
    assert state["run"]["status"] == "active" and state["assessment"]["status"] == "not_assessed"
    assert state["history_counts"]["assessments"] == 0


def test_checkpoint_response_loss_replays_original_submission(caller, monkeypatch):
    client, workspace, _ = caller
    binding = bound_run(caller)
    original = client.execute
    lost = []
    def lose_after_submit(operation, *args, **kwargs):
        result = original(operation, *args, **kwargs)
        if operation == "invoke" and args[0]["request"]["operation"] == "checkpoint" and not lost:
            lost.append(result["submitted_candidate_hash"])
            raise helper.ClientError("CALL_OUTCOME_UNKNOWN", "simulated lost response after commit")
        return result
    monkeypatch.setattr(client, "execute", lose_after_submit)
    with pytest.raises(helper.ClientError, match="lost response"):
        client.checkpoint(binding, "exact-action", wait_seconds=0)
    (workspace / "payload.txt").write_text("ready")
    result = client.checkpoint(binding, "exact-action", wait_seconds=15)
    assert result["submitted_candidate_hash"] == lost[0]
    assert result["job"]["result"]["status"] == "failed"
    assert client.validate_binding(binding)["history_counts"]["jobs"] == 1


def test_checkpoint_refuses_superseding_candidate(caller, monkeypatch):
    from harness_external.dispatch import invoke
    from harness_external.service import Harness
    from harness_external.errors import HarnessError
    client, workspace, _ = caller
    binding = bound_run(caller)
    original = client.execute
    api = Harness(client.config["state_dir"])
    original_submit = api.submit
    def supersede(*args, **kwargs):
        submitted = original_submit(*args, **kwargs)
        (workspace / "payload.txt").write_text("ready")
        Harness(client.config["state_dir"]).submit(binding["run_id"], "external-submit")
        return submitted
    monkeypatch.setattr(api, "submit", supersede)
    def interleave(operation, *args, **kwargs):
        if operation == "invoke" and args[0]["request"]["operation"] == "checkpoint":
            try:
                return {"ok": True, **invoke(api, args[0]["request"])}
            except HarnessError as error:
                return {"ok": False, "error": {"code": error.code}}
        return original(operation, *args, **kwargs)
    monkeypatch.setattr(client, "execute", interleave)
    result = client.checkpoint(binding, "change", wait_seconds=0)
    assert not result["ok"] and result["error"]["code"] == "CANDIDATE_SUPERSEDED"
    assert client.validate_binding(binding)["history_counts"]["jobs"] == 0


def test_wait_window_does_not_cancel_restart_or_call_a_model(caller, monkeypatch):
    from harness_external.dispatch import waiting
    client, _, _ = caller
    binding = bound_run(caller)
    calls = []
    core_calls = []
    class RunningAPI:
        def status(self, *args, **kwargs):
            core_calls.append("status")
            return {"job": {"status": "running", "job_id": "controlled-job"}}
    def running(operation, arguments, *args):
        calls.append(operation)
        assert arguments["request"]["operation"] == "wait"
        return {"ok": True, **waiting(RunningAPI(), binding["run_id"], "controlled-job", 0)}
    monkeypatch.setattr(client, "execute", running)
    result = client.wait(binding, "controlled-job", wait_seconds=0, validated=True)
    assert result["wait_status"] == "pending" and calls == ["invoke"] and core_calls == ["status"]


def test_transport_lock_serializes_mutations_without_blocking_reads(caller):
    client, workspace, _ = caller
    binding = bound_run(caller)
    other = helper.Client(client.config)
    with client.serialized(binding["run_id"]):
        with pytest.raises(helper.ClientError) as error:
            other.call(request(workspace, binding["run_id"], "submit", "concurrent"))
        assert error.value.code == "CALLER_BUSY"
        assert other.validate_binding(binding)["ok"]
    assert other.call(request(workspace, binding["run_id"], "submit", "concurrent"))["ok"]


def test_discovery_never_selects_the_newest_run(caller):
    client, workspace, _ = caller
    first = client.call(start_request(workspace, "task-one"))["run_id"]
    second = client.call(start_request(workspace, "task-two"))["run_id"]
    found = client.find(str(workspace), "develop")
    assert {item["run_id"] for item in found["matches"]} == {first, second}
    assert found["selected_run_id"] is None and not found["truncated"]
    assert client.find(str(workspace), "develop", "different goal")["matches"] == []


def test_discovery_reports_truncation_instead_of_claiming_unique_match(caller, monkeypatch):
    client, workspace, _ = caller
    row = {"workspace": str(workspace), "domain_id": "develop", "goal": "same", "run_id": "one"}
    monkeypatch.setattr(client, "execute", lambda *a, **k: {"ok": True, "items": [row], "next_offset": 100})
    found = client.find(str(workspace), "develop", max_pages=1)
    assert found["truncated"] and found["selected_run_id"] is None


def test_installed_copy_and_fresh_process_resume_the_same_state(caller, tmp_path):
    client, workspace, config = caller
    installed = Path(installer.install(tmp_path / "host-skills")["skill"])
    assert (installed / "SKILL.md").is_file()
    assert (installed / "references" / "protocol.md").is_file()
    moved_helper = installed / "scripts" / "harness_client.py"
    code, started = invoke_cli(moved_helper, config, start_request(workspace), tmp_path)
    assert code == 0, started
    run = started["run_id"]
    code, state = invoke_cli(moved_helper, config, request(workspace, run, "resume"), ROOT)
    assert code == 0 and state["run"]["run_id"] == run
    assert state["run"]["workspace"] == str(workspace)
    assert state["candidate"] is None
    with pytest.raises(FileExistsError):
        installer.install(tmp_path / "host-skills")
    assert client.call(request(workspace, run, "resume"))["history_counts"]["jobs"] == 0


def test_timeout_does_not_automatically_retry_or_claim_verification_failed(caller, monkeypatch):
    client, workspace, _ = caller
    calls = []
    envelopes = []
    def timeout(argv, **options):
        calls.append(argv)
        source = next(item.split("=", 1)[1] for item in argv if item.startswith("--request="))
        envelopes.append(json.loads(Path(source).read_text()))
        raise subprocess.TimeoutExpired(argv, 1)
    monkeypatch.setattr(helper.subprocess, "run", timeout)
    with pytest.raises(helper.ClientError) as error:
        client.call(start_request(workspace, "keep-this-id"))
    assert error.value.code == "CALL_OUTCOME_UNKNOWN"
    assert len(calls) == 1 and envelopes[0]["request_id"] == "keep-this-id"


def test_older_core_is_reported_without_unguarded_fallback(caller, monkeypatch):
    client, workspace, _ = caller
    calls = []
    def unsupported(argv, **options):
        calls.append(argv)
        options["stderr"].write(b"argument command: invalid choice: 'invoke' (choose from 'start', 'resume')")
        return subprocess.CompletedProcess(argv, 2)
    monkeypatch.setattr(helper.subprocess, "run", unsupported)
    with pytest.raises(helper.ClientError) as error:
        client.call(start_request(workspace))
    assert error.value.code == "CORE_UPGRADE_REQUIRED" and len(calls) == 1


def test_failure_repair_resubmission_and_closeout_are_preserved(caller):
    client, workspace, _ = caller
    run = client.call(start_request(workspace))["run_id"]
    first_candidate = client.call(request(workspace, run, "submit", "submit-1"))["candidate"]
    first = client.call(request(workspace, run, "verify", "verify-1"))
    assert client.call(request(workspace, run, "verify", "verify-1")) == first
    assert wait_job(client, workspace, run, first["job_id"])["result"]["status"] == "failed"
    refused = client.call(request(workspace, run, "finish", "finish-too-soon", outcome="completed"))
    assert not refused["ok"]
    (workspace / "payload.txt").write_text("ready", encoding="utf-8")
    # New process/client state does not reset Core budgets or failed measurements.
    client = helper.Client(client.config)
    second_candidate = client.call(request(workspace, run, "submit", "submit-2"))["candidate"]
    assert second_candidate["candidate_hash"] != first_candidate["candidate_hash"]
    second = client.call(request(workspace, run, "verify", "verify-2"))
    assert wait_job(client, workspace, run, second["job_id"])["result"]["status"] == "passed"
    records = client.call(request(workspace, run, "records", kind="measurements", limit=10))
    assert [row["comparison_status"] for row in records["items"]] == ["failed", "passed"]
    observed = records["items"][-1]["observation_id"]
    assessment = {"interpretation_revision": 1, "status": "satisfied", "summary": "Measured ready bytes", "uncertainties": [],
                  "cited_observation_ids": [observed]}
    assessed = client.call(request(workspace, run, "assess", "assess-1", data=assessment))
    assert assessed["assessment"]["citation_summary"] == {"current": 1, "contextual": 0}
    finished = client.call(request(workspace, run, "finish", "finish-1", outcome="completed"))
    assert finished["resolution"]["display_status"] == "closed_checks_passed"
    assert finished["record"]["ready"] is False and finished["record"]["signature"] is None
    state = client.call(request(workspace, run, "status", check_workspace=True))
    assert state["current_workspace_matches_submitted_files"]
    assert state["history_counts"]["jobs"] == 2
    assert state["usage"]["verification_attempts"] == 2


@pytest.mark.skipif(os.environ.get("BASE_HARNESS_EXTERNAL_LIVE") != "1", reason="opt-in real namespace Sandbox")
def test_live_skill_client_executes_existing_sandbox(caller):
    client, workspace, _ = caller
    doctor = client.call({"operation": "doctor", "arguments": {"sandbox": True}})
    assert doctor["healthy"] and doctor["command_execution_verified"], doctor
    (workspace / "test_behavior.py").write_text("import unittest\nclass Behavior(unittest.TestCase):\n    def test_value(self):\n        self.assertEqual(6 * 7, 42)\n")
    payload = start_request(workspace)
    payload["arguments"]["required_check"] = ["domain.behavior"]
    payload["arguments"]["parameters"] = {
        "profile": "execution", "inputs": ["test_behavior.py"], "artifacts": ["test_behavior.py"],
        "expectations": [{"id": "test-file", "path": "test_behavior.py", "operator": "contains", "expected": "unittest"}],
        "test_commands": [{"id": "behavior", "argv": ["python3", "-m", "unittest", "discover", "-v"], "timeout_seconds": 30}]}
    run = client.call(payload)["run_id"]
    binding = client.bind(run, str(workspace), "develop", workspace.parent / "live-binding.json")["binding"]
    context = client.context(binding)
    while context["next_page"]:
        context = client.context(binding, page=context["next_page"])
    verified = client.checkpoint(binding, "live-checkpoint", wait_seconds=30)
    assert verified["wait_status"] == "terminal" and verified["job"]["result"]["status"] == "passed"
    job = verified["job"]["job_id"]
    delta = client.context(binding, after=context["next_cursor"])
    assert delta["critical"]["measurement_status"] == "passed" and delta["critical"]["gate_status"] == "passed"
    facts = client.call(request(workspace, run, "records", kind="measurements", job_id=job, limit=10))["items"]
    command = next(row for row in facts if row["check_id"] == "domain.behavior")
    assert command["report"]["sandbox"]["backend"] == "namespace"
    assert command["report"]["sandbox"]["network"] == "loopback_only"
    assert command["origin"] == "verifier"
    assert client.call(request(workspace, run, "finish", "finish-live", outcome="completed"))["record"]["ready"] is False
