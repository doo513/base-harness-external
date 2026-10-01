"""Evaluation must fail closed when required coverage or source binding is lost."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.fixture
def evaluator():
    spec = importlib.util.spec_from_file_location("closeout_evaluate", Path(__file__).resolve().parents[1] / "scripts/evaluate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("state,required,code,changed,expected", [
    ("passed", True, 0, False, 0),
    ("skipped", True, 0, False, 1),
    ("skipped", False, 0, False, 0),
    ("missing", True, 0, False, 1),
    ("failed", True, 1, False, 1),
    ("passed", True, 0, True, 1),
])
def test_evaluation_exit_and_source_binding(evaluator, tmp_path, monkeypatch, capsys, state, required, code, changed, expected):
    repository = tmp_path / "checkout"
    (repository / "scripts").mkdir(parents=True)
    (repository / "evaluation").mkdir()
    manifest = {"schema_version": "test", "scope": "controlled runner test",
                "scenarios": [{"id": "case", "test": "test_case", "required": required}]}
    (repository / "evaluation/closeout-scenarios.json").write_text(json.dumps(manifest))
    monkeypatch.setattr(evaluator, "__file__", str(repository / "scripts/evaluate.py"))
    monkeypatch.setattr(sys, "argv", ["evaluate.py"])
    digests = iter(["a" * 64, ("b" if changed else "a") * 64])
    monkeypatch.setattr(evaluator, "source_digest", lambda _: next(digests))
    def controlled_run(argv, **kwargs):
        if argv[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(argv, 0, "c" * 40 + "\n", "")
        if argv[:2] == ["git", "status"]:
            return subprocess.CompletedProcess(argv, 0, " M src/example.py\n", "")
        assert "--junitxml" in argv
        xml = Path(argv[argv.index("--junitxml") + 1])
        child = {"passed": "", "failed": "<failure/>", "skipped": "<skipped/>"}.get(state, "")
        case = "" if state == "missing" else '<testcase name="test_case" time="0">' + child + '</testcase>'
        xml.write_text("<testsuites><testsuite>" + case + "</testsuite></testsuites>")
        return subprocess.CompletedProcess(argv, code, "controlled pytest output", "")
    monkeypatch.setattr(evaluator.subprocess, "run", controlled_run)
    assert evaluator.main() == expected
    report = json.loads(capsys.readouterr().out)
    assert report["passed"] is (expected == 0)
    assert report["dirty"] is True and report["head_commit"] == "c" * 40
    assert report["tested_tree_digest"] == "a" * 64
    assert report["source_unchanged"] is (not changed)


def test_source_digest_covers_tests_but_excludes_generated_reports(evaluator, tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "evaluation").mkdir()
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    (tmp_path / "evaluation/closeout-scenarios.json").write_text("{}")
    test = tmp_path / "tests/test_boundary.py"
    test.write_text("assert True\n")
    before = evaluator.source_digest(tmp_path)
    (tmp_path / "evaluation/latest.json").write_text('{"passed": true}')
    assert evaluator.source_digest(tmp_path) == before
    test.write_text("assert False\n")
    assert evaluator.source_digest(tmp_path) != before


def test_source_digest_covers_distributed_skill_and_installer(evaluator, tmp_path):
    (tmp_path / "evaluation").mkdir()
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    (tmp_path / "evaluation/closeout-scenarios.json").write_text("{}")
    (tmp_path / "skills/harness-workflow/scripts").mkdir(parents=True)
    (tmp_path / "scripts").mkdir()
    for relative in ("skills/harness-workflow/SKILL.md", "skills/harness-workflow/scripts/harness_client.py",
                     "scripts/install-skill.py", "INSTALL.md"):
        before = evaluator.source_digest(tmp_path)
        (tmp_path / relative).write_text("initial")
        assert evaluator.source_digest(tmp_path) != before
        before = evaluator.source_digest(tmp_path)
        (tmp_path / relative).write_text("changed")
        assert evaluator.source_digest(tmp_path) != before
