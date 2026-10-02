"""Read-only difficult-task report. Never executes a model or candidate code."""
import argparse
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import sys


HERE = Path(__file__).resolve().parent


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    value = importlib.util.module_from_spec(spec)
    sys.modules[name] = value
    spec.loader.exec_module(value)
    return value


runner = module("hard_report_runner", "hard_queue_study.py")
common = module("hard_report_common", "report_agent_study.py")


def expected_specs(task):
    profile = runner.profile(task)
    result = {"domain."+entry["id"]: {"parameters": {"kind": "file", **{key:value for key,value in entry.items() if key != "id"}}, "timeoutMs": 1000}
              for entry in profile["expectations"]}
    result["domain.behavior"] = {"parameters": {"kind": "command", "argv": profile["test_commands"][0]["argv"], "cwd": ".", "expectedExitCode": 0}, "timeoutMs": 30000}
    return result


def report(root):
    plan = runner.validate(root)
    common.validate_frozen_inputs(root)
    common.expected_specs = expected_specs
    task = runner.study.load(root / "private/corpus.json")[0]
    trials, missing = [], []
    for row in plan["trials"]:
        result = common.summarize_trial(root, row, task)
        if result is None:
            missing.append(row["id"])
            continue
        raw = runner.study.load(root / "receipts" / row["id"] / "result.json")
        for summarized, stage in zip(result["stages"], raw["stages"]):
            summarized["failed_scenarios"] = [case["name"] for case in stage["grade"]["cases"] if not case["passed"]]
            summarized["claimed_harness_run_id"] = (stage["process"].get("claim") or {}).get("harness_run_id")
            summarized["scope_receipts_confirmed"] = stage["process"]["termination_confirmed"]
        trials.append({"requested_model": plan["hosts"][row["host"]], **result})
    return {"schema_version": "hard-queue-comparison-v1", "generated_at": datetime.now(timezone.utc).isoformat(),
            "task": task["title"], "complete": len(trials) == 2 and not missing, "missing": missing,
            "conditions": trials, "initial_seconds_limit": 900, "followup_seconds_limit": 300,
            "fixed_holdout_scenarios": len(task["cases"]), "public_acceptance_unittest_cases": 6,
            "plan_sha256": runner.study.digest((root / "plan.json").read_bytes()),
            "controls": runner.study.load(root / "receipts/controls.json"), "defect_controls": plan["mutations_detected"],
            "raw_receipts": str(root / "receipts"),
            "limitations": ["Luna ordinary versus AGY Harness changes both model and workflow. No causal Harness advantage can be inferred.",
                            "One task and one initial trial per condition, followed by a bounded repair or recovery session: not statistical performance or a model ceiling measurement.",
                            "36 holdout scenarios and the internal positive control were authored by the same evaluator. Four defect controls and explicit sanity assertions passed, but this is not independent third-party goal coverage.",
                            "The six supplied acceptance tests are only partial. A passing Harness command is not a claim that the external holdout or the entire goal passed.",
                            "Raw usage counters differ between providers. Time is consumed session time; no billing or overall financial saving is inferred.",
                            "Model identifiers are requested profiles, not attestations of served model weights.",
                            "Core/Skill results remain unsigned local-advisory with ready=false. No protected certification or Windows-native execution claim."]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = report(args.root.absolute())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n")
    print(json.dumps({"complete": result["complete"], "conditions": [
        {"id": row["id"], "initial_passed": row["initial_passed"], "final_passed": row["final_passed"], "seconds": row["total_seconds"]}
        for row in result["conditions"]]}, ensure_ascii=False))
