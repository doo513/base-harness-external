"""Continuation configuration tests; no provider or candidate execution."""
import copy
import importlib.util
from pathlib import Path
import sys

import pytest


@pytest.fixture
def runner(monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("luna_queue_test", scripts / "luna_harness_queue_study.py")
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def test_continuation_restricts_model_and_workflow(runner, monkeypatch, tmp_path):
    plan = {"luna_harness_wrapper_hash": runner.study.digest(runner.HERE.read_bytes()),
            "hosts": {"codex": {"model": "gpt-6-luna", "effort": "medium"}},
            "trials": [{"host": "codex", "arm": "after"}]}
    monkeypatch.setattr(runner.hard, "original_validate", lambda root: plan)
    assert runner.validate(tmp_path) is plan
    plan["trials"][0]["arm"] = "plain"
    with pytest.raises(ValueError, match="Harness condition"):
        runner.validate(tmp_path)
    plan["trials"][0]["arm"] = "after"
    plan["hosts"]["codex"]["model"] = "other"
    with pytest.raises(ValueError, match="Only Luna"):
        runner.validate(tmp_path)


def test_continuation_prompt_requires_harness_without_previous_answers(runner, tmp_path):
    task = copy.deepcopy(runner.hard.definition.TASK)
    prompt = runner.study.task_prompt(tmp_path, tmp_path / "trial", {"arm": "after"}, task)
    assert task["spec"] in prompt and "Use mode acceptance" in prompt
    assert "Capture the pre-edit baseline" in prompt and "900 seconds" in prompt
    assert "fixed holdout failures" not in prompt
    assert runner.study.REPAIR_SECONDS == 300
