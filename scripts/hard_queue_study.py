"""Opt-in two-condition difficult-task evaluation. Not a product model runtime.
Luna ordinary versus AGY + current Harness: model and workflow are confounded.
"""
import argparse
import copy
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile

HERE = Path(__file__).resolve()
ROOT = HERE.parents[1]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    sys.modules[name] = value
    spec.loader.exec_module(value)
    return value


study = module("hard_study_base", HERE.with_name("agent_study.py"))
definition = module("hard_queue_definition", ROOT / "evaluation/hard_queue/task.py")
study.MAX_SECONDS = 900
study.REPAIR_SECONDS = 300
study.COMMON = study.COMMON.replace("600 seconds", "900 seconds")
study.tasks = lambda: [copy.deepcopy(definition.TASK)]
original_validate = study.validate_plan


def profile(task):
    return {"profile": "execution", "inputs": list(task["sources"]) + ["acceptance_tests/test_task.py"],
            "artifacts": list(task["sources"]),
            "expectations": [{"id": "entry0", "path": "leasequeue.py", "operator": "contains", "expected": "class Queue"},
                             {"id": "entry1", "path": "leasequeue_cli.py", "operator": "contains", "expected": "def main"}],
            "test_commands": [{"id": "behavior", "argv": ["python3", "-m", "unittest", "discover", "-s", "acceptance_tests", "-v"], "timeout_seconds": 30}]}


study.profile = profile


def validate(root):
    plan = original_validate(root)
    if plan.get("hard_wrapper_hash") != study.digest(HERE.read_bytes()):
        raise ValueError("Frozen hard-task controller changed")
    if {(x["host"], x["arm"]) for x in plan["trials"]} != {("codex", "plain"), ("agy", "after")} or len(plan["trials"]) != 2:
        raise ValueError("This evaluation authorizes only Luna ordinary and AGY Harness")
    return plan


def sanity(cases):
    rows = {item["name"]: item["actual"] for item in cases}
    assert len(rows) == 36 and all("driver_error" not in row for row in rows.values())
    assert [v["id"] if isinstance(v, dict) else v for v in rows["priority-and-insertion-ties"]["outputs"][3:]] == ["high", "z", "a", None]
    assert rows["finish-at-exact-deadline"]["outputs"][2] is False
    assert rows["renew-at-exact-deadline"]["outputs"][2] is False
    assert rows["retry-exhaustion"]["outputs"][-1]["status"] == "failed"
    assert [v["status"] for v in rows["expiry-and-transitive-blocking"]["outputs"][-3:]] == ["failed", "blocked", "blocked"]
    assert rows["renewal-uses-now-not-old-deadline"]["outputs"][3]["lease_until"] == 6
    assert rows["replay-original-claim-after-completion"]["outputs"][1] == rows["replay-original-claim-after-completion"]["outputs"][3]
    assert rows["canonical-objects-but-distinct-number-types"]["outputs"][:4] == [True, True, {"error": "ValueError"}, {"error": "ValueError"}]
    assert rows["detached-input-and-return"]["outputs"][-1]["payload"] == {"items": [1]}
    for name in ("adds", "add-replay", "claims", "replay", "finish"):
        row = rows["concurrent-"+name]
        assert all(code == 0 for code in row["exits"]) and row["contiguous_events"]
    assert rows["concurrent-adds"]["true_count"] == rows["concurrent-adds"]["event_count"] == 6
    assert rows["concurrent-add-replay"]["true_count"] == 6 and rows["concurrent-add-replay"]["event_count"] == 1
    assert rows["concurrent-claims"]["ids"] == ["j"+str(i) for i in range(6)] and rows["concurrent-claims"]["unique_tokens"] == 6
    assert rows["concurrent-replay"]["unique_tokens"] == 1 and rows["concurrent-replay"]["event_count"] == 2
    assert rows["concurrent-finish"]["true_count"] == 1 and rows["concurrent-finish"]["false_count"] == 5


