"""Luna + Harness extension of the frozen hard-queue study; no product changes."""
import argparse
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess
import time

from hard_queue_study import module

HERE = Path(__file__).resolve()
hard = module("luna_harness_hard", HERE.with_name("hard_queue_study.py"))
study = hard.study


def validate(root):
    plan = hard.original_validate(root)
    if plan.get("luna_harness_wrapper_hash") != study.digest(HERE.read_bytes()):
        raise ValueError("Frozen continuation runner changed")
    if plan["hosts"] != {"codex": {"model": "gpt-6-luna", "effort": "medium"}}:
        raise ValueError("Only Luna medium is authorized")
    if len(plan["trials"]) != 1 or (plan["trials"][0]["host"], plan["trials"][0]["arm"]) != ("codex", "after"):
        raise ValueError("Expected one fresh Luna Harness condition")
    return plan


def prepare(root, reference):
    started = time.monotonic()
    prior = hard.validate(reference)
    if root.exists():
        raise ValueError("Use a new study directory; never overwrite a previous trial")
    root.mkdir(parents=True)
    for directory in ("sources", "private", "policies"):
        shutil.copytree(reference / directory, root / directory, ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("scope.py", "response-schema.json"):
        shutil.copy2(reference / name, root / name)
    (root / "receipts").mkdir()
    task = study.load(root / "private/corpus.json")[0]
    row = {"id": "codex__leasequeue__after", "host": "codex", "task": "leasequeue", "arm": "after"}
    trial = root / "trials" / row["id"]
    workspace = trial / "workspace"
    (workspace / "acceptance_tests").mkdir(parents=True)
    for name, source in task["sources"].items():
        (workspace / name).write_text(source)
    (workspace / "acceptance_tests/test_task.py").write_text(task["acceptance_test"])
    (workspace / "TASK.md").write_text(task["spec"]+"\n")
    (workspace / "AGENTS.md").write_text(study.COMMON)
    subprocess.run(["git", "init", "--quiet", str(workspace)], check=True)
    study.write_json(trial / "client.json", {"schema_version": 1, "harness_repo": str(root / "sources/after"),
                     "state_dir": str(trial / "state"), "state_lifetime": "persistent", "policy_root": str(root / "policies/leasequeue")})
    study.write_json(trial / "task-parameters.json", hard.profile(task))
    (trial / "prompt.txt").write_text(study.task_prompt(root, trial, row, task))
    row.update(initial_files=study.file_set(workspace), prompt_hash=study.digest((trial / "prompt.txt").read_bytes()))
    plan = copy.deepcopy(prior)
    plan.update(schema_version="luna-harness-hard-queue-v1", hosts={"codex": study.HOSTS["codex"]}, trials=[row],
                created_at=datetime.now(timezone.utc).isoformat(), automatic_setup_seconds=time.monotonic()-started,
                reference_study=str(reference), reference_plan_sha256=study.digest((reference / "plan.json").read_bytes()),
                luna_harness_wrapper_hash=study.digest(HERE.read_bytes()),
                comparison="Same requested Luna model/effort, original task, frozen tests and runtime; new Harness condition versus previously observed plain condition. One non-simultaneous sample, not statistical superiority.")
    study.write_json(root / "plan.json", plan)
    (root / "plan.sha256").write_text(study.digest((root / "plan.json").read_bytes())+"\n")
    validate(root)
    if study.controls(root) != 0 or study.qualify(root) != 0:
        raise ValueError("Controls/qualification failed; do not launch a model")
    archive = root / "receipts/executed-controller"
    archive.mkdir()
    for path in (HERE, HERE.with_name("hard_queue_study.py"), HERE.with_name("agent_study.py")):
        shutil.copy2(path, archive / path.name)


def run(root):
    plan = validate(root)
    assert study.load(root / "receipts/controls.json")["passed"]
    assert study.load(root / "receipts/qualification.json")["passed"]
    row = plan["trials"][0]
    trial, output = root / "trials" / row["id"], root / "receipts" / row["id"]
    if output.exists():
        raise ValueError("Prior invocation exists; observe it instead of starting another")
    if study.file_set(trial / "workspace") != row["initial_files"] or study.digest((trial / "prompt.txt").read_bytes()) != row["prompt_hash"]:
        raise ValueError("Initial inputs changed")
    task = study.load(root / "private/corpus.json")[0]
    stages = []
    for index in range(2):
        stage = "initial" if not index else "recovery" if stages[0]["grade"]["all_passed"] else "repair"
        if not index:
            prompt, seconds = (trial / "prompt.txt").read_text(), 900
        else:
            if stages[0]["grade"]["status"] == "unavailable":
                break
            failed = [c["name"] for c in stages[0]["grade"]["cases"] if not c["passed"]]
            prompt = (study.COMMON.replace("900 seconds", "300 seconds") + "\nFresh Host session for the same task. Read TASK.md and saved notes in " + str(trial / "workspace") + ". " +
                      ("Repair the existing implementation; fixed holdout failures: " + json.dumps(failed) if stage == "repair" else "The fixed holdout passed. Recover/inspect state without inventing a code change or new Run.") +
                      " Use the Harness Skill at " + str(root / "sources/after/skills/harness-workflow/SKILL.md") + " and config " + str(trial / "client.json") + ". Preserve mandatory policy. Resume the existing Run if active; a real code repair after closure requires an explicitly linked replacement Run with the same intent/constraints. No new Run merely to report unchanged success.")
            seconds = 300
        process = study.invoke(root, row, stage, prompt, seconds)
        artifacts = output / stage / "artifacts"
        artifacts.mkdir()
        for name in study.file_set(trial / "workspace"):
            destination = artifacts / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(trial / "workspace" / name, destination)
        grade = study.grade(root, task, artifacts, output / stage / "grade.json")
        result = {"stage": stage, "process": process, "grade": {k:v for k,v in grade.items() if k != "sandbox"},
                  "harness": study.harvest(root, row, output / stage / "harness"), "human_interventions": 0,
                  "false_completion": bool(process["claim"] and process["claim"].get("status") == "completed" and grade["status"] != "unavailable" and not grade["all_passed"])}
        study.write_json(output / stage / "result.json", result)
        stages.append(result)
        validate(root)
        print(json.dumps({"stage": stage, "passed": grade["passed"], "total": grade["total"], "seconds": process["wall_seconds"]}), flush=True)
    study.write_json(output / "result.json", {**row, "stages": stages, "total_wall_seconds": sum(s["process"]["wall_seconds"] for s in stages)})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("prepare", "run"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--reference", type=Path)
    args = parser.parse_args()
    study.validate_plan = validate
    if args.operation == "prepare":
        if args.reference is None:
            parser.error("prepare requires --reference")
        prepare(args.root.absolute(), args.reference.absolute())
    else:
        run(args.root.absolute())
