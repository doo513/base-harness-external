"""Opt-in, fixed-task agent comparison. This is NOT the Harness execution runtime.

prepare/controls/qualify/summarize do not invoke a model. run explicitly launches
one selected existing CLI over the frozen finite trial list, keeping raw failures.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time


REPO = Path(__file__).resolve().parents[1]
BASE = "8dad67a475dfeaedd8e3653f82697be5165e20cf"
HOSTS = {"codex": {"model": "gpt-6-luna", "effort": "medium"}, "agy": {"model": "gemini-3.8-flash-high", "effort": "model-high"}}
RUNTIME_PATHS = ("src/harness", "src/harness_external", "runtime", "skills", "scripts/harness-tool", "pyproject.toml", "README.md", "INSTALL.md", "CALLER_USAGE.md", "POLICIES.md", "AGENTS.md")
MAX_SECONDS = 600
REPAIR_SECONDS = 300
SCHEMA = {"type": "object", "properties": {"status": {"type": "string", "enum": ["completed", "partial", "blocked"]},
          "summary": {"type": "string"}, "uncertainties": {"type": "array", "items": {"type": "string"}},
          "harness_run_id": {"type": ["string", "null"]}},
          "required": ["status", "summary", "uncertainties", "harness_run_id"], "additionalProperties": False}
COMMON = """Work only in the assigned trial directory, using Python standard library and existing local tools.
Do not install dependencies, use network/search/browser tools, spawn subagents, read credentials,
modify global configuration, inspect other trials or hidden evaluator files. Do not edit Harness
source, its state files directly, operator policy/configuration, or provided acceptance_tests.
You may add your own tests and notes. The supplied acceptance tests cover only part of the written
requirements; satisfying them is not a claim of complete goal coverage. Preserve all required
behavior and inspect actual outputs. Your final response is one JSON object with status
(completed/partial/blocked), summary, uncertainties (string array), harness_run_id (string/null).
Report partial work or untested behavior honestly. The initial end-to-end budget is 600 seconds.
"""


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, value, *, exclusive=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x" if exclusive else "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def tasks():
    spec = importlib.util.spec_from_file_location("paired_task_definitions", REPO / "evaluation/paired_tasks.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.TASKS


def file_set(directory):
    result = {}
    for path in sorted(directory.rglob("*")):
        if any(part in {".git", "__pycache__", ".pytest_cache"} for part in path.relative_to(directory).parts):
            continue
        if path.is_symlink():
            raise ValueError("Study artifact contains a symlink: " + str(path))
        if path.is_file():
            result[path.relative_to(directory).as_posix()] = digest(path.read_bytes())
    return result


def snapshot_runtime(target, *, before=False):
    target.mkdir(parents=True)
    if before:
        listed = subprocess.check_output(["git", "ls-tree", "-r", "--name-only", BASE], cwd=REPO, text=True).splitlines()
        for name in listed:
            if any(name == prefix or name.startswith(prefix + "/") for prefix in RUNTIME_PATHS):
                path = target / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(subprocess.check_output(["git", "show", BASE + ":" + name], cwd=REPO))
    else:
        for name in RUNTIME_PATHS:
            source = REPO / name
            if source.is_dir():
                shutil.copytree(source, target / name, ignore=shutil.ignore_patterns("__pycache__"))
            elif source.is_file():
                (target / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target / name)
    return file_set(target)


def profile(task):
    names = list(task["sources"])
    entries = [{"id": "entry" + str(index), "path": name, "operator": "contains", "expected": "Journal" if name == "journal.py" else
                "normalize" if name == "settings.py" else "plan" if name == "planner.py" else "main"} for index, name in enumerate(names)]
    return {"profile": "execution", "inputs": names + ["acceptance_tests/test_task.py"], "artifacts": names,
            "expectations": entries, "test_commands": [{"id": "behavior", "argv": ["python3", "-m", "unittest", "discover", "-s", "acceptance_tests", "-v"], "timeout_seconds": 30}]}


def task_prompt(root, trial, row, task):
    result = COMMON + "\nTask requirements:\n" + task["spec"] + "\nWorkspace: " + str(trial / "workspace")
    if row["arm"] == "plain":
        return result + "\nUse ordinary development/tests. Do not call Harness or its Skill. Set harness_run_id to null."
    runtime = root / "sources" / row["arm"]
    result += ("\nUse the Harness workflow Skill at " + str(runtime / "skills/harness-workflow/SKILL.md") +
               ". Existing caller configuration is " + str(trial / "client.json") +
               ". Start one develop Run before editing, preserving the original request. "
               "Do not weaken the mandatory acceptance checks or change their test files. "
               "Additional exploratory tests are your choice. Declare caller proposals as model-authored. "
               "Use the real strict Sandbox, never an unsandboxed fallback. Include the actual Run ID in your final JSON. ")
    if row["arm"] == "before":
        result += ("This version uses explicit caller configuration: the Domain parameters are in " + str(trial / "task-parameters.json") +
                   ". Use mode exploratory with required_check IDs domain.entry0, domain.entry1, domain.behavior. "
                   "Retain that mandatory scope for the whole Run; it is the operator's supplied task policy, not permission to choose weaker criteria.")
    else:
        result += ("This version has the same task policy registered as the operator's Domain default. Use mode acceptance; "
                   "parameters may be omitted. Capture the pre-edit baseline and obtain an original-versus-submitted comparison. "
                   "Use the bundled mechanical helper operations where useful; they do not decide task success.")
    return result


def prepare(root):
    started = time.monotonic()
    if root.exists() and any(root.iterdir()):
        raise ValueError("prepare requires a new or empty study directory; never overwrite a study")
    if root.resolve().is_relative_to(REPO) or REPO.is_relative_to(root.resolve()):
        raise ValueError("Study and implementation checkout must be disjoint")
    for name in ("private", "receipts", "trials", "sources", "policies"):
        (root / name).mkdir(parents=True, exist_ok=True)
    corpus = tasks()
    write_json(root / "private/corpus.json", corpus, exclusive=True)
    shutil.copy2(REPO / "scripts/study_subject_driver.py", root / "private/subject_driver.py")
    shutil.copytree(REPO / "evaluation/paired_references", root / "private/references")
    source_hashes = {arm: snapshot_runtime(root / "sources" / arm, before=arm == "before") for arm in ("before", "after")}
    shutil.copy2(REPO / "scripts/study_scope.py", root / "scope.py")
    write_json(root / "response-schema.json", SCHEMA, exclusive=True)
    rows = []
    for task in corpus:
        parent = root / "policies" / task["id"]
        policy = parent / task["id"]
        bundle = policy / "bundle/acceptance_tests"
        bundle.mkdir(parents=True)
        (bundle / "test_task.py").write_text(task["acceptance_test"])
        ids = ["domain.entry0", "domain.entry1", "domain.behavior"]
        write_json(parent / "registry.json", {"defaults": {"develop": task["id"]}})
        write_json(policy / "policy.json", {"schema_version": "acceptance-policy-v1", "policy_id": task["id"], "revision": "1", "domain_id": "develop",
                   "parameters": profile(task), "required_check_ids": ids,
                   "requirements": [{"id": "written-task", "statement": "Preserve and implement the written task; supplied checks are partial coverage", "check_ids": ids, "minimum_evidence": "command"}],
                   "bundle": {"version": "study-1", "files": ["acceptance_tests/test_task.py"]},
                   "authorship": {"criteria": "model", "tests": "model"},
                   "approval": {"declared_by": "internal evaluation setup", "reference": "Fixed operator configuration; not authenticated human approval or third-party audit"}})
    for host_index, host in enumerate(HOSTS):
        for task_index, task in enumerate(corpus):
            arms = ["plain", "before", "after"]
            shift = (task_index + host_index) % 3
            arms = arms[shift:] + arms[:shift]
            if host_index:
                arms.reverse()
            for arm in arms:
                row = {"id": host + "__" + task["id"] + "__" + arm, "host": host, "task": task["id"], "arm": arm}
                trial = root / "trials" / row["id"]
                workspace = trial / "workspace"
                (workspace / "acceptance_tests").mkdir(parents=True)
                for name, source in task["sources"].items():
                    (workspace / name).write_text(source)
                (workspace / "acceptance_tests/test_task.py").write_text(task["acceptance_test"])
                (workspace / "TASK.md").write_text(task["spec"] + "\n")
                (workspace / "AGENTS.md").write_text(COMMON)
                subprocess.run(["git", "init", "--quiet", str(workspace)], check=True)
                if arm != "plain":
                    config = {"schema_version": 1, "harness_repo": str(root / "sources" / arm), "state_dir": str(trial / "state"), "state_lifetime": "persistent"}
                    if arm == "after":
                        config["policy_root"] = str(root / "policies" / task["id"])
                    write_json(trial / "client.json", config)
                write_json(trial / "task-parameters.json", profile(task))
                (trial / "prompt.txt").write_text(task_prompt(root, trial, row, task))
                row.update(initial_files=file_set(workspace), prompt_hash=digest((trial / "prompt.txt").read_bytes()))
                rows.append(row)
    manifest = {"schema_version": "paired-agent-study-v2", "created_at": datetime.now(timezone.utc).isoformat(), "hosts": HOSTS,
                "before_commit": BASE, "after_source": "uncommitted frozen runtime file hashes", "source_files": source_hashes,
                "definition_hashes": file_set(root / "private"), "policy_hashes": file_set(root / "policies"),
                "scope_hash": digest((root / "scope.py").read_bytes()), "controller_hash": digest(Path(__file__).read_bytes()),
                "initial_seconds": MAX_SECONDS, "repair_seconds": REPAIR_SECONDS, "trials": rows,
                "repair_policy": "At most one fresh-Host repair on a failed artifact, with deterministic failed-case feedback; no manual solver intervention. Journal always has a fresh-Host recovery/inspection session, even when the first artifact passes.",
                "comparison": "ordinary workflow vs previous caller vs configured acceptance/mechanical caller; whole workflow change, not one-factor causal isolation",
                "primary": "all fixed holdout observations match; initial and post-repair results reported separately",
                "secondary": ["scope consistency", "baseline transitions", "false completion", "elapsed time", "raw CLI token counters", "tool calls", "repair effort", "human interventions"],
                "limits": ["Three multi-file stdlib tasks, one cell each: internal pilot, not statistical superiority.",
                           "Runtime and task setup are measured separately, not attributed to model solve time.",
                           "After arm includes mandatory baseline comparison; any extra cost is included and is not isolated from caller changes.",
                           "Fixed checks and positive controls are model-authored; not a third-party evaluation.",
                           "Raw CLI token counters differ; no monetary or cross-provider token equivalence claimed."]}
    manifest["automatic_setup_seconds"] = time.monotonic() - started
    manifest["unmeasured_costs"] = ["Task/test authoring", "Human review time", "Provider monetary/subscription charges"]
    write_json(root / "plan.json", manifest, exclusive=True)
    (root / "plan.sha256").write_text(digest((root / "plan.json").read_bytes()) + "\n")
    print(json.dumps({"prepared": len(rows), "plan_hash": digest((root / "plan.json").read_bytes())}))


def validate_plan(root):
    raw = (root / "plan.json").read_bytes()
    if digest(raw) != (root / "plan.sha256").read_text().strip():
        raise ValueError("Frozen plan changed")
    plan = json.loads(raw)
    for arm, expected in plan["source_files"].items():
        if file_set(root / "sources" / arm) != expected:
            raise ValueError("Frozen runtime changed: " + arm)
    for directory, field in (("private", "definition_hashes"), ("policies", "policy_hashes")):
        if file_set(root / directory) != plan[field]:
            raise ValueError("Frozen evaluation definitions changed: " + directory)
    if digest((root / "scope.py").read_bytes()) != plan["scope_hash"] or digest(Path(__file__).read_bytes()) != plan["controller_hash"]:
        raise ValueError("Evaluation runner changed after plan freeze")
    return plan


def same(expected, actual):
    if isinstance(expected, bool) or isinstance(actual, bool):
        return type(expected) is type(actual) and expected == actual
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return expected == actual
    if type(expected) is not type(actual):
        return False
    if isinstance(expected, dict):
        return expected.keys() == actual.keys() and all(same(v, actual[k]) for k, v in expected.items())
    if isinstance(expected, list):
        return len(expected) == len(actual) and all(same(a, b) for a, b in zip(expected, actual))
    return expected == actual


def grade(root, task, workspace, destination):
    started = time.monotonic()
    hashes = file_set(workspace)
    with tempfile.TemporaryDirectory(prefix="harness-study-grade-") as temporary:
        payload = Path(temporary)
        for relative in hashes:
            target = payload / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(workspace / relative, target)
        if (payload / "_study_driver.py").exists() or (payload / "_study_requests.json").exists():
            raise ValueError("Candidate collides with reserved evaluator filenames")
        shutil.copy2(root / "private/subject_driver.py", payload / "_study_driver.py")
        write_json(payload / "_study_requests.json", [item["request"] for item in task["cases"]])
        request = {"workspace": str(payload), "cwd": ".", "argv": ["python3", "_study_driver.py", "_study_requests.json"], "timeoutMs": 60000}
        process = subprocess.run([shutil.which("bun"), "run", str(root / "sources/after/runtime/script/external-harness-sandbox.ts")],
                                 input=json.dumps(request), capture_output=True, text=True, timeout=90)
        receipt = json.loads(process.stdout)
        capture = receipt.get("capture", {})
        observed = None
        if receipt.get("status") == "completed" and capture.get("exitCode") == 0:
            marker = "__STUDY_OBSERVATIONS__"
            lines = [line[len(marker):] for line in capture.get("stdout", "").splitlines() if line.startswith(marker)]
            if len(lines) == 1:
                observed = json.loads(lines[0])
        valid = isinstance(observed, list) and len(observed) == len(task["cases"])
        cases = [{"name": item["name"], "passed": same(item["expected"], actual), "actual": actual}
                 for item, actual in zip(task["cases"], observed)] if valid else []
        result = {"task": task["id"], "status": "completed" if valid else "unavailable" if receipt.get("status") == "not_run" else "subject_execution_failed",
                  "source_files": hashes, "case_set_hash": digest(encoded(task["cases"])), "cases": cases,
                  "passed": sum(item["passed"] for item in cases), "total": len(task["cases"]),
                  "all_passed": valid and all(item["passed"] for item in cases), "sandbox": receipt,
                  "elapsed_seconds": time.monotonic() - started}
        write_json(destination, result)
        return result


def controls(root):
    validate_plan(root)
    outcomes = []
    for task in load(root / "private/corpus.json"):
        with tempfile.TemporaryDirectory(prefix="harness-study-baseline-") as directory:
            for name, source in task["sources"].items():
                (Path(directory) / name).write_text(source)
            baseline = grade(root, task, Path(directory), root / "receipts/controls" / (task["id"] + "-baseline.json"))
        positive = grade(root, task, root / "private/references" / task["id"], root / "receipts/controls" / (task["id"] + "-positive.json"))
        outcome = {"task": task["id"], "baseline": baseline["passed"], "positive": positive["passed"], "total": positive["total"],
                   "valid": baseline["status"] == "completed" and not baseline["all_passed"] and positive["all_passed"]}
        outcomes.append(outcome)
    write_json(root / "receipts/controls.json", {"passed": all(x["valid"] for x in outcomes), "outcomes": outcomes})
    print(json.dumps(outcomes))
    return 0 if all(x["valid"] for x in outcomes) else 1


def cli_argv(host, root, trial, prompt):
    if host == "codex":
        return ["codex", "exec", "--ignore-user-config", "--ignore-rules", "--skip-git-repo-check", "--ephemeral", "--json", "--color", "never",
                "-m", HOSTS[host]["model"], "-c", 'model_reasoning_effort=' + json.dumps(HOSTS[host]["effort"]), "-s", "danger-full-access", "--add-dir", str(trial),
                "--output-schema", str(root / "response-schema.json"), prompt]
    return ["agy", "--model", HOSTS[host]["model"], "--mode", "accept-edits", "--sandbox=false", "--new-project",
            "--output-format", "stream-json", "--print-timeout", "0", "--json-schema", str(root / "response-schema.json"), "--print", prompt]


def parse_trace(host, path):
    usage, final, claim, session, errors, seen = None, "", None, None, [], set()
    for line in path.read_text(errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if host == "codex":
            if event.get("type") == "thread.started":
                session = event.get("thread_id")
            if event.get("type") == "turn.completed":
                usage = event.get("usage")
            if event.get("type") in {"turn.failed", "error"}:
                errors.append(event)
            if event.get("type") == "item.completed":
                item = event["item"]
                if item.get("type") == "agent_message":
                    final = item.get("text", "")
                elif item.get("type") in {"command_execution", "file_change", "mcp_tool_call", "web_search"}:
                    seen.add(item.get("id"))
        else:
            if event.get("event") == "init":
                session = event.get("conversation_id")
            if event.get("event") == "step_update":
                step = event["step_update"]
                if step.get("step_type") == "tool" and step.get("state") == "DONE":
                    seen.add(step["step_index"])
            if event.get("event") == "result":
                value = event["result"]
                final, usage, claim = value.get("response", ""), value.get("usage"), value.get("structured_output")
                if value.get("status") != "SUCCESS":
                    errors.append(value)
    if not isinstance(claim, dict):
        try:
            claim = json.loads(final.strip().removeprefix("```json").removesuffix("```").strip())
        except ValueError:
            claim = None
    return {"session_id": session, "usage": usage, "claim": claim if isinstance(claim, dict) else None,
            "final_text": final, "tool_calls": len(seen), "errors": errors}


def ensure_task_execution(result):
    if result.get("errors") and result.get("tool_calls", 0) == 0 and result.get("claim") is None:
        raise RuntimeError("Model invocation failed before task execution; receipt retained. Stop this Host instead of scoring unchanged stubs or issuing more attempts.")


def invoke(root, row, stage, prompt, seconds):
    trial = root / "trials" / row["id"]
    output = root / "receipts" / row["id"] / stage
    if output.exists():
        raise ValueError("Refusing duplicate invocation; inspect existing stage first: " + str(output))
    output.mkdir(parents=True)
    command = [sys.executable, str(root / "scope.py"), str(root), str(trial), str(REPO), *cli_argv(row["host"], root, trial, prompt)]
    started, timed_out = time.monotonic(), False
    with (output / "stdout.jsonl").open("w") as stdout, (output / "stderr.txt").open("w") as stderr:
        process = subprocess.Popen(command, cwd=trial / "workspace", stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, start_new_session=True)
        write_json(output / "started.json", {"pid": process.pid, "started_at": datetime.now(timezone.utc).isoformat(),
                   "started_monotonic": started, "deadline_monotonic": started + seconds,
                   "command_without_prompt": command[:-1], "prompt_hash": digest(prompt.encode()), "seconds": seconds})
        print(json.dumps({"started": row["id"], "stage": stage, "pid": process.pid}), flush=True)
        try:
            process.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
    finished = time.monotonic()
    scope_events = []
    for line in (output / "stdout.jsonl").read_text(errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "study.scope.exit":
            scope_events.append(event)
    confirmed = bool(scope_events) and not scope_events[-1]["remaining_namespace_pids"]
    result = {"exit_code": process.returncode, "timed_out": timed_out, "wall_seconds": finished - started,
              "ended_monotonic": finished, "ended_at": datetime.now(timezone.utc).isoformat(),
              "scope_events": scope_events, "termination_confirmed": confirmed,
              "trace_hash_at_exit": digest((output / "stdout.jsonl").read_bytes()),
              **parse_trace(row["host"], output / "stdout.jsonl")}
    write_json(output / "process.json", result)
    if not confirmed:
        raise RuntimeError("Scope termination was not confirmed; do not launch another trial or infer completion")
    ensure_task_execution(result)
    return result


def harness_command(root, row, args):
    runtime = root / "sources" / row["arm"]
    p = subprocess.run(["bash", str(runtime / "scripts/harness-tool"), "--state-dir", str(root / "trials" / row["id"] / "state"), *args], capture_output=True, text=True, timeout=30)
    return json.loads(p.stdout)


def harvest(root, row, output):
    state = root / "trials" / row["id"] / "state/runs.sqlite3"
    if row["arm"] == "plain" or not state.exists():
        return []
    listing = harness_command(root, row, ["list-runs", "--limit", "100"])
    summaries = []
    for item in listing.get("items", []):
        summary = harness_command(root, row, ["status", "--run-id", item["run_id"], "--check-workspace"])
        write_json(output / (item["run_id"] + ".json"), summary)
        histories = {}
        for kind in ("checks", "measurements"):
            offset, collected = 0, []
            for _ in range(20):
                page = harness_command(root, row, ["records", "--run-id", item["run_id"], "--kind", kind, "--offset", str(offset), "--limit", "100"])
                collected.extend(page.get("items", []))
                offset = page.get("next_offset")
                if offset is None:
                    break
            histories[kind] = collected
            write_json(output / (item["run_id"] + "-" + kind + ".json"), {"items": collected, "truncated": offset is not None})
        current_subject = (summary.get("candidate") or {}).get("subject")
        commands = [m for m in histories["measurements"] if m.get("subject_role", "candidate") == "candidate"
                    and m.get("subject") == current_subject and m.get("check_set_hash") == summary.get("check_set_hash")
                    and any(c["check_id"] == m["check_id"] and c["kind"] == "command" for c in summary.get("checks", []))]
        latest = {c["check_id"]: c for c in histories["checks"]}
        mandatory = {key: latest[key]["spec"] for key in (summary.get("policy") or {}).get("required_check_ids", []) if key in latest}
        summaries.append({"run_id": item["run_id"], "resolution": summary.get("resolution"), "usage": summary.get("usage"),
                          "history_counts": summary.get("history_counts"), "policy": summary.get("policy"),
                          "workspace_matches": summary.get("current_workspace_matches_submitted_files"),
                          "mandatory_specs": mandatory,
                          "command_passes": sum(m["comparison_status"] == "passed" for m in commands),
                          "baseline_comparison": summary.get("measurement", {}).get("baseline_comparison")})
    return summaries


def run_host(root, host):
    plan = validate_plan(root)
    if not load(root / "receipts/controls.json")["passed"] or not load(root / "receipts/qualification.json")["passed"]:
        raise ValueError("Positive/negative controls and scope qualification must pass first")
    corpus = {item["id"]: item for item in load(root / "private/corpus.json")}
    for row in (row for row in plan["trials"] if row["host"] == host):
        output = root / "receipts" / row["id"]
        if (output / "result.json").exists():
            continue
        if output.exists():
            raise ValueError("Unresolved previous invocation; inspect/recover without rerunning: " + row["id"])
        trial, task = root / "trials" / row["id"], corpus[row["task"]]
        if file_set(trial / "workspace") != row["initial_files"] or digest((trial / "prompt.txt").read_bytes()) != row["prompt_hash"]:
            raise ValueError("Initial task input changed")
        stages = []
        for index in range(2):
            stage = "initial" if index == 0 else "repair" if not stages[0]["grade"]["all_passed"] else "recovery"
            if stage == "initial":
                prompt, seconds = (trial / "prompt.txt").read_text(), MAX_SECONDS
            else:
                if (stages[0]["grade"]["all_passed"] and row["task"] != "journal") or stages[0]["grade"]["status"] == "unavailable":
                    break
                failed = [item["name"] for item in stages[0]["grade"]["cases"] if not item["passed"]]
                prompt = (COMMON.replace("600 seconds", "300 seconds") + "\nContinue this same task from its existing files in " + str(trial / "workspace") +
                          ". This is a fresh Host session. Inspect TASK.md and your saved notes/Run binding. " +
                          ("The fixed evaluation found failures: " + json.dumps(failed or [stages[0]["grade"]["status"]]) + ". Repair the implementation without weakening the supplied tests. "
                           if stage == "repair" else "The initial fixed checks passed. Independently re-check the existing result and recover the task state; do not invent a code change or a new Run merely for this handoff. ") +
                          ("Do not use Harness." if row["arm"] == "plain" else "Use the Harness Skill at " + str(root / "sources" / row["arm"] / "skills/harness-workflow/SKILL.md") + " and configuration at " + str(trial / "client.json") +
                           (". Resume the correct Run if active. A real repair after closure requires an explicitly linked new Run, not reopening the old one."
                            if stage == "repair" else ". Recover and inspect the existing Run, including its closed record if already closed. Do not start another Run merely to re-report a passing result.")))
                seconds = REPAIR_SECONDS
            process = invoke(root, row, stage, prompt, seconds)
            artifacts = output / stage / "artifacts"
            artifacts.mkdir()
            for name in file_set(trial / "workspace"):
                target = artifacts / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(trial / "workspace" / name, target)
            graded = grade(root, task, artifacts, output / stage / "grade.json")
            stage_result = {"stage": stage, "process": process, "grade": {k: v for k, v in graded.items() if k != "sandbox"},
                            "harness": harvest(root, row, output / stage / "harness"),
                            "false_completion": bool(process["claim"] and process["claim"].get("status") == "completed"
                                                     and graded["status"] != "unavailable" and not graded["all_passed"]),
                            "human_interventions": 0}
            write_json(output / stage / "result.json", stage_result)
            stages.append(stage_result)
            print(json.dumps({"finished": row["id"], "stage": stage, "passed": graded["passed"], "total": graded["total"], "seconds": round(process["wall_seconds"], 1)}), flush=True)
        write_json(output / "result.json", {**row, "stages": stages, "total_wall_seconds": sum(x["process"]["wall_seconds"] for x in stages),
                   "model_interventions": len(stages) - 1, "repair_sessions": sum(x["stage"] == "repair" for x in stages),
                   "recovery_sessions": sum(x["stage"] == "recovery" for x in stages), "human_interventions": 0})
        validate_plan(root)


def qualify(root):
    plan = validate_plan(root)
    row = next(row for row in plan["trials"] if row["arm"] == "after")
    trial = root / "trials" / row["id"]
    runtime = root / "sources/after"
    code = ("import json,os,subprocess; from pathlib import Path; "
            "p=subprocess.run(['python3'," + repr(str(runtime / 'skills/harness-workflow/scripts/harness_client.py')) +
            ",'--config'," + repr(str(trial / 'client.json')) + ",'preflight'],capture_output=True,text=True); "
            "v=json.loads(p.stdout); print(json.dumps({'doctor':v,'private_visible':os.access(" + repr(str(root / 'private/corpus.json')) + ",os.R_OK),"
            "'control_writable':os.access(" + repr(str(root / 'plan.json')) + ",os.W_OK),"
            "'workspace_writable':os.access('.',os.W_OK)}))")
    process = subprocess.run([sys.executable, str(root / "scope.py"), str(root), str(trial), str(REPO), "python3", "-c", code], capture_output=True, text=True, timeout=60)
    values = [json.loads(line) for line in process.stdout.splitlines() if line.startswith("{")] if process.returncode == 0 else []
    result = next((value for value in values if "doctor" in value), {"error": process.stderr[-2000:]})
    scope_event = next((value for value in values if value.get("type") == "study.scope.exit"), None)
    passed = (process.returncode == 0 and bool(scope_event) and not scope_event["remaining_namespace_pids"]
              and result.get("doctor", {}).get("command_execution_verified") is True
              and not result["private_visible"] and not result["control_writable"] and result["workspace_writable"])
    result["scope_event"] = scope_event
    write_json(root / "receipts/qualification.json", {"passed": passed, "result": result})
    print(json.dumps({"passed": passed, "result": result}))
    return 0 if passed else 1


def summarize(root):
    plan = validate_plan(root)
    rows = []
    for row in plan["trials"]:
        output = root / "receipts" / row["id"] / "result.json"
        if output.exists():
            result = load(output)
            stages = result["stages"]
            rows.append({"id": row["id"], "host": row["host"], "task": row["task"], "arm": row["arm"],
                         "initial_passed": stages[0]["grade"]["all_passed"], "final_passed": stages[-1]["grade"]["all_passed"],
                         "initial_seconds": stages[0]["process"]["wall_seconds"], "total_seconds": result["total_wall_seconds"],
                         "repairs": result["repair_sessions"], "recovery_sessions": result["recovery_sessions"],
                         "false_completion": any(s["false_completion"] for s in stages),
                         "tool_calls": sum(s["process"]["tool_calls"] for s in stages),
                         "raw_usage": [s["process"]["usage"] for s in stages], "human_interventions": 0,
                         "timed_out": any(s["process"]["timed_out"] for s in stages)})
    report = {"schema_version": "paired-agent-summary-v2", "planned": len(plan["trials"]), "completed": len(rows), "results": rows,
              "all_trials_complete": len(rows) == len(plan["trials"]), "limitations": plan["limits"],
              "cost_semantics": "Timeout rows report consumed time at the limit, not successful completion latency. Missing usage is unknown, never zero."}
    write_json(root / "receipts/summary.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("prepare", "controls", "qualify", "run", "summarize", "status"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--host", choices=tuple(HOSTS))
    args = parser.parse_args()
    root = args.root.absolute()
    if args.operation == "prepare":
        prepare(root)
    elif args.operation in {"controls", "qualify"}:
        raise SystemExit(controls(root) if args.operation == "controls" else qualify(root))
    elif args.operation == "run":
        if args.host is None:
            parser.error("run requires an explicit --host")
        run_host(root, args.host)
    else:
        summarize(root)
