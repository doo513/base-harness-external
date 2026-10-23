#!/usr/bin/env python3
"""Local evaluation of caller ergonomics; no external model calls.

mechanics has no provider calls. prepare/controls freeze an existing benign task.
scripted compares fixed local caller paths using the same reference artifact.
"""
import argparse
import base64
import copy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    sys.modules[name] = value
    spec.loader.exec_module(value)
    return value


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def sha(value):
    return hashlib.sha256(value).hexdigest()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def timed(function, count=5):
    samples, result = [], None
    for _ in range(count):
        began = time.perf_counter()
        result = function()
        samples.append((time.perf_counter() - began) * 1000)
    return result, {"median_ms": statistics.median(samples), "samples_ms": samples}


def mechanics(output, project):
    from harness_external.service import Harness
    from harness_external import worker
    from harness_external.dispatch import waiting
    from change_lens import analyze
    with tempfile.TemporaryDirectory(prefix="lean-mechanics-") as directory:
        root = Path(directory)
        workspace = root / "work"
        workspace.mkdir()
        (workspace / "value.txt").write_text("ready", encoding="utf-8")
        api = Harness(root / "state")
        names = [f"c{i}" for i in range(24)]
        run = api.start(domain_id="develop", goal="Measure declared ready bytes", workspace=str(workspace), request_id="begin",
            mode="exploratory", required_checks=["domain." + name for name in names], parameters={
            "profile": "structural", "inputs": ["value.txt"], "artifacts": ["value.txt"],
            "expectations": [{"id": name, "path": "value.txt", "operator": "equals", "expected": "ready"} for name in names]})["run_id"]
        api.submit(run, "submit")
        with patch.object(worker, "spawn_worker", return_value=None):
            job = api.verify(run, "verify")["job_id"]
        worker.execute_job(api.store.root, job)
        summary, full_time = timed(lambda: waiting(api, run, job, 0))
        brief, brief_time = timed(lambda: waiting(api, run, job, 0, view="brief"))
        detail = copy.deepcopy(brief["details"]["job"])
        raw = bytearray()
        while True:
            response = api.records(run, **detail)
            raw.extend(base64.b64decode(response["data"]))
            if response["next_offset"] is None:
                break
            detail["detail"]["offset"] = response["next_offset"]
        assert sha(raw) == detail["detail"]["sha256"]
        assert json.loads(raw) == summary["job"]
        assert brief["gates"]["status"] == "passed" and brief["selected_job"]["candidate_hash"] == summary["job"]["candidate_hash"]
        cold, cold_time = timed(lambda: analyze(project, cache_dir=root / "cache", refresh=True), 1)
        warm, warm_time = timed(lambda: analyze(project, cache_dir=root / "cache"))
        assert cold.edges == warm.edges and cold.by_path == warm.by_path
        result = {"schema_version": "lean-mechanics-v1", "python": sys.version,
            "check_count": 24, "same_job_detail_recovered": True,
            "summary_bytes": len(encoded(summary)), "brief_bytes": len(encoded(brief)),
            "summary_query": full_time, "brief_query": brief_time,
            "exploration_project": str(project), "exploration_files": len(warm.modules),
            "cold_exploration": cold_time, "warm_exploration": warm_time, "cache": warm.cache_info,
            "limits": ["In-process API projection timings, not model round-trip/billing.",
                       "Brief omits details which were separately recovered and compared.",
                       "Warm exploration is metadata-based advisory reuse, not verification."]}
    save(output, result)
    print(json.dumps(result, ensure_ascii=False))


def study_module():
    return module("lean_existing_study", ROOT / "scripts/agent_study.py")


def copy_runtime(source, destination):
    destination.mkdir(parents=True)
    for name in ("src", "runtime", "skills", "scripts/harness-tool", "pyproject.toml"):
        path = source / name
        target = destination / name
        if path.is_dir():
            shutil.copytree(path, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)


