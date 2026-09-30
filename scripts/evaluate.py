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


def main():
    parser = argparse.ArgumentParser(description="Common Closeout conformance evaluation (no commercial-model benchmark)")
    parser.add_argument("--output", type=Path, help="optional JSON report destination")
    args = parser.parse_args()
    repository = Path(__file__).resolve().parents[1]
    suite = json.loads((repository / "evaluation" / "closeout-scenarios.json").read_text())
    sources = sorted(path for path in (repository / "src").rglob("*") if path.suffix in {".py", ".json"})
    sources += sorted((repository / "runtime").rglob("*.ts"))
    sources += [Path(__file__), repository / "tests" / "test_closeout_semantics.py",
                repository / "evaluation" / "closeout-scenarios.json", repository / "pyproject.toml"]
    source_digest = hashlib.sha256(json.dumps({str(path.relative_to(repository)): hashlib.sha256(path.read_bytes()).hexdigest()
                                               for path in sources}, sort_keys=True).encode()).hexdigest()
    revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repository, capture_output=True, text=True)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="closeout-evaluation-") as temporary:
        results = Path(temporary) / "results.xml"
        execution = subprocess.run([sys.executable, "-m", "pytest", "-q", "tests/test_closeout_semantics.py", "--junitxml", str(results)],
                                   cwd=repository, capture_output=True, text=True)
        require_results = results.is_file()
        cases = {item.get("name"): item for item in ET.parse(results).iter("testcase")} if require_results else {}
        outcomes = []
        for scenario in suite["scenarios"]:
            case = cases.get(scenario["test"])
            state = "missing" if case is None else "failed" if case.find("failure") is not None or case.find("error") is not None else "skipped" if case.find("skipped") is not None else "passed"
            outcomes.append({**scenario, "status": state, "elapsed_seconds": float(case.get("time", "0")) if case is not None else None})
        report = {"schema_version": "closeout-evaluation-report-v1", "suite_version": suite["schema_version"], "scope": suite["scope"],
                  "generated_at": datetime.now(timezone.utc).isoformat(), "model_quality_evaluated": False,
                  "source_digest": source_digest, "base_commit": revision.stdout.strip() if revision.returncode == 0 else None,
                  "elapsed_seconds": time.monotonic() - started, "pytest_exit_code": execution.returncode,
                  "counts": {key: sum(row["status"] == key for row in outcomes) for key in ("passed", "failed", "skipped", "missing")},
                  "scenarios": outcomes, "limitations": ["Scripted caller conformance is not model task quality or comparative performance.",
                                                        "Worker-kill uses a controlled blocking adapter for the second stage.",
                                                        "The real namespace command test is a separate opt-in test."]}
        if execution.returncode or not require_results:
            report["diagnostic"] = (execution.stdout + execution.stderr)[-6000:]
        encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(encoded)
        print(encoded, end="")
        return 1 if execution.returncode or report["counts"]["failed"] or report["counts"]["missing"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
