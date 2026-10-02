"""User-requested Luna-only continuation of the frozen paired study.

AGY can finish its already-running study without changing that runner's hash.
This wrapper records its own identity and copies the SAME frozen runtimes.
It never invokes Astra or another unrequested model.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys


HERE = Path(__file__).resolve()
spec = importlib.util.spec_from_file_location("paired_study_base", HERE.with_name("agent_study.py"))
study = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = study
spec.loader.exec_module(study)
study.HOSTS = {"codex": {"model": "gpt-6-luna", "effort": "medium", "host": "Codex CLI"}}
original_argv = study.cli_argv
original_validate = study.validate_plan
original_invoke = study.invoke


def luna_argv(host, root, trial, prompt):
    if host != "codex":
        raise ValueError("This continuation is Luna-only")
    argv = original_argv(host, root, trial, prompt)
    argv[argv.index('-c') + 1] = 'model_reasoning_effort="medium"'
    assert argv[argv.index('-m') + 1] == 'gpt-6-luna'
    return argv


def validate(root):
    plan = original_validate(root)
    if plan.get("wrapper_hash") != hashlib.sha256(HERE.read_bytes()).hexdigest():
        raise ValueError("Frozen Luna wrapper changed")
    return plan


def invoke(*args, **kwargs):
    result = original_invoke(*args, **kwargs)
    if result.get("errors") and result.get("tool_calls", 0) == 0 and result.get("claim") is None:
        raise RuntimeError("Model invocation failed before task execution; receipt retained. Stop this Host instead of scoring unchanged stubs or issuing more attempts.")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("prepare", "controls", "qualify", "run", "summarize"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--reference-study", type=Path, help="existing study supplying the frozen before/after runtime bytes")
    args = parser.parse_args()
    root = args.root.absolute()
    study.cli_argv = luna_argv
    study.invoke = invoke
    if args.operation == "prepare":
        if args.reference_study is None:
            parser.error("prepare requires --reference-study")
        reference = args.reference_study.absolute()
        prior = json.loads((reference / "plan.json").read_text())
        def snapshots(target, *, before=False):
            arm = "before" if before else "after"
            source = reference / "sources" / arm
            if study.file_set(source) != prior["source_files"][arm]:
                raise ValueError("Reference runtime changed")
            shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
            return study.file_set(target)
        study.snapshot_runtime = snapshots
        study.prepare(root)
        plan = json.loads((root / "plan.json").read_text())
        plan.update(wrapper_hash=hashlib.sha256(HERE.read_bytes()).hexdigest(), reference_study=str(reference),
                    model_change_reason="User restricted model tests to Luna and AGY; Astra results are not part of this model comparison.")
        if json.loads((root / "private/corpus.json").read_text()) != json.loads((reference / "private/corpus.json").read_text()):
            raise ValueError("Task requirements or fixed observations changed")
        study.write_json(root / "plan.json", plan)
        (root / "plan.sha256").write_text(study.digest((root / "plan.json").read_bytes()) + "\n")
    else:
        study.validate_plan = validate
        if args.operation == "run":
            study.run_host(root, "codex")
        elif args.operation == "controls":
            raise SystemExit(study.controls(root))
        elif args.operation == "qualify":
            raise SystemExit(study.qualify(root))
        else:
            study.summarize(root)
