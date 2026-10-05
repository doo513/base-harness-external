#!/usr/bin/env python3
"""A/B/C fixed-artifact conformance pilot, not a model capability comparison."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from harness_external.adapter_execution import execute
from harness_external.develop_cases import normalize
from harness_external.errors import HarnessError
from harness_external.observation_rules import evaluate
from harness_external.pytest_adapter import prepare_runtime
from harness_external.service import Harness

TASKS = {
    "declared_case_missing": "def test_other(): assert True\n",
    "declared_case_skipped": "import pytest\n@pytest.mark.skip(reason='not implemented')\ndef test_required(): pass\n",
    "declared_case_failed": "def test_required(): assert False\n",
    "declared_case_passed": "from app import value\ndef test_required(): assert value() == 1\n",
}


def policy():
    return {"schema_version": "acceptance-policy-v1", "policy_id": "pilot", "domain_id": "develop", "revision": "1",
            "parameters": {"profile": "execution", "inputs": ["app.py", "test_cases.py"], "artifacts": ["app.py"],
                           "expectations": [{"id": "entry", "path": "app.py", "operator": "contains", "expected": "def value"}],
                           "pytest_checks": [{"id": "behavior", "paths": ["test_cases.py"], "timeout_seconds": 10}]},
            "required_check_ids": ["domain.entry", "domain.behavior"],
            "requirements": [{"id": "behavior", "statement": "Observe the declared test case", "check_ids": ["domain.entry", "domain.behavior"], "minimum_evidence": "testcase"}],
            "coverage": {"schema_version": "develop-coverage-v2", "profile_id": "pilot", "profile_revision": "1", "known_gaps": [],
                         "scenarios": [{"id": "required", "requirement_id": "behavior", "check_id": "domain.behavior", "kind": "positive", "required": True,
                                        "test_ref": {"framework": "pytest", "path": "test_cases.py", "case_id": "test_cases.py::test_required"}}]},
            "bundle": {"version": "1", "files": ["test_cases.py"]}, "authorship": {"criteria": "model", "tests": "model"},
            "approval": {"declared_by": "controlled evaluation", "reference": "fixed synthetic artifacts; not independent goal validation"}}


def main():
    results = []
    with tempfile.TemporaryDirectory(prefix="develop-pytest-pilot-") as temporary:
        root = Path(temporary)
        direct_state = root / "direct-state"
        direct_state.mkdir()
        runtime = prepare_runtime(direct_state)
        for name, tests in TASKS.items():
            trial = root / name
            workspace = trial / "workspace"
            workspace.mkdir(parents=True)
            (workspace / "app.py").write_text("def value(): return 1\n")
            (workspace / "test_cases.py").write_text(tests)
            check = normalize({"id": "behavior", "paths": ["test_cases.py"], "required_cases": ["test_cases.py::test_required"], "timeout_seconds": 10},
                              ["app.py", "test_cases.py"])
            started = time.monotonic()
            captured, facts = execute(check, workspace, direct_state, threading.Event(), 30, runtime["identity"], lambda *args: None)
            direct_seconds = time.monotonic() - started
            a = captured.get("capture", {}).get("exitCode") == 0
            b = a and all(c["result"] == "pass" for c in evaluate(facts, check["adapter"]["rules"]))
            policies = trial / "policies"
            bundle = policies / "pilot/bundle"
            bundle.mkdir(parents=True)
            (bundle / "test_cases.py").write_text(tests)
            (policies / "pilot/policy.json").write_text(json.dumps(policy()))
            api = Harness(trial / "state", policy_root=policies)
            run_id = api.start(domain_id="develop", goal="Measure " + name, workspace=str(workspace), parameters={},
                               policy_id="pilot", request_id="start")["run_id"]
            api.submit(run_id, "submit")
            started = time.monotonic()
            job_id = api.verify(run_id, "verify")["job_id"]
            deadline = time.monotonic() + 45
            while True:
                job = api.status(run_id, job_id)["job"]
                if job["status"] not in {"queued", "running"}:
                    break
                if time.monotonic() >= deadline:
                    api.cancel(run_id, job_id, "cancel")
                    raise RuntimeError("Controlled verification exceeded its observation window")
                time.sleep(.1)
            c = api.resume(run_id)["gates"]["status"] == "passed"
            try:
                record = api.finish(run_id, "finish", outcome="completed")["record"]
                completed = True
            except HarnessError as error:
                assert error.code == "VERIFICATION_REQUIRED", error.code
                api.finish(run_id, "partial", outcome="partial")
                completed = False
            expected = name == "declared_case_passed"
            assert b == c == completed == expected
            results.append({"task": name, "test_source_sha256": hashlib.sha256(tests.encode()).hexdigest(),
                            "A_process_exit_passed": a, "B_domain_case_rules_passed": b,
                            "C_core_gate_passed": c, "C_completed_allowed": completed,
                            "adapter_seconds": direct_seconds, "core_verification_seconds": time.monotonic() - started,
                            "run_id": run_id, "job_id": job_id, "measurement_status": job["result"]["status"],
                            "case_outcomes": {x["case_id"]: x["outcome"] for x in facts["cases"]},
                            "case_observations": api.records(run_id, "cases", job_id=job_id)["items"],
                            "check_observation_refs": job["result"]["observation_refs"],
                            "requirement_observations": job["result"]["requirement_observations"]})
    print(json.dumps({"schema_version": "develop-pytest-conformance-pilot-v1", "model_calls": 0,
                      "framework_runtime": runtime["identity"], "conditions": {
                          "A": "same execution, command exit criterion", "B": "same adapter facts, Domain-normalized case criteria without Run state",
                          "C": "same artifacts/criteria through full Core submission, observations and gates"},
                      "results": results, "limitations": [
                          "Fixed synthetic artifacts, not Host/model A/B/C performance or cost evidence.",
                          "A and B share one execution; their difference is the criterion, not solver behavior.",
                          "Temporary Run stores are removed after validation; this report retains case bodies, outcomes and bindings."]}, indent=2))


if __name__ == "__main__":
    main()
