"""Read-only study audit/aggregation; no model calls and no scoring rewrites."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def expected_specs(task):
    files = list(task["sources"])
    result = {}
    for index, name in enumerate(files):
        term = "Journal" if name == "journal.py" else "normalize" if name == "settings.py" else "plan" if name == "planner.py" else "main"
        result["domain.entry" + str(index)] = {"parameters": {"kind": "file", "path": name, "operator": "contains", "expected": term}, "timeoutMs": 1000}
    result["domain.behavior"] = {"parameters": {"kind": "command", "argv": ["python3", "-m", "unittest", "discover", "-s", "acceptance_tests", "-v"], "cwd": ".", "expectedExitCode": 0}, "timeoutMs": 30000}
    return result


def artifact_matches(directory, hashes):
    observed = {}
    for path in directory.rglob("*"):
        relative = path.relative_to(directory)
        if any(part in {".git", "__pycache__", ".pytest_cache"} for part in relative.parts):
            continue
        if path.is_symlink():
            return False
        if path.is_file():
            observed[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return observed == hashes


def validate_frozen_inputs(root):
    raw = (root / "plan.json").read_bytes()
    if hashlib.sha256(raw).hexdigest() != (root / "plan.sha256").read_text().strip():
        raise ValueError("Plan identity changed")
    plan = load(root / "plan.json")
    for arm, hashes in plan["source_files"].items():
        if not artifact_matches(root / "sources" / arm, hashes):
            raise ValueError("Frozen runtime changed: " + arm)
    for directory, field in (("private", "definition_hashes"), ("policies", "policy_hashes")):
        if not artifact_matches(root / directory, plan[field]):
            raise ValueError("Frozen definitions changed: " + directory)
    if hashlib.sha256((root / "scope.py").read_bytes()).hexdigest() != plan["scope_hash"]:
        raise ValueError("Frozen execution scope changed")
    if not all(load(root / "receipts" / name)["passed"] for name in ("controls.json", "qualification.json")):
        raise ValueError("Controls or execution qualification did not pass")
    return plan


def selected_trials(plan, host, tasks):
    trials = [trial for trial in plan["trials"] if trial["host"] == host]
    expected = {(task, arm) for task in tasks for arm in ("plain", "before", "after")}
    if (len(tasks) != 3 or len(trials) != 9 or len({trial["id"] for trial in trials}) != 9
            or {(trial["task"], trial["arm"]) for trial in trials} != expected):
        raise ValueError("Expected exactly nine unique task/arm conditions per model")
    return trials


def summarize_trial(root, planned, task):
    path = root / "receipts" / planned["id"] / "result.json"
    if not path.exists():
        return None
    result = load(path)
    source = Path(result.get("reused_from", str(path.parent)))
    stages = result["stages"]
    expected = expected_specs(task)
    stage_rows = []
    for stage in stages:
        process, grade = stage["process"], stage["grade"]
        stage_path = source / stage["stage"]
        if process != load(stage_path / "process.json"):
            raise ValueError("Process receipt differs from trial result")
        original_grade = load(stage_path / "grade.json")
        if grade != {key: value for key, value in original_grade.items() if key != "sandbox"}:
            raise ValueError("Grade receipt differs from trial result")
        if grade["case_set_hash"] != hashlib.sha256(encoded(task["cases"])).hexdigest():
            raise ValueError("Scored cases differ from frozen task")
        if process.get("trace_hash_at_exit") and hashlib.sha256((stage_path / "stdout.jsonl").read_bytes()).hexdigest() != process["trace_hash_at_exit"]:
            raise ValueError("Process trace changed after exit")
        if not artifact_matches(source / stage["stage"] / "artifacts", grade["source_files"]):
            raise ValueError("Scored artifact changed: " + planned["id"] + "/" + stage["stage"])
        executions = []
        for run in stage["harness"]:
            definitions = run.get("mandatory_specs", {})
            matching = {key: key in definitions and all(definitions[key].get(field) == value for field, value in body.items())
                        for key, body in expected.items()}
            comparison = run.get("baseline_comparison") or {}
            executions.append({"run_id": run["run_id"], "mandatory_scope_preserved": all(matching.values()), "checks": matching,
                               "mode": (run.get("policy") or {}).get("mode"), "resolution": run.get("resolution"),
                               "current_command_passes": run["command_passes"], "workspace_matches": run["workspace_matches"],
                               "baseline_status": comparison.get("status", "not_requested"),
                               "required_behavior_improved": any(c["check_id"] == "domain.behavior" and c["status"] == "improved" for c in comparison.get("changes", [])),
                               "acceptance": (run.get("resolution") or {}).get("acceptance"),
                               "usage": run.get("usage"), "history_counts": run.get("history_counts")})
        stage_rows.append({"stage": stage["stage"], "artifact_passed": grade["all_passed"], "passed_checks": grade["passed"], "total_checks": grade["total"],
                           "grade_status": grade["status"], "claim": (process.get("claim") or {}).get("status"),
                           "process_exit_code": process["exit_code"], "timed_out": process["timed_out"],
                           "namespace_termination_confirmed": process.get("termination_confirmed"),
                           "reuse_validation": result.get("reuse_validation"),
                           "consumed_seconds": process["wall_seconds"], "tool_calls": process["tool_calls"], "raw_usage": process["usage"],
                           "external_grading_seconds": grade["elapsed_seconds"],
                           "false_completion": stage["false_completion"], "human_interventions": stage["human_interventions"],
                           "harness_runs": executions, "implementation_hashes": {key: grade["source_files"].get(key) for key in task["sources"]}})
    return {"id": planned["id"], "task": planned["task"], "arm": planned["arm"], "source_receipts": str(source),
            "reused": "reused_from" in result, "stages": stage_rows,
            "initial_passed": stage_rows[0]["artifact_passed"], "final_passed": stage_rows[-1]["artifact_passed"],
            "initial_seconds": stage_rows[0]["consumed_seconds"], "total_seconds": sum(s["consumed_seconds"] for s in stage_rows),
            "repairs": sum(s["stage"] == "repair" for s in stage_rows), "recovery_sessions": sum(s["stage"] == "recovery" for s in stage_rows),
            "recovery_preserved_run_ids": (sorted(r["run_id"] for r in stage_rows[0]["harness_runs"])
                                           == sorted(r["run_id"] for r in stage_rows[-1]["harness_runs"]))
                                          if planned["arm"] != "plain" and stage_rows[-1]["stage"] == "recovery" else None,
            "implementation_changed_during_followup": len(stage_rows) > 1 and stage_rows[0]["implementation_hashes"] != stage_rows[-1]["implementation_hashes"]}


def report(luna_root, agy_root):
    plans = [validate_frozen_inputs(root) for root in (luna_root, agy_root)]
    if plans[0]["hosts"]["codex"]["model"] != "gpt-6-luna":
        raise ValueError("The user authorized Luna, not another Codex model")
    if plans[1]["hosts"]["agy"]["model"] != "gemini-3.8-flash-high":
        raise ValueError("Unexpected AGY model profile")
    if plans[0]["source_files"] != plans[1]["source_files"] or plans[0]["policy_hashes"] != plans[1]["policy_hashes"]:
        raise ValueError("Models did not use identical frozen Harness/policy bytes")
    corpus = load(luna_root / "private/corpus.json")
    if corpus != load(agy_root / "private/corpus.json"):
        raise ValueError("Task requirements or fixed observations differ")
    tasks = {task["id"]: task for task in corpus}
    rows, missing = [], []
    for root, plan, host, label in ((luna_root, plans[0], "codex", "luna"), (agy_root, plans[1], "agy", "agy")):
        for trial in selected_trials(plan, host, tasks):
            row = summarize_trial(root, trial, tasks[trial["task"]])
            if row is None:
                missing.append(label + ":" + trial["id"])
            else:
                rows.append({"model": label, "requested_profile": plan["hosts"][host], **row})
    groups = []
    for model in ("luna", "agy"):
        for arm in ("plain", "before", "after"):
            selected = [r for r in rows if r["model"] == model and r["arm"] == arm]
            stages = [stage for row in selected for stage in row["stages"]]
            usage = {}
            for stage in stages:
                for key, value in (stage["raw_usage"] or {}).items():
                    if type(value) in (int, float):
                        usage[key] = usage.get(key, 0) + value
            groups.append({"model": model, "arm": arm, "tasks_observed": len(selected), "tasks_planned": 3,
                           "initial_passed": sum(r["initial_passed"] for r in selected), "final_passed": sum(r["final_passed"] for r in selected),
                           "initial_consumed_seconds": sum(r["initial_seconds"] for r in selected), "total_consumed_seconds": sum(r["total_seconds"] for r in selected),
                           "external_grading_seconds": sum(s["external_grading_seconds"] for s in stages),
                           "repair_sessions": sum(r["repairs"] for r in selected), "recovery_sessions": sum(r["recovery_sessions"] for r in selected),
                           "timed_out_sessions": sum(s["timed_out"] for s in stages), "false_completion_sessions": sum(s["false_completion"] for s in stages),
                           "tool_calls": sum(s["tool_calls"] for s in stages), "raw_usage_totals": usage,
                           "usage_sessions": sum(s["raw_usage"] is not None for s in stages), "usage_missing_sessions": sum(s["raw_usage"] is None for s in stages),
                           "human_solver_interventions": sum(s["human_interventions"] for s in stages)})
    return {"schema_version": "luna-agy-study-audit-v1", "generated_at": datetime.now(timezone.utc).isoformat(),
            "complete": not missing and len(rows) == 18, "planned_conditions": 18, "observed_conditions": len(rows),
            "missing": missing, "groups": groups, "trials": rows,
            "automatic_setup_seconds": {"luna_study": plans[0]["automatic_setup_seconds"], "original_mixed_host_study": plans[1]["automatic_setup_seconds"]},
            "audit": {"frozen_runtimes_and_definitions_match": True, "controls_and_scope_qualification_passed": True,
                      "receipt_and_artifact_identities_match": True, "complete_condition_matrix": True},
            "frozen_runtime_hashes": {arm: hashlib.sha256(encoded(value)).hexdigest() for arm, value in plans[0]["source_files"].items()},
            "limitations": ["Only Luna and AGY are in this comparison; previous Astra experiments are retained separately, not relabeled.",
                            "Three tasks per cell and one repetition: internal pilot, not statistical superiority or general goal certification.",
                            "Timeout durations are consumed time at the budget, not successful completion latency. Missing usage is unknown, not zero.",
                            "Raw token accounting differs across providers; no cross-provider monetary conversion or savings claim.",
                            "Task/test authoring, actual human review time and subscription charges are unmeasured.",
                            "Three normally completed AGY settings conditions were explicitly reused; earlier termination-affected conditions were excluded and retained.",
                            "Frozen models compare the same runtime bytes. A later Core guard against malformed custom Domain mandatory-ID output is verified separately."]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--luna-root", type=Path, required=True)
    parser.add_argument("--agy-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = report(args.luna_root, args.agy_root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"complete": result["complete"], "observed": result["observed_conditions"], "planned": 18, "groups": result["groups"]}, ensure_ascii=False))
