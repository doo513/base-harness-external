import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location("study_report_test", Path(__file__).resolve().parents[1] / "scripts/report_agent_study.py")
reporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reporter)


def test_artifact_audit_detects_changes_and_added_importable_files(tmp_path):
    path = tmp_path / "main.py"
    path.write_text("original")
    hashes = {"main.py": hashlib.sha256(path.read_bytes()).hexdigest()}
    assert reporter.artifact_matches(tmp_path, hashes)
    (tmp_path / "extra.py").write_text("changed dependency")
    assert not reporter.artifact_matches(tmp_path, hashes)


def test_policy_audit_includes_behavior_not_just_presence():
    specs = reporter.expected_specs({"sources": {"settings.py": "", "settings_cli.py": ""}})
    assert set(specs) == {"domain.entry0", "domain.entry1", "domain.behavior"}
    assert specs["domain.behavior"]["parameters"] == {
        "kind": "command", "argv": ["python3", "-m", "unittest", "discover", "-s", "acceptance_tests", "-v"],
        "cwd": ".", "expectedExitCode": 0}


def test_report_requires_each_of_the_nine_conditions_once():
    tasks = ["settings", "journal", "planner"]
    trials = [{"id": f"codex__{task}__{arm}", "host": "codex", "task": task, "arm": arm}
              for task in tasks for arm in ("plain", "before", "after")]
    assert reporter.selected_trials({"trials": trials}, "codex", tasks) == trials
    for malformed in ([], trials[:-1], trials[:-1] + [trials[0]]):
        with pytest.raises(ValueError, match="nine unique"):
            reporter.selected_trials({"trials": malformed}, "codex", tasks)


def test_report_rechecks_frozen_files_not_only_manifest_equality(tmp_path):
    for name in ("sources/after", "private", "policies", "receipts"):
        (tmp_path / name).mkdir(parents=True)
    source = tmp_path / "sources/after/module.py"
    source.write_text("original")
    (tmp_path / "scope.py").write_text("scope")
    for name in ("controls.json", "qualification.json"):
        (tmp_path / "receipts" / name).write_text('{"passed":true}')
    plan = {"source_files": {"after": {"module.py": hashlib.sha256(source.read_bytes()).hexdigest()}},
            "definition_hashes": {}, "policy_hashes": {}, "scope_hash": hashlib.sha256(b"scope").hexdigest()}
    (tmp_path / "plan.json").write_text(json.dumps(plan))
    (tmp_path / "plan.sha256").write_text(hashlib.sha256((tmp_path / "plan.json").read_bytes()).hexdigest())
    assert reporter.validate_frozen_inputs(tmp_path) == plan
    source.write_text("changed after scoring")
    with pytest.raises(ValueError, match="Frozen runtime changed"):
        reporter.validate_frozen_inputs(tmp_path)