def bounded_driver(source):
    """Keep six independent writers but synchronize pairs within the 8-PID Sandbox.

    No Sandbox setting is relaxed. The original all-at-once list comprehension
    can exceed its process allowance and leak already-started children on failure.
    """
    old = '''                code = "import sys; from journal import Journal; Journal(sys.argv[1]).append(sys.argv[2],1)"
                processes = [subprocess.Popen([sys.executable, "-c", code, str(path), "same" if request["duplicate"] else "k" + str(i)],
                                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for i in range(6)]
                exits = [p.wait(timeout=8) for p in processes]'''
    new = '''                code = ("import sys,time; from pathlib import Path; from journal import Journal\\n"
                        "ready=Path(sys.argv[3]); (ready/sys.argv[4]).write_bytes(b'1')\\n"
                        "deadline=time.monotonic()+5\\n"
                        "while len(list(ready.iterdir()))<2:\\n"
                        "    if time.monotonic()>deadline: raise TimeoutError('pair barrier')\\n"
                        "    time.sleep(.001)\\n"
                        "Journal(sys.argv[1]).append(sys.argv[2],1)\\n")
                exits = []
                for offset in range(0, 6, 2):
                    ready = Path(directory) / ('ready-' + str(offset))
                    ready.mkdir()
                    processes = []
                    try:
                        for i in range(offset, offset + 2):
                            processes.append(subprocess.Popen([sys.executable, "-c", code, str(path),
                                "same" if request["duplicate"] else "k" + str(i), str(ready), str(i)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
                        exits.extend(p.wait(timeout=8) for p in processes)
                    finally:
                        for process in processes:
                            if process.poll() is None:
                                process.kill()
                            process.wait(timeout=8)'''
    if source.count(old) != 1:
        raise ValueError("The evaluator driver changed; inspect it instead of applying a guessed transformation")
    return source.replace(old, new)


def prepare(root, before):
    if root.exists():
        raise ValueError("Refusing to overwrite an existing study")
    root.mkdir(parents=True)
    study = study_module()
    task = next(t for t in study.tasks() if t["id"] == "journal")
    for label, source in (("before", before), ("after", ROOT)):
        copy_runtime(source, root / "sources" / label)
    private = root / "private"
    private.mkdir()
    driver = bounded_driver((ROOT / "scripts/study_subject_driver.py").read_text(encoding="utf-8"))
    (private / "subject_driver.py").write_text(driver, encoding="utf-8")
    shutil.copytree(ROOT / "evaluation/paired_references/journal", private / "reference", ignore=shutil.ignore_patterns("__pycache__"))
    save(private / "task.json", task)
    policy_dir = root / "policies" / "journal"
    bundle = policy_dir / "bundle"
    (bundle / "acceptance_tests").mkdir(parents=True)
    (bundle / "acceptance_tests/test_task.py").write_text(task["acceptance_test"], encoding="utf-8")
    (bundle / "SPEC.md").write_text(task["spec"] + "\n", encoding="utf-8")
    policy = {"schema_version":"acceptance-policy-v1", "policy_id":"journal", "revision":"lean-study-1", "domain_id":"develop",
        "parameters": {"profile":"execution", "inputs":[*task["sources"], "SPEC.md", "acceptance_tests/test_task.py"],
            "artifacts":list(task["sources"]), "expectations":[
                {"id":"library", "path":"journal.py", "operator":"contains", "expected":"class Journal"},
                {"id":"cli", "path":"journal_cli.py", "operator":"contains", "expected":"def main"}],
            "execution_checks":[{"kind":"command", "id":"public", "argv":["python3","-m","unittest","discover","-s","acceptance_tests","-v"], "timeout_seconds":30}]},
        "requirements":[{"id":"journal-contract", "statement":"Preserve the specified Journal API and pass the pinned public regression examples", "minimum_evidence":"command",
                         "check_ids":["domain.library","domain.cli","domain.public"]}],
        "required_check_ids":["domain.library","domain.cli","domain.public"],
        "bundle":{"version":"public-regression-1", "files":["SPEC.md","acceptance_tests/test_task.py"]},
        "approval":{"declared_by":"evaluation configuration", "reference":"same frozen task and public tests for A/B/C"},
        "authorship":{"criteria":"model","tests":"model"}}
    save(policy_dir / "policy.json", policy)
    save(root / "policies/registry.json", {"defaults":{"develop":"journal"}})
    for arm in ("A","B","C"):
        workspace = root / "trials" / arm / "workspace"
        (workspace / "acceptance_tests").mkdir(parents=True)
        for name, body in task["sources"].items():
            (workspace / name).write_text(body, encoding="utf-8")
        (workspace / "SPEC.md").write_text(task["spec"] + "\n", encoding="utf-8")
        (workspace / "acceptance_tests/test_task.py").write_text(task["acceptance_test"], encoding="utf-8")
    initial = [study.file_set(root / "trials" / arm / "workspace") for arm in ("A","B","C")]
    assert initial[0] == initial[1] == initial[2]
    plan = {"schema_version":"lean-local-pilot-v1", "created_at":datetime.now(timezone.utc).isoformat(),
        "model_task_execution":False, "model":None, "billed_tokens":None, "task":"journal",
        "arms":{"A":"previous minimal workflow", "B":"new lightweight workflow", "C":"B plus optional deterministic exploration"},
        "initial_files":initial[0], "policy_hash":sha(encoded(policy)), "task_hash":sha(encoded(task)),
        "definition_hashes":study.file_set(private), "policy_files":study.file_set(root / "policies"),
        "grading_controller_hash":sha((ROOT / "scripts/agent_study.py").read_bytes()),
        "concurrency":{"independent_writers":6,"simultaneous_writers":2,"sandbox_process_limit":8},
        "runtime_hashes":{label:study.file_set(root / "sources" / label) for label in ("before","after")},
        "limitations":["A fixed local caller installs the same reference; no model develops an artifact.",
                       "One trial per condition; timings include noise and do not establish model quality/token benefit.",
                       "The same Python launches separate local servers; this comparison does not measure configuration-time pinning.",
                       "Public gates are partial examples; held-out evaluator outcomes are separate local observations."]}
    save(root / "plan.json", plan)
    print(json.dumps({"prepared":str(root), "initial_files_equal":True, "model_task_execution":False}))


