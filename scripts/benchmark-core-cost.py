#!/usr/bin/env python3
"""Controlled CLI/context cost comparison. No model calls or production state."""
import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import tempfile
import time


def subject(root, repetitions):
    sys.path.insert(0, str(root / "src"))
    from harness_external.service import Harness
    spec = importlib.util.spec_from_file_location("cost_caller", root / "skills/harness-workflow/scripts/harness_client.py")
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    with tempfile.TemporaryDirectory(prefix="harness-cost-state-") as temporary:
        workspace = Path(temporary) / "workspace"
        workspace.mkdir()
        (workspace / "sample.txt").write_text("ready")
        api = Harness(Path(temporary) / "state")
        run_id = api.start(domain_id="develop", goal="Goal detail. " * 1000, workspace=str(workspace),
                           parameters={"profile": "structural", "inputs": ["sample.txt"], "artifacts": ["sample.txt"],
                                       "expectations": [{"path": "sample.txt", "operator": "equals", "expected": "ready"}]},
                           request_id="start", mode="exploratory")["run_id"]
        state = api.resume(run_id)
        client = helper.Client({"schema_version": 1, "harness_repo": str(root), "state_dir": str(api.store.root), "state_lifetime": "ephemeral"})
        binding = {"schema_version": "harness-caller-binding-v1", "config": client.config, "run_id": run_id,
                   "workspace": str(workspace), "domain": "develop", "intent_ref": state["intent"]["ref"], "policy_ref": state["policy"]["ref"]}
        original = client.execute
        calls = []
        def counted(*args, **kwargs):
            calls.append(args[0])
            return original(*args, **kwargs)
        client.execute = counted
        samples, counts = [], []
        for index in range(repetitions + 2):
            calls.clear()
            started = time.perf_counter()
            request = client.make_request("status", {}, binding=binding)["request"]
            result = client.call(request)
            elapsed = time.perf_counter() - started
            assert result["ok"], result
            if index >= 2:
                samples.append(elapsed)
                counts.append(len(calls))
        result = {"seconds": samples, "median_seconds": statistics.median(samples),
                  "p95_seconds": sorted(samples)[max(0, int(len(samples) * .95 + .9999) - 1)],
                  "core_cli_calls": counts, "status_response_bytes": len(json.dumps(result).encode())}
        if hasattr(api, "context"):
            total = 0
            response = api.context(run_id)
            while True:
                total += len(json.dumps(response).encode())
                if not response["next_page"]:
                    break
                response = api.context(run_id, page=response["next_page"])
            delta = api.context(run_id, after=response["next_cursor"])
            assert not delta["items"]
            result["context"] = {"goal_characters": 13000, "full_transfer_bytes": total,
                                 "unchanged_delta_bytes": len(json.dumps(delta).encode()),
                                 "reduction": 1 - len(json.dumps(delta).encode()) / total}
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--subject", type=Path)
    parser.add_argument("--repetitions", type=int, default=20)
    args = parser.parse_args()
    if args.subject:
        print(json.dumps(subject(args.subject.resolve(), args.repetitions)))
        return
    if not args.baseline or args.repetitions < 20:
        parser.error("supply a frozen baseline checkout and at least 20 repetitions")
    current = Path(__file__).resolve().parents[1]
    # Both imports run from the same filesystem, with the same Python executable.
    # Only read-only source is copied; the temporary trees and state are disposable.
    with tempfile.TemporaryDirectory(prefix="harness-core-cost-", dir=current.parent) as temporary:
        conditions = {}
        for name, origin in (("before", args.baseline.resolve()), ("after", current)):
            checkout = Path(temporary) / name
            checkout.mkdir()
            for directory in ("src", "scripts", "skills"):
                shutil.copytree(origin / directory, checkout / directory, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            (checkout / ".venv").symlink_to(current / ".venv", target_is_directory=True)
            process = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--subject", str(checkout),
                                      "--repetitions", str(args.repetitions)], capture_output=True, text=True, timeout=240)
            if process.returncode:
                raise RuntimeError(process.stderr + process.stdout)
            conditions[name] = json.loads(process.stdout)
    reduction = 1 - conditions["after"]["median_seconds"] / conditions["before"]["median_seconds"]
    report = {"schema_version": "core-cost-comparison-v1", "baseline_commit": "fdd2767", "repetitions": args.repetitions,
              "warmups_each": 2, "model_calls": 0, "conditions": conditions, "median_time_reduction": reduction,
              "acceptance": {"one_core_call": set(conditions["after"]["core_cli_calls"]) == {1},
                             "median_reduction_at_least_50_percent": reduction >= .5,
                             "delta_reduction_at_least_80_percent": conditions["after"]["context"]["reduction"] >= .8},
              "limitations": ["Protocol/process benchmark, not model quality or monetary savings.",
                              "Same machine/filesystem/Python, sequential conditions; timing is environment-dependent.",
                              "Structural query only; real Sandbox behavior is tested separately."]}
    print(json.dumps(report, indent=2))
    if not all(report["acceptance"].values()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
