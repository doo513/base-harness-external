"""Use the actual portable CLI, not only Python helper methods."""
import json
from pathlib import Path
import subprocess
import sys

from test_skill_workflow import helper, caller, bound_run, HELPER


def test_default_checkpoint_cli_is_brief_saves_request_and_optional_values(caller, tmp_path):
    client, workspace, config = caller
    binding = bound_run(caller)
    binding_path = tmp_path / "lean-binding.json"
    helper.write_new_json(binding_path, binding)
    (workspace / "payload.txt").write_text("ready", encoding="utf-8")
    payload = {"fields": {"unresolved": None}, "extensions": {"platform": {"new_field": [False, 0, ""]}}}
    payload_path = tmp_path / "payload.json"
    payload_path.write_text(json.dumps(payload), encoding="utf-8")
    argv = [sys.executable, str(HELPER), "--config", str(config), "checkpoint", "--binding", str(binding_path),
            "--action-id", "one-checkpoint", "--payload", str(payload_path), "--wait-seconds", "30"]
    process = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", timeout=45)
    assert process.returncode == 0, process.stdout + process.stderr
    result = json.loads(process.stdout)
    assert result["view"] == "brief" and result["gates"]["status"] == "passed"
    assert result["selected_job"]["relevance"] == "current"
    saved = helper.read_json(result["caller_request"]["request_file"])
    assert saved["arguments"]["payload"] == payload
    payload_path.write_text('{"extensions":{"different":true}}', encoding="utf-8")
    replay = subprocess.run([sys.executable, str(HELPER), "--config", str(config), "call", "--request", result["caller_request"]["request_file"]],
                            capture_output=True, text=True, encoding="utf-8", timeout=45)
    again = json.loads(replay.stdout)
    assert replay.returncode == 0 and again["selected_job"]["job_id"] == result["selected_job"]["job_id"]
    notes = client.call_operation("records", {"kind": "notes"}, binding=binding)["items"]
    assert len(notes) == 1 and notes[0]["payload"] == {**payload, "parents": []}
    assert json.dumps(notes[0]["payload"], sort_keys=True) == json.dumps({**payload, "parents": []}, sort_keys=True)
    state = client.validate_binding(binding)
    assert state["history_counts"]["jobs"] == 1 and state["history_counts"]["assessments"] == 0
    assert state["run"]["status"] == "active"


def test_checkpoint_error_reports_committed_note_separately(caller, tmp_path):
    client, workspace, _ = caller
    binding = bound_run(caller)
    (workspace / "payload.txt").unlink()
    result = client.checkpoint(binding, "missing-input", payload={"extensions": {"exception": "missing file"}},
                               binding_path=tmp_path / "binding.json", view="brief")
    assert not result["ok"]
    progress = result["checkpoint_progress"]
    assert progress["stage"] == "submit" and progress["caller_record"]["record_ref"]["run_id"] == binding["run_id"]
    assert Path(result["caller_request"]["request_file"]).is_file()
    state = client.validate_binding(binding)
    assert state["history_counts"]["notes"] == 1 and state["history_counts"]["jobs"] == 0