def validate(root):
    study = study_module()
    plan = json.loads((root / "plan.json").read_text(encoding="utf-8"))
    task = json.loads((root / "private/task.json").read_text(encoding="utf-8"))
    if sha(encoded(task)) != plan["task_hash"]:
        raise ValueError("Frozen task changed")
    policy = json.loads((root / "policies/journal/policy.json").read_text(encoding="utf-8"))
    if sha(encoded(policy)) != plan["policy_hash"]:
        raise ValueError("Frozen policy changed")
    if study.file_set(root / "private") != plan.get("definition_hashes") or study.file_set(root / "policies") != plan.get("policy_files"):
        raise ValueError("Frozen evaluator/bundle files changed or belong to an older setup")
    if sha((ROOT / "scripts/agent_study.py").read_bytes()) != plan.get("grading_controller_hash"):
        raise ValueError("Shared grading controller changed")
    for label in ("before","after"):
        if study.file_set(root / "sources" / label) != plan["runtime_hashes"][label]:
            raise ValueError("Frozen runtime changed")
    return study, plan, task


def controls(root):
    study, _, task = validate(root)
    baseline = study.grade(root, task, root / "trials/A/workspace", root / "receipts/baseline.json")
    positive = study.grade(root, task, root / "private/reference", root / "receipts/positive.json")
    result = {"baseline":baseline["passed"], "positive":positive["passed"], "total":positive["total"],
              "valid":baseline["status"]=="completed" and not baseline["all_passed"] and positive["all_passed"]}
    save(root / "receipts/controls.json", result)
    print(json.dumps(result))
    return 0 if result["valid"] else 1