def prepare(root):
    study.prepare(root)  # No provider calls. Unselected templates are never executed.
    shutil.copy2(ROOT / "evaluation/hard_queue/driver.py", root / "private/subject_driver.py")
    reference = root / "private/references/leasequeue"
    shutil.copytree(ROOT / "evaluation/hard_queue/reference", reference, ignore=shutil.ignore_patterns("__pycache__"))
    (reference / "acceptance_tests").mkdir()
    shutil.copy2(ROOT / "evaluation/hard_queue/public_tests.py", reference / "acceptance_tests/test_task.py")
    corpus = study.load(root / "private/corpus.json")
    observed = study.grade(root, corpus[0], reference, root / "receipts/reference-observations.json")
    if observed["status"] != "completed":
        raise ValueError("Reference execution unavailable; do not start models")
    sanity(observed["cases"])
    for case, measured in zip(corpus[0]["cases"], observed["cases"]):
        case["expected"] = measured["actual"]
    study.write_json(root / "private/corpus.json", corpus)
    # Check simple intended defects against the fixed oracle before model trials.
    mutations = {
        "late-expiry": ('job["lease_until"] <= now', 'job["lease_until"] < now'),
        "no-request-replay": ('if request_id in state["requests"]:', 'if False:'),
        "ignore-dependencies": ('and all(jobs[d]["status"] == "succeeded" for d in j["dependencies"])', 'and True'),
        "ignore-fencing-token": ('or job["token"] != token', 'or False'),
    }
    mutations_detected = []
    for name, (old, new) in mutations.items():
        with tempfile.TemporaryDirectory(prefix="hard-queue-control-") as temporary:
            mutant = Path(temporary) / "subject"
            shutil.copytree(reference, mutant, ignore=shutil.ignore_patterns("__pycache__"))
            path = mutant / "leasequeue.py"
            source = path.read_text()
            assert source.count(old) == 1
            path.write_text(source.replace(old, new))
            result = study.grade(root, corpus[0], mutant, root / "receipts/mutations" / (name+".json"))
            assert result["status"] == "completed" and not result["all_passed"], name
            mutations_detected.append({"name": name, "passed": result["passed"], "total": result["total"], "detected": True})
    plan = study.load(root / "plan.json")
    study.write_json(root / "receipts/unselected-template-plan.json", plan)
    plan["trials"] = [row for row in plan["trials"] if (row["host"], row["arm"]) in {("codex", "plain"), ("agy", "after")}]
    plan.update(schema_version="hard-queue-study-v1", hard_wrapper_hash=study.digest(HERE.read_bytes()),
                definition_hashes=study.file_set(root / "private"),
                comparison="Luna ordinary vs AGY current Harness; model/workflow confounded, not a causal Harness benefit test",
                oracle="Internal hidden reference outputs, explicit state/concurrency sanity assertions and four defect controls; fixed before providers, not third-party validation",
                recovery_policy="One 300-second fresh session: failed-case names for repair, otherwise recovery inspection; no manual code changes",
                mutations_detected=mutations_detected)
    study.write_json(root / "plan.json", plan)
    (root / "plan.sha256").write_text(study.digest((root / "plan.json").read_bytes())+"\n")
    archive = root / "receipts/executed-controller"
    archive.mkdir()
    for file in (HERE, HERE.with_name("agent_study.py")):
        shutil.copy2(file, archive / file.name)
    study.validate_plan = validate
    if study.controls(root) != 0 or study.qualify(root) != 0:
        raise ValueError("Controls/scope qualification failed")
    print(json.dumps({"prepared_scored_conditions": 2, "holdout_scenarios": len(corpus[0]["cases"]), "mutations": mutations_detected}), flush=True)


def run(root, host):
    plan = validate(root)
    if not study.load(root / "receipts/controls.json")["passed"] or not study.load(root / "receipts/qualification.json")["passed"]:
        raise ValueError("Controls must pass before provider calls")
    row = next(item for item in plan["trials"] if item["host"] == host)
    output = root / "receipts" / row["id"]
    if output.exists():
        raise ValueError("Prior invocation exists; inspect rather than reissue it")
    trial = root / "trials" / row["id"]
    task = study.load(root / "private/corpus.json")[0]
    if study.file_set(trial / "workspace") != row["initial_files"] or study.digest((trial / "prompt.txt").read_bytes()) != row["prompt_hash"]:
        raise ValueError("Initial inputs changed")
    stages = []
    for index in range(2):
        stage = "initial" if index == 0 else "recovery" if stages[0]["grade"]["all_passed"] else "repair"
        if index == 0:
            prompt, seconds = (trial / "prompt.txt").read_text(), 900
        else:
            if stages[0]["grade"]["status"] == "unavailable":
                break
            failed = [c["name"] for c in stages[0]["grade"]["cases"] if not c["passed"]]
            prompt = (study.COMMON.replace("900 seconds", "300 seconds") + "\nFresh Host session for the same task. Read TASK.md and saved notes in " + str(trial / "workspace") + ". " +
                      ("Repair the existing implementation; fixed holdout failures: " + json.dumps(failed) if stage == "repair" else "The fixed holdout passed. Recover/inspect state without inventing a code change or new Run.") +
                      (" Use ordinary tools; no Harness." if host == "codex" else " Use the Harness Skill at " + str(root / "sources/after/skills/harness-workflow/SKILL.md") + " and config " + str(trial / "client.json") + ". Preserve mandatory policy. Resume the existing Run if active; a real code repair after closure requires an explicitly linked replacement Run with the same intent/constraints. No new Run merely to report unchanged success."))
            seconds = 300
        process = study.invoke(root, row, stage, prompt, seconds)
        artifacts = output / stage / "artifacts"
        artifacts.mkdir()
        for name in study.file_set(trial / "workspace"):
            target = artifacts / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(trial / "workspace" / name, target)
        grade = study.grade(root, task, artifacts, output / stage / "grade.json")
        result = {"stage": stage, "process": process, "grade": {k:v for k,v in grade.items() if k != "sandbox"},
                  "harness": study.harvest(root, row, output / stage / "harness"),
                  "false_completion": bool(process["claim"] and process["claim"].get("status") == "completed" and grade["status"] != "unavailable" and not grade["all_passed"]),
                  "human_interventions": 0}
        study.write_json(output / stage / "result.json", result)
        stages.append(result)
        validate(root)
        print(json.dumps({"finished": row["id"], "stage": stage, "passed": grade["passed"], "total": grade["total"], "seconds": round(process["wall_seconds"], 1)}), flush=True)
    study.write_json(output / "result.json", {**row, "stages": stages,
                     "total_wall_seconds": sum(s["process"]["wall_seconds"] for s in stages),
                     "repair_sessions": sum(s["stage"] == "repair" for s in stages), "recovery_sessions": sum(s["stage"] == "recovery" for s in stages)})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("prepare", "run"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--host", choices=("codex", "agy"))
    args = parser.parse_args()
    study.validate_plan = validate
    if args.operation == "prepare":
        prepare(args.root.absolute())
    elif args.host:
        run(args.root.absolute(), args.host)
    else:
        parser.error("run requires --host")
