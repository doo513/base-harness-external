"""Run the declared common-track scenarios and write a machine-readable report."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET


def source_digest(repository):
    sources = sorted(path for folder in ("src", "runtime", "tests", "scripts", "skills", "examples")
                     for path in (repository / folder).rglob("*")
                     if path.is_file() and (path.suffix in {".py", ".json", ".ts", ".md"} or path.name == "harness-tool"))
    sources += sorted((repository / ".github/workflows").glob("*.yml"))
    sources += [repository / "evaluation" / "closeout-scenarios.json", repository / "pyproject.toml"]
    requirements = repository / "evaluation/implementation-requirements.json"
    if requirements.is_file():
        sources.append(requirements)
    sources += sorted((repository / "evaluation").glob("*.py"))
    sources += sorted((repository / "evaluation/paired_references").rglob("*.py"))
    sources += [repository / name for name in ("README.md", "INSTALL.md", "CALLER_USAGE.md", "POLICIES.md") if (repository / name).is_file()]
    return hashlib.sha256(json.dumps({str(path.relative_to(repository)): hashlib.sha256(path.read_bytes()).hexdigest()
                                     for path in sources}, sort_keys=True).encode()).hexdigest()


def git_state(repository):
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repository, capture_output=True, text=True)
        state = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=repository, capture_output=True, text=True)
        return {"head_commit": head.stdout.strip() if head.returncode == 0 else None,
                "dirty": bool(state.stdout) if state.returncode == 0 else None}
    except OSError:
        return {"head_commit": None, "dirty": None}


def evaluation_passed(exit_code, outcomes, source_unchanged):
    return (exit_code == 0 and source_unchanged and bool(outcomes)
            and all(row["status"] == "passed" or (row["status"] == "skipped" and row.get("required") is False)
                    for row in outcomes))


def main():
    parser = argparse.ArgumentParser(description="Common Closeout conformance evaluation (no commercial-model benchmark)")
    parser.add_argument("--output", type=Path, help="optional JSON report destination")
    args = parser.parse_args()
    repository = Path(__file__).resolve().parents[1]
    suite = json.loads((repository / "evaluation" / "closeout-scenarios.json").read_text())
    tested_digest = source_digest(repository)
    source_state = git_state(repository)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="closeout-evaluation-") as temporary:
        results = Path(temporary) / "results.xml"
        execution = subprocess.run([sys.executable, "-m", "pytest", "-q", "tests/test_closeout_semantics.py",
                                    "tests/test_core_regressions.py", "tests/test_agent_workflow.py", "tests/test_protocol_v2.py",
                                    "tests/test_dogfood_regressions.py", "tests/test_skill_workflow.py", "tests/test_acceptance_policy.py", "tests/test_agent_study.py",
                                    "tests/test_luna_study.py", "tests/test_study_report.py", "--junitxml", str(results)],
                                   cwd=repository, capture_output=True, text=True)
        require_results = results.is_file()
        cases = {item.get("name"): item for item in ET.parse(results).iter("testcase")} if require_results else {}
        outcomes = []
        for scenario in suite["scenarios"]:
            case = cases.get(scenario["test"])
            state = "missing" if case is None else "failed" if case.find("failure") is not None or case.find("error") is not None else "skipped" if case.find("skipped") is not None else "passed"
            outcomes.append({**scenario, "status": state, "elapsed_seconds": float(case.get("time", "0")) if case is not None else None})
        final_digest = source_digest(repository)
        passed = evaluation_passed(execution.returncode, outcomes, tested_digest == final_digest)
        report = {"schema_version": "closeout-evaluation-report-v1", "suite_version": suite["schema_version"], "scope": suite["scope"],
                  "generated_at": datetime.now(timezone.utc).isoformat(), "model_quality_evaluated": False,
                  "source_digest": tested_digest, "base_commit": source_state["head_commit"],
                  **source_state, "tested_tree_digest": tested_digest, "source_digest_after": final_digest,
                  "digest_scope": "src/runtime/tests/scripts/skills/examples sources and skill docs, top-level usage/install/policy docs, harness-tool, CI workflows, scenario and implementation-requirement manifests, evaluation task definitions/positive controls and pyproject.toml; excludes generated reports",
                  "source_unchanged": tested_digest == final_digest, "passed": passed,
                  "elapsed_seconds": time.monotonic() - started, "pytest_exit_code": execution.returncode,
                  "counts": {key: sum(row["status"] == key for row in outcomes) for key in ("passed", "failed", "skipped", "missing")},
                  "scenarios": outcomes, "limitations": ["Scripted caller conformance is not model task quality or comparative performance.",
                                                        "Worker-kill uses a controlled blocking adapter for the second stage.",
                                                        "The real namespace command test is a separate opt-in test."]}
        if not passed:
            report["diagnostic"] = (execution.stdout + execution.stderr)[-6000:]
        encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(encoded)
        print(encoded, end="")
        return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
