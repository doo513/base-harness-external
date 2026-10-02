"""Evaluator self-tests do not execute a commercial model."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import signal
import sys
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("study_test_module", ROOT / "scripts/agent_study.py")
study = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = study
spec.loader.exec_module(study)


def test_study_comparator_rejects_bool_and_missing_fields():
    assert not study.same({"value": 1}, {"value": True})
    assert not study.same([1, 2], [1])
    assert not study.same({"value": 1}, {"value": 1, "invented": True})
    assert study.same({"seconds": 1.0}, {"seconds": 1})


def test_study_native_agy_structured_claim_and_raw_usage(tmp_path):
    path = tmp_path / "trace.jsonl"
    events = [{"event": "init", "conversation_id": "one"},
              {"event": "step_update", "step_update": {"step_type": "tool", "state": "DONE", "step_index": 1}},
              {"event": "result", "result": {"status": "SUCCESS", "response": "Prose preceding JSON",
               "structured_output": {"status": "partial"}, "usage": {"input_tokens": 2, "cache_read_tokens": 100}}}]
    path.write_text("\n".join(json.dumps(event) for event in events))
    parsed = study.parse_trace("agy", path)
    assert parsed["claim"] == {"status": "partial"}
    assert parsed["tool_calls"] == 1 and parsed["usage"] == {"input_tokens": 2, "cache_read_tokens": 100}


def test_future_studies_use_only_luna_and_agy(tmp_path):
    assert set(study.HOSTS) == {"codex", "agy"}
    for host, model in (("codex", "gpt-6-luna"), ("agy", "gemini-3.8-flash-high")):
        argv = study.cli_argv(host, tmp_path, tmp_path, "controlled task")
        assert model in argv
        assert "gpt-6-astra" not in argv
        if host == "codex":
            assert argv[argv.index("-c") + 1] == 'model_reasoning_effort="medium"'


def test_base_runner_stops_preexecution_failure_for_either_host():
    for error in ({"type": "turn.failed", "error": "quota"}, {"status": "ERROR", "message": "quota"}):
        with pytest.raises(RuntimeError, match="before task execution"):
            study.ensure_task_execution({"errors": [error], "tool_calls": 0, "claim": None})
    # A model which did execute tools can have a partial artifact; retain/scoring
    # semantics for that case are different from an unavailable provider.
    study.ensure_task_execution({"errors": [{"message": "later failure"}], "tool_calls": 1, "claim": None})
    study.ensure_task_execution({"errors": [], "tool_calls": 0, "claim": {"status": "blocked"}})


@pytest.mark.parametrize("stdout,expected", [("no measurements", False), ('__STUDY_OBSERVATIONS__[{"value":7}]\n', True)])
def test_study_grader_measures_only_sandbox_observations(tmp_path, monkeypatch, stdout, expected):
    private = tmp_path / "private"
    private.mkdir()
    (private / "subject_driver.py").write_text("# controlled test input")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.py").write_text("# never executed by this self-test")
    task = {"id": "controlled", "cases": [{"name": "one", "request": {"operation": "probe"}, "expected": {"value": 7}}]}
    calls = []
    def captured(argv, **kwargs):
        calls.append(argv)
        request = json.loads(kwargs["input"])
        assert argv[-1].endswith("external-harness-sandbox.ts")
        transported = json.loads((Path(request["workspace"]) / "_study_requests.json").read_text())
        assert transported == [{"operation": "probe"}]  # no expected answers in subject environment
        return subprocess.CompletedProcess(argv, 0, json.dumps({"status": "completed", "capture": {"exitCode": 0, "stdout": stdout}}), "")
    monkeypatch.setattr(study.subprocess, "run", captured)
    result = study.grade(tmp_path, task, workspace, tmp_path / "grade.json")
    assert result["all_passed"] is expected and len(calls) == 1
    assert result["total"] == 1


def test_study_never_overwrites_existing_run_directory(tmp_path):
    (tmp_path / "existing.txt").write_text("preserve")
    with pytest.raises(ValueError, match="never overwrite"):
        study.prepare(tmp_path)
    assert (tmp_path / "existing.txt").read_text() == "preserve"


def test_study_sources_and_test_cases_are_finite_and_explicit():
    tasks = study.tasks()
    assert len(tasks) == 3 and len({task["id"] for task in tasks}) == 3
    for task in tasks:
        assert len(task["sources"]) == 2
        assert len({case["name"] for case in task["cases"]}) == len(task["cases"]) >= 20
        assert len(study.profile(task)["test_commands"]) == 1
        assert set(study.profile(task)["artifacts"]) == set(task["sources"])
    assert study.MAX_SECONDS == 600 and study.REPAIR_SECONDS == 300


def test_driver_accepts_value_error_subclasses():
    spec = importlib.util.spec_from_file_location("study_driver_test", ROOT / "scripts/study_subject_driver.py")
    driver = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(driver)
    assert driver.error_kind(json.JSONDecodeError("bad", "x", 0)) == "ValueError"
    assert driver.error_kind(TypeError("wrong")) == "TypeError"


@pytest.mark.skipif(os.environ.get("BASE_HARNESS_EXTERNAL_LIVE") != "1", reason="opt-in actual user/PID namespaces")
def test_live_scope_terminates_namespace_and_new_process_group(tmp_path):
    spec = importlib.util.spec_from_file_location("scope_test", ROOT / "scripts/study_scope.py")
    scope = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scope)
    root = tmp_path / "study"
    trial = root / "trials/probe"
    for path in (root / "private", root / "receipts", trial / "workspace"):
        path.mkdir(parents=True, exist_ok=True)
    # Only hide a disposable stand-in, not the running test checkout.
    hidden = tmp_path / "hidden"
    hidden.mkdir()
    code = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import time; time.sleep(90)'],start_new_session=True); print('running',flush=True); time.sleep(90)"
    process = subprocess.Popen([sys.executable, str(ROOT / "scripts/study_scope.py"), str(root), str(trial), str(hidden), "python3", "-c", code],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    owned = {}
    try:
        until = time.monotonic() + 4
        while not owned and time.monotonic() < until:
            owned.update(scope.namespace_children(process.pid))
            time.sleep(.1)
        assert owned
        time.sleep(.3)
        os.killpg(process.pid, signal.SIGTERM)
        output, errors = process.communicate(timeout=8)
        events = [json.loads(line) for line in output.splitlines() if line.startswith('{')]
        assert process.returncode == 124, errors
        assert events[-1]["type"] == "study.scope.exit" and not events[-1]["remaining_namespace_pids"]
        assert all(scope.identity(pid) != token for pid, token in owned.items())
    finally:
        scope.terminate_owned(owned)
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
