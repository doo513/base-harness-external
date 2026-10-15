#!/usr/bin/env python3
"""Compare real caller processes and read payloads. No model calls or token estimate."""
import argparse
import importlib.util
import json
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from harness_external.service import Harness
from harness_external import context, worker


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def cli(path, config, *arguments):
    started = time.perf_counter()
    result = subprocess.run([sys.executable, str(path), "--config", str(config), *arguments], capture_output=True,
                            encoding="utf-8", timeout=180)
    value = json.loads(result.stdout)
    if result.returncode != 0 or not value.get("ok"):
        raise RuntimeError(result.stdout + result.stderr)
    return value, time.perf_counter() - started, len(result.stdout.encode("utf-8"))


def arguments(value):
    return "json:" + json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before-helper", type=Path, required=True, help="a saved pre-change helper")
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--cases", type=int, default=50)
    parser.add_argument("--startup-only", action="store_true", help="isolate start/binding/compact changes; do not attribute older read optimizations to this change")
    args = parser.parse_args()
    if not (1 <= args.repetitions <= 20 and 3 <= args.cases <= 100):
        parser.error("repetitions must be 1..20, cases 3..100")
    after_path = ROOT / "skills/harness-workflow/scripts/harness_client.py"
    after = module("client_cost_after", after_path)
    report = {"before_helper": str(args.before_helper), "repetitions": args.repetitions, "model_calls": 0,
              "billed_model_tokens": None, "limitations": ["Helper launches are not LLM turns: a Host can already batch shell commands.",
                  "Bytes and local latency do not measure model quality or billed token savings."]}
    with tempfile.TemporaryDirectory(prefix="client-cost-") as temporary:
        root = Path(temporary)
        workspace = root / "workspace"
        workspace.mkdir()
        (workspace / "data").write_text("ready", encoding="utf-8")
        api = Harness(root / "state")
        run = api.start(domain_id="develop", goal="목표와 제약을 유지하며 관측을 확인한다. " * 400,
            workspace=str(workspace), request_id="start", parameters={"profile": "structural", "inputs": ["data"], "artifacts": ["data"],
                "expectations": [{"path": "data", "operator": "equals", "expected": "ready"}]})["run_id"]
        config = root / "client.json"
        settings = after.configure(config, str(ROOT), str(api.store.root), ephemeral=True)["config"]
        client = after.Client(settings)
        startup_samples = []
        for index in range(args.repetitions):
            start_args = {"domain": "develop", "goal": "Preserve the complete task and criteria", "workspace": str(workspace),
                          "mode": "strict", "provenance": {"declared_author": "application"}, "constraints": ["Do not change the criteria"],
                          "parameters": {"profile": "structural", "inputs": ["data"], "artifacts": ["data"],
                                         "expectations": [{"path": "data", "operator": "equals", "expected": "ready"}]}}
            before_start, first_seconds, first_bytes = cli(args.before_helper, config, "call", "--operation", "start",
                "--arguments", arguments(start_args), "--action-id", "before-start-" + str(index), "--output", str(root / f"before-start-{index}.json"))
            _, bind_seconds, bind_bytes = cli(args.before_helper, config, "bind", "--run-id", before_start["run_id"],
                "--workspace", str(workspace), "--domain", "develop", "--output", str(root / f"before-binding-{index}.json"))
            current, seconds, byte_count = cli(after_path, config, "call", "--operation", "start",
                "--arguments", arguments(start_args), "--action-id", "after-start-" + str(index), "--output", str(root / f"after-start-{index}.json"),
                "--binding-output", str(root / f"after-binding-{index}.json"), "--format", "compact")
            if (current["details"]["contract_hash"] != before_start["contract"]["contract_hash"]
                    or current["goal"] != before_start["contract"]["original_goal"] or current["constraints"] != start_args["constraints"]
                    or current["contract_projection"]["checks"] != before_start["contract"]["checks"]):
                raise AssertionError("Startup projection changed intent or criteria")
            binding = after.read_json(str(root / f"after-binding-{index}.json"))
            if client.validate_binding(binding)["run"]["run_id"] != current["run_id"]:
                raise AssertionError("Returned binding differs from Core")
            startup_samples.append((first_seconds + bind_seconds, seconds, first_bytes + bind_bytes, byte_count))
        report["startup_binding"] = {key: {"median_seconds": statistics.median(row[index] for row in startup_samples),
            "median_response_bytes": statistics.median(row[index + 2] for row in startup_samples),
            "helper_cli_launches": 2 if index == 0 else 1}
            for key, index in (("before", 0), ("after", 1))} | {"same_current_core": True, "criteria_equal": True, "binding_matches_core": True}
        if args.startup_only:
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return
        binding_path = root / "binding.json"
        client.bind(run, str(workspace), "develop", binding_path)
        timing, size = {key: [] for key in ("before", "after")}, {key: [] for key in ("before", "after")}
        for index in range(args.repetitions):
            value = {"data": {"note": "동일한 내용의 의도적인 관측 기록"}}
            saved = root / f"before-{index}.json"
            _, elapsed, count = cli(args.before_helper, config, "request", "--binding", str(binding_path), "--operation", "observe",
                                    "--arguments", arguments(value), "--action-id", f"before-{index}", "--output", str(saved))
            _, elapsed2, count2 = cli(args.before_helper, config, "call", "--request", str(saved))
            timing["before"].append(elapsed + elapsed2)
            size["before"].append(count + count2)
            _, elapsed, count = cli(after_path, config, "call", "--binding", str(binding_path), "--operation", "observe",
                "--arguments", arguments(value), "--action-id", f"after-{index}", "--output", str(root / f"after-{index}.json"))
            timing["after"].append(elapsed)
            size["after"].append(count)
        report["request_and_send"] = {key: {"median_seconds": statistics.median(timing[key]),
            "median_response_bytes": statistics.median(size[key]), "helper_cli_launches": 2 if key == "before" else 1,
            "core_mutations": 1} for key in timing}
        for index in range(10):
            api.observe(run, {"note": str(index) + "관측과 다음 판단을 연결한다. " * 60}, f"long-{index}")
        samples, last_pages, packet = [], [], None
        for _ in range(args.repetitions):
            pages, token, elapsed, count = [], None, 0, 0
            while True:
                command = ["context", "--binding", str(binding_path)] + (["--page", token] if token else [])
                page, seconds, byte_count = cli(args.before_helper, config, *command)
                pages.append(page)
                elapsed += seconds
                count += byte_count
                token = page["next_page"]
                if token is None:
                    break
            packet, seconds, byte_count = cli(after_path, config, "context", "--binding", str(binding_path), "--complete")
            expected = {}
            for page in pages:
                expected = context.apply_page(expected, page)
            if after.expand_context(packet) != expected:
                raise AssertionError("Coalescing changed the delivered context")
            samples.append((elapsed, seconds, count, byte_count))
            last_pages = pages
        report["complete_context"] = {key: {"median_seconds": statistics.median(row[index] for row in samples),
            "median_response_bytes": statistics.median(row[index + 2] for row in samples),
            "helper_cli_launches": len(last_pages) if index == 0 else 1, "core_read_requests": len(last_pages)}
            for key, index in (("before", 0), ("after", 1))} | {"exact_restored_data_equal": True}

        source = "import pytest\ndef test_bad(): assert 'a' * 2000 == 'b' * 2000\n@pytest.mark.skip(reason='unavailable')\ndef test_skip(): pass\n"
        source += "\n".join(f"def test_ok_{index}(): assert True" for index in range(args.cases - 2))
        (workspace / "test_cases.py").write_text(source, encoding="utf-8")
        requirements = [{"id": "behavior", "statement": "Observe declared cases", "check_ids": ["domain.tests"], "minimum_evidence": "testcase"}]
        names = ["test_bad", "test_skip", *[f"test_ok_{index}" for index in range(args.cases - 2)]]
        coverage = {"schema_version": "develop-coverage-v2", "profile_id": "cost", "profile_revision": "1", "known_gaps": [],
            "scenarios": [{"id": f"case-{index}", "requirement_id": "behavior", "check_id": "domain.tests", "kind": "positive",
                "required": False, "test_ref": {"framework": "pytest", "path": "test_cases.py", "case_id": "test_cases.py::" + name}}
                for index, name in enumerate(names)]}
        target = api.start(domain_id="develop", goal="Inspect auxiliary framework reports", workspace=str(workspace), request_id="cases", mode="exploratory",
            parameters={"inputs": ["test_cases.py"], "artifacts": ["test_cases.py"],
                "expectations": [{"path": "test_cases.py", "operator": "contains", "expected": "def test"}],
                "execution_checks": [{"kind": "cases", "id": "tests", "adapter_id": "pytest-cases-v1",
                    "selector": {"paths": ["test_cases.py"], "args": []}, "timeout_seconds": 10}],
                "requirements": requirements, "coverage": coverage})["run_id"]
        api.submit(target, "submit")
        original_spawn = worker.spawn_worker
        try:
            worker.spawn_worker = lambda *a: None
            job = api.verify(target, "verify")["job_id"]
            worker.execute_job(api.store.root, job)
        finally:
            worker.spawn_worker = original_spawn
        if api.status(target, job)["job"]["result"]["status"] != "failed":
            raise AssertionError("Actual failing pytest workload did not run")
        case_binding_path = root / "cases-binding.json"
        client.bind(target, str(workspace), "develop", case_binding_path)
        case_binding = after.read_json(str(case_binding_path))
        before_client = module("client_cost_before", args.before_helper).Client(settings)
        samples = []
        for _ in range(args.repetitions):
            request = before_client.make_request("status", {"job_id": job}, binding=case_binding)["request"]
            summary, elapsed, count = cli(args.before_helper, config, "call", "--request", arguments(request))
            records, offset, launches = [], 0, 1
            while True:
                request = before_client.make_request("records", {"kind": "cases", "job_id": job, "offset": offset, "limit": 20,
                    "view": "references"}, binding=case_binding)["request"]
                page, seconds, byte_count = cli(args.before_helper, config, "call", "--request", arguments(request))
                records.extend(page["items"])
                elapsed += seconds
                count += byte_count
                launches += 1
                if page["next_offset"] is None:
                    break
                offset = page["next_offset"]
            attention, seconds, byte_count = cli(after_path, config, "call", "--binding", str(case_binding_path), "--operation", "status",
                "--arguments", arguments({"job_id": job}), "--format", "attention")
            expected = {(item["data"]["case"]["case_id"], item["data"]["case"]["outcome"], item["data"]["observation_id"])
                        for item in records if item["data"]["case"]["outcome"] != "passed"}
            actual = {(item["case_id"], item["status"], item["observation_id"]) for item in attention["attention"] if item["kind"] == "case"}
            if actual != expected:
                raise AssertionError("Attention view lost observed nonpassing cases")
            samples.append((elapsed, seconds, count, byte_count))
        report["case_attention"] = {key: {"median_seconds": statistics.median(row[index] for row in samples),
            "median_response_bytes": statistics.median(row[index + 2] for row in samples), "helper_cli_launches": launches if index == 0 else 1,
            "core_read_requests": launches if index == 0 else 2} for key, index in (("before", 0), ("after", 1))} | {
                "case_count": args.cases, "same_nonpassing_case_ids_outcomes_and_observations": True}
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
