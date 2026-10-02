"""Evaluator-only tests; never call a model or import candidate implementations."""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("hard_queue_tests", ROOT / "scripts/hard_queue_study.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def test_hard_queue_task_has_frozen_state_and_concurrency_scope():
    task = runner.definition.TASK
    assert len(task["cases"]) == len({c["name"] for c in task["cases"]}) == 36
    assert sum(c["request"]["operation"] == "concurrent" for c in task["cases"]) == 5
    assert {"finish-at-exact-deadline", "replay-original-claim-after-completion",
            "expiry-and-transitive-blocking", "canonical-objects-but-distinct-number-types"} <= {c["name"] for c in task["cases"]}
    profile = runner.profile(task)
    assert profile["artifacts"] == list(task["sources"])
    assert profile["test_commands"][0]["argv"] == ["python3", "-m", "unittest", "discover", "-s", "acceptance_tests", "-v"]
    assert runner.study.MAX_SECONDS == 900 and runner.study.REPAIR_SECONDS == 300
    assert runner.study.HOSTS["codex"] == {"model": "gpt-6-luna", "effort": "medium"}


def test_hard_queue_plan_rejects_extra_or_different_conditions(monkeypatch, tmp_path):
    plan = {"hard_wrapper_hash": runner.study.digest(runner.HERE.read_bytes()),
            "trials": [{"host": "codex", "arm": "plain"}, {"host": "agy", "arm": "after"}]}
    monkeypatch.setattr(runner, "original_validate", lambda root: plan)
    assert runner.validate(tmp_path) is plan
    plan["trials"].append({"host": "codex", "arm": "after"})
    with pytest.raises(ValueError, match="only Luna ordinary"):
        runner.validate(tmp_path)
    plan["trials"].pop()
    plan["hard_wrapper_hash"] = "changed"
    with pytest.raises(ValueError, match="controller changed"):
        runner.validate(tmp_path)


def test_hard_queue_driver_has_no_expected_answers():
    driver = (ROOT / "evaluation/hard_queue/driver.py").read_text()
    assert 'request["expected"]' not in driver and 'request["cases"]' not in driver
    assert '"__STUDY_OBSERVATIONS__"' in driver
    # The operator's corpus is hidden; only the inherited sandbox grader copies
    # each case's request, not the reference output, into the subject workspace.
    assert '[item["request"] for item in task["cases"]]' in (ROOT / "scripts/agent_study.py").read_text()