def scripted(root):
    """Exercise real portable CLI/socket/Sandbox paths, without a model/provider."""
    study, plan, task = validate(root)
    if plan.get("schema_version") != "lean-local-pilot-v1":
        raise ValueError("Prepare a new local study; do not relabel a frozen earlier plan")
    control = json.loads((root / "receipts/controls.json").read_text(encoding="utf-8"))
    if not control.get("valid"):
        raise ValueError("Both negative and positive controls must work before comparison")
    for arm in ("A", "B", "C"):
        trial = root / "trials" / arm
        if (trial / "client.json").exists() or study.file_set(trial / "workspace") != plan["initial_files"]:
            raise ValueError("Trials must be unused with identical initial files; do not silently retry a mutation")
    results = {}
    for arm in ("A", "B", "C"):
        trial = root / "trials" / arm
        workspace = trial / "workspace"
        runtime = root / "sources" / ("before" if arm == "A" else "after")
        endpoint_dir = trial / "transport"
        endpoint_dir.mkdir(mode=0o700)
        endpoint = endpoint_dir / "core.sock"
        config_path, binding = trial / "client.json", trial / "binding.json"
        save(config_path, {"schema_version":1, "harness_repo":str(runtime), "state_dir":str(trial / "state"),
             "state_lifetime":"ephemeral", "policy_root":str(root / "policies"), "server_socket":str(endpoint)})
        config_path.chmod(0o600)
        bootstrap = "import sys; sys.path.insert(0,sys.argv.pop(1)); from harness_external.__main__ import main; raise SystemExit(main())"
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE":"1"}
        calls = []
        prefix = [sys.executable, "-B", str(runtime / "skills/harness-workflow/scripts/harness_client.py"),
                  "--config", str(config_path), "--trace-file", str(trial / "trace.jsonl")]

        def call(label, arguments, *, phase="workflow"):
            began = time.perf_counter()
            process = subprocess.run([*prefix, *arguments], capture_output=True, timeout=60, env=env)
            receipt = {"phase":phase, "label":label, "argv":arguments, "returncode":process.returncode,
                       "elapsed_ms":(time.perf_counter()-began)*1000, "stdout_bytes":len(process.stdout),
                       "stderr_bytes":len(process.stderr)}
            calls.append(receipt)
            (trial / (label + ".stdout")).write_bytes(process.stdout)
            (trial / (label + ".stderr")).write_bytes(process.stderr)
            save(trial / "calls.json", calls)
            value = json.loads(process.stdout)
            if process.returncode or not value.get("ok"):
                raise RuntimeError(f"{arm}/{label} failed; preserved receipt: {trial}")
            return value

        with (trial / "server.log").open("wb") as log:
            server = subprocess.Popen([sys.executable, "-I", "-B", "-c", bootstrap, str(runtime / "src"),
                "--state-dir", str(trial / "state"), "--policy-root", str(root / "policies"), "serve",
                "--socket", str(endpoint), "--workspace-root", str(workspace)], stdout=log, stderr=log, env=env)
            try:
                deadline = time.monotonic()+15
                while not endpoint.exists():
                    if server.poll() is not None or time.monotonic() > deadline:
                        raise RuntimeError(f"{arm} local server unavailable; inspect {trial / 'server.log'}")
                    time.sleep(.05)
                preflight = call("preflight", ["preflight"], phase="setup")
                if not preflight.get("command_execution_verified"):
                    raise RuntimeError("Strict Sandbox unavailable; no Host fallback")
                arguments = {"domain":"develop", "goal":task["spec"], "workspace":str(workspace), "mode":"acceptance",
                             "provenance":{"declared_author":"application"}}
                start = call("start", ["call", "--operation", "start", "--arguments", "json:"+json.dumps(arguments),
                    "--action-id", "start-local-trial", "--binding-output", str(binding), "--format", "compact"])
                # Fixed controller action, explicitly not evidence of model task execution.
                for name in task["sources"]:
                    shutil.copy2(root / "private/reference" / name, workspace / name)
                if arm == "C":
                    for label in ("explore-cold", "explore-warm"):
                        call(label, ["explore", "context", str(workspace), "--changed", "journal.py", "--limit", "8",
                             "--cache-dir", str(trial / "exploration-cache"), "--format", "json"], phase="exploration")
                checkpoint = call("checkpoint", ["checkpoint", "--binding", str(binding), "--action-id", "reference-candidate",
                                                  "--wait-seconds", "30"])
                for attempt in range(3):
                    if checkpoint["wait_status"] != "pending":
                        break
                    selected = checkpoint.get("selected_job") or checkpoint.get("job")
                    checkpoint = call("wait-"+str(attempt), ["wait", "--binding", str(binding), "--job-id", selected["job_id"],
                                                            "--wait-seconds", "30"])
                selected = checkpoint.get("selected_job") or checkpoint.get("job")
                measurement = selected.get("result_status") or (selected.get("result") or {}).get("status")
                if checkpoint["wait_status"] != "terminal" or measurement != "passed":
                    raise RuntimeError(f"{arm} did not reach a measured pass; no automatic repair/finish")
                status = call("status", ["call", "--operation", "status", "--binding", str(binding),
                              "--arguments", 'json:{"check_workspace":true}', "--format", "compact"])
                if not status.get("current_workspace_matches_submitted_files") or status["gates"]["status"] != "passed":
                    raise RuntimeError(f"{arm} workspace/gate mismatch; no finish")
                closed = call("finish", ["call", "--operation", "finish", "--binding", str(binding),
                    "--arguments", 'json:{"outcome":"completed","summary":"Fixed local reference passed declared public checks; no model quality claim."}',
                    "--action-id", "close-local-trial", "--format", "compact"])
            finally:
                server.terminate()
                try:
                    server.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=5)
        grade = study.grade(root, task, workspace, trial / "grade.json")
        events = [json.loads(line) for line in (trial / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
        transport = [event for event in events if event.get("kind") == "transport" and event.get("operation") != "doctor"]
        workflow = [item for item in calls if item["phase"] == "workflow"]
        exploration = [item for item in calls if item["phase"] == "exploration"]
        results[arm] = {"run_id":start["run_id"], "task_id":start.get("task_id"),
            "job_id":selected["job_id"], "candidate_hash":selected["candidate_hash"], "model_task_execution":False,
            "input_tokens":None, "output_tokens":None, "billed_tokens":None,
            "workflow_calls":len(workflow), "transport_calls":len(transport),
            "workflow_stdout_bytes":sum(item["stdout_bytes"] for item in workflow),
            "transport_response_bytes":sum(item["response_bytes"] for item in transport),
            "workflow_elapsed_ms":sum(item["elapsed_ms"] for item in workflow),
            "checkpoint_stdout_bytes":next(item["stdout_bytes"] for item in calls if item["label"]=="checkpoint"),
            "exploration_calls":len(exploration), "exploration_stdout_bytes":sum(item["stdout_bytes"] for item in exploration),
            "exploration_elapsed_ms":sum(item["elapsed_ms"] for item in exploration),
            "grade":{"passed":grade["passed"], "total":grade["total"], "all_passed":grade["all_passed"]},
            "closure":closed, "source_hashes":{name:sha((workspace/name).read_bytes()) for name in task["sources"]}}
        save(trial / "result.json", results[arm])
        print(json.dumps({"arm":arm, **{k:v for k,v in results[arm].items() if k not in {"closure","source_hashes"}}}), flush=True)
    if not results["A"]["source_hashes"] == results["B"]["source_hashes"] == results["C"]["source_hashes"]:
        raise AssertionError("Final task sources differ")
    validate(root)
    save(root / "receipts/local-comparison.json", {"schema_version":"lean-local-comparison-v1", "python":sys.version,
        "model_task_execution":False, "policy_hash":plan["policy_hash"], "arms":results, "limitations":plan["limitations"]})
    return 0 if all(item["grade"]["all_passed"] for item in results.values()) else 1



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    metrics = sub.add_parser("mechanics")
    metrics.add_argument("--output", type=Path, required=True)
    metrics.add_argument("--project", type=Path, default=ROOT)
    prep = sub.add_parser("prepare")
    prep.add_argument("--output", type=Path, required=True)
    prep.add_argument("--before-runtime", type=Path, required=True)
    control = sub.add_parser("controls")
    control.add_argument("--root", type=Path, required=True)
    local = sub.add_parser("scripted")
    local.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "mechanics":
        mechanics(args.output, args.project)
    elif args.command == "prepare":
        prepare(args.output.absolute(), args.before_runtime.absolute())
    elif args.command == "controls":
        return controls(args.root.absolute())
    else:
        return scripted(args.root.absolute())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
