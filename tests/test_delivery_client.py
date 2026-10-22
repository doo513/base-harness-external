"""Field inputs and hash-bound reuse through the portable caller and real Core."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "skills/harness-workflow/scripts/harness_client.py"
spec = importlib.util.spec_from_file_location("delivery_client", HELPER)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


@pytest.fixture
def client(tmp_path):
    work = tmp_path / "작업 space"
    work.mkdir()
    (work / "main.py").write_text("print('실제 실행')\n", encoding="utf-8")
    config_path = tmp_path / "client.json"
    configuration = helper.configure(config_path, str(ROOT), str(tmp_path / "state"), ephemeral=True)["config"]
    caller = helper.Client(configuration)
    return caller, work, config_path


def cli(config, *args):
    process = subprocess.run([sys.executable, str(HELPER), "--config", str(config), *map(str, args)],
                             capture_output=True, encoding="utf-8", timeout=60)
    assert process.stdout, process.stderr
    return process.returncode, json.loads(process.stdout)


def start(client, tmp_path, *, command=False):
    caller, work, _ = client
    params = {"profile": "execution" if command else "structural", "inputs": ["main.py"], "artifacts": ["main.py"],
              "expectations": [{"id": "entry", "path": "main.py", "operator": "contains", "expected": "실제 실행"}]}
    if command:
        params["execution_checks"] = [{"kind": "command", "id": "run", "argv": ["python3", "main.py"], "timeout_seconds": 20}]
    binding_path = tmp_path / "binding.json"
    result = caller.call_operation("start", {"domain": "develop", "workspace": str(work), "goal": "원문 목표\n",
        "mode": "exploratory", "provenance": {"declared_author": "model"}, "parameters": params,
        "required_check": ["domain.run"] if command else ["domain.entry"]},
        action_id="start", output=tmp_path / "start.json", binding_output=binding_path)
    assert result["ok"], result
    return helper.read_json(str(binding_path)), binding_path, params


def test_field_builder_preserves_text_unicode_empty_null_and_extensions(client, tmp_path):
    caller, _, _ = client
    text = '한글 "quote" \\slash\r\n${not_a_command}\tend'
    source = tmp_path / "source.txt"
    source.write_bytes(text.encode("utf-8"))
    arguments, refs = helper.field_arguments(caller, "observe", {}, text_files=["/data/payload/fields/body=" + str(source)],
        texts=["/data/payload/fields/empty=", "/data/payload/extensions/a~1b~0c=extra"],
        json_fields=["/data/payload/fields/list=[true,0,null]"], nulls=["/data/payload/fields/unknown"])
    assert refs is None
    assert arguments == {"data": {"note": "", "payload": {"fields": {"body": text, "empty": "", "list": [True, 0, None], "unknown": None},
                                                          "extensions": {"a/b~c": "extra"}}}}


def test_start_fields_preserve_original_text_and_blank_analysis_does_not_admit_execution(client, tmp_path):
    caller, work, config = client
    goal = tmp_path / "request.txt"
    raw = "원문 그대로\r\nSecond line: \"quoted\"\r\n".encode("utf-8")
    goal.write_bytes(raw)
    binding_path = tmp_path / "binding.json"
    code, result = cli(config, "call", "--operation", "start", "--action-id", "start-fields",
        "--text", "/domain=develop", "--text", "/workspace=" + str(work), "--text", "/mode=exploratory",
        "--text", "/provenance/declared_author=model", "--text-file", "/goal=" + str(goal),
        "--binding-output", binding_path, "--format", "compact")
    assert code == 0, result
    binding = helper.read_json(str(binding_path))
    resumed = caller.call(caller.make_request("resume", {}, binding=binding)["request"])
    assert resumed["intent"]["original_goal"].encode("utf-8") == raw
    code, note = cli(config, "call", "--binding", binding_path, "--operation", "observe", "--action-id", "blank",
                     "--null", "/data/payload/fields/verification")
    assert code == 0, note
    finish = caller.call_operation("finish", {"outcome": "completed"}, binding=binding, binding_path=binding_path, action_id="early")
    assert not finish["ok"] and finish["error"]["code"] == "VERIFICATION_REQUIRED"


@pytest.mark.parametrize("changes", [
    {"texts": ["/data/note=one", "/data/note=two"]},
    {"texts": ["/data=one", "/data/note=two"]},
    {"texts": ["/data/note=one", "/data=two"]},
    {"texts": ["no-pointer=value"]}, {"texts": ["/bad~9=value"]},
    {"json_fields": ["/data={\"note\":1,\"note\":2}"]}, {"json_fields": ["/data=NaN"]},
])
def test_ambiguous_field_inputs_do_not_call_core(client, changes):
    caller, _, _ = client
    with patch.object(caller, "call", side_effect=AssertionError("no Core call")), pytest.raises(helper.ClientError):
        helper.field_arguments(caller, "observe", {}, **changes)


def test_actual_cli_record_reuse_and_fresh_session(client, tmp_path):
    caller, _, config = client
    binding, path, _ = start(client, tmp_path)
    receipt = tmp_path / "prior.json"
    code, first = cli(config, "call", "--binding", path, "--operation", "observe", "--action-id", "note",
                      "--text", "/data/payload/fields/summary=필드를 그대로 전달",
                      "--null", "/data/payload/fields/unresolved", "--text", "/data/payload/extensions/idea=추가 정보",
                      "--response-output", receipt)
    assert code == 0, first
    assert first["observation"]["payload"]["fields"]["unresolved"] is None
    code, revised = cli(config, "call", "--binding", path, "--operation", "revise", "--action-id", "reuse",
                        "--json", "/data/expected_revision=1", "--from-record", "/data/goal_summary=" + str(receipt) + "#/field_refs/summary")
    assert code == 0, revised
    saved = helper.read_json(revised["caller_request"]["request_file"])
    assert saved["input_bindings"][0]["source"] == first["field_refs"]["summary"]
    assert saved["arguments"]["data"]["goal_summary"] == "필드를 그대로 전달"
    receipt.write_text("{}", encoding="utf-8")  # replay never re-reads caller files
    code, replay = cli(config, "call", "--request", revised["caller_request"]["request_file"])
    assert code == 0 and replay["interpretation"]["ref"] == revised["interpretation"]["ref"]
    code, resumed = cli(config, "context", "--binding", path, "--complete")
    assert code == 0, resumed
    restored = helper.expand_context(resumed)
    assert restored["field:intent"]["original_goal"] == "원문 목표\n"
    assert restored["observations:0"]["payload"]["fields"]["summary"] == "필드를 그대로 전달"
    assert restored["interpretations:1"]["goal_summary"] == "필드를 그대로 전달"
    assert restored["interpretations:1"]["input_bindings"] == saved["input_bindings"]


def test_binding_and_saved_intent_conflicts_do_not_create_another_operation(client, tmp_path):
    caller, _, config = client
    binding, path, _ = start(client, tmp_path)
    code, first = cli(config, "call", "--binding", path, "--operation", "observe", "--action-id", "note", "--text", "/data/note=first")
    assert code == 0
    code, second = cli(config, "call", "--binding", path, "--operation", "observe", "--action-id", "note", "--text", "/data/note=changed")
    assert code == 2 and second["error"]["code"] == "SAVED_REQUEST_CONFLICT"
    notes = caller.call(caller.make_request("records", {"kind": "notes"}, binding=binding)["request"])
    assert notes["total"] == 1


def test_unknown_response_replays_original_binding_exactly(client, tmp_path):
    caller, _, _ = client
    binding, path, _ = start(client, tmp_path)
    first = caller.call_operation("observe", {"data": {"note": "", "payload": {"fields": {"summary": "same"}}}},
                                   binding=binding, binding_path=path, action_id="note")
    record = tmp_path / "record.json"
    helper.write_new_json(record, first)
    args, refs = helper.field_arguments(caller, "revise", {"data": {"expected_revision": 1}}, binding=binding,
                                       from_records=["/data/goal_summary=" + str(record) + "#/field_refs/summary"])
    real_call = caller.call
    def lost(request):
        result = real_call(request)
        assert result["ok"], result
        raise helper.ClientError("CALL_OUTCOME_UNKNOWN", "injected response loss after commit")
    with patch.object(caller, "call", side_effect=lost), pytest.raises(helper.ClientError) as caught:
        caller.call_operation("revise", args, binding=binding, binding_path=path, action_id="reuse", input_bindings=refs)
    saved = caught.value.saved_request["request_file"]
    result = helper.Client(caller.config).call(helper.read_json(saved))
    assert result["ok"] and result["input_bindings"] == refs
    history = caller.call(caller.make_request("records", {"kind": "interpretations"}, binding=binding)["request"])
    assert history["total"] == 2


def test_response_file_failure_does_not_claim_operation_rollback(client, tmp_path):
    caller, _, config = client
    binding, path, _ = start(client, tmp_path)
    receipt = tmp_path / "existing.json"
    receipt.write_text('{"do":"not overwrite"}', encoding="utf-8")
    code, result = cli(config, "call", "--binding", path, "--operation", "observe", "--action-id", "note",
                       "--text", "/data/note=committed", "--response-output", receipt)
    assert code == 0 and result["ok"]
    assert result["response_output"]["status"] == "failed"
    assert receipt.read_text(encoding="utf-8") == '{"do":"not overwrite"}'
    replay = caller.call(helper.read_json(result["caller_request"]["request_file"]))
    assert replay["observation"]["note"] == "committed"


@pytest.mark.skipif(os.environ.get("BASE_HARNESS_EXTERNAL_LIVE") != "1", reason="opt-in strict Sandbox")
def test_field_reference_to_domain_to_worker_verifier_over_external_socket(client, tmp_path):
    from harness_external.local_transport import HarnessServer
    from harness_external.service import Harness
    caller, work, _ = client
    api = Harness(caller.config["state_dir"])
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    with HarnessServer(api, runtime / "h.sock", [work]) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            caller = helper.Client({**caller.config, "server_socket": str(server.socket_path)})
            binding, path, params = start((caller, work, None), tmp_path, command=True)
            first = caller.call_operation("observe", {"data": {"note": "", "payload": {"fields": {"parameters": params},
                         "extensions": {"passed": True}}}}, binding=binding, binding_path=path, action_id="proposal")
            receipt = tmp_path / "proposal.json"
            helper.write_new_json(receipt, first)
            arguments, refs = helper.field_arguments(caller, "revise", {"data": {"expected_revision": 1}}, binding=binding,
                                                   from_records=["/data/parameters=" + str(receipt) + "#/field_refs/parameters"])
            # The Host-side client is forbidden to spawn local Core or tools.
            with patch.object(helper.subprocess, "run", side_effect=AssertionError("No local fallback")):
                revised = caller.call_operation("revise", arguments, binding=binding, binding_path=path, action_id="criteria", input_bindings=refs)
                assert revised["ok"], revised
                assert revised["interpretation"]["parameters"] == params
                assert revised["interpretation"]["input_bindings"] == refs
                result = caller.checkpoint(binding, "measure", wait_seconds=30)
                assert result["ok"] and result["wait_status"] == "terminal", result
                facts = caller.call(caller.make_request("records", {"kind": "measurements", "job_id": result["job"]["job_id"]}, binding=binding)["request"])
                resume = caller.call(caller.make_request("resume", {}, binding=binding)["request"])
            command = next(item for item in facts["items"] if item["check_id"] == "domain.run")
            assert command["execution_status"] == "completed" and command["comparison_status"] == "passed"
            assert "실제 실행" in json.dumps(command, ensure_ascii=False)
            assert resume["gates"]["status"] == "passed"
            assert resume["assessment"]["status"] == "not_assessed"
            assert resume["intent"]["original_goal"] == "원문 목표\n"
            assert facts["total"] == 2  # only the actual file + command measurements
            assert not resume["ready"]
        finally:
            server.shutdown()
            thread.join(timeout=5)
