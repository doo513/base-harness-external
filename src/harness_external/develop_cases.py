"""Develop owns pytest selectors, declared mappings and limited requirement reports."""
import copy
import json

from .errors import fields, require, relative_path, integer

PERSPECTIVES = {
    "stateful-cli": ["positive", "defaults_equivalence", "replay_idempotency", "recovery"],
    "concurrent-queue": ["positive", "boundary", "concurrency", "retry", "recovery"],
    "pure-function": ["positive", "invalid_input", "boundary"],
}


def node_id(value):
    require(isinstance(value, str) and 0 < len(value.encode()) <= 4096 and "\x00" not in value and "\n" not in value
            and "::" in value, "PYTEST_CASE_ID", "Use an exact pytest node ID")
    relative_path(value.split("::", 1)[0])
    return value


def normalize(value, inputs):
    fields(value, {"id", "paths", "args", "required_cases", "allowed_outcomes", "timeout_seconds"}, {"id", "paths"})
    paths = value["paths"]
    require(isinstance(paths, list) and 1 <= len(paths) <= 32 and all(isinstance(p, str) for p in paths)
            and len(set(paths)) == len(paths), "PYTEST_SCOPE", "Declare unique test files")
    for path in paths:
        relative_path(path)
        require(path in inputs, "PYTEST_SCOPE", "Pytest collection files must be declared inputs")
    require(".harness-cases.jsonl" not in inputs, "PYTEST_SCOPE", "The observer output path is reserved")
    args = value.get("args", [])
    require(isinstance(args, list) and len(args) <= 32 and all(isinstance(v, str) and len(v) <= 2000 for v in args),
            "PYTEST_ARGS", "Invalid pytest arguments")
    pending = False
    for argument in args:
        if pending:
            require(bool(argument) and "\x00" not in argument, "PYTEST_ARGS", "Missing selector value")
            pending = False
        elif argument in {"-k", "-m", "--maxfail"}:
            pending = True
        else:
            require(argument in {"-q", "-v", "-x", "--strict-markers", "--collect-only"}
                    or argument.startswith(("--maxfail=", "-k=", "-m=")), "PYTEST_ARGS", "Unsupported pytest option: " + argument)
    require(not pending, "PYTEST_ARGS", "Missing selector value")
    required = value.get("required_cases", [])
    require(isinstance(required, list) and len(required) <= 128 and all(isinstance(p, str) for p in required)
            and len(set(required)) == len(required), "PYTEST_CASE_ID", "Invalid required case list")
    for item in required:
        node_id(item)
        require(item.split("::", 1)[0] in paths, "PYTEST_SCOPE", "Required case is outside collection scope")
    allowed = value.get("allowed_outcomes", ["passed"])
    require(isinstance(allowed, list) and allowed and all(isinstance(p, str) for p in allowed) and len(set(allowed)) == len(allowed)
            and set(allowed) <= {"passed", "skipped", "xfail", "xpass"}, "PYTEST_OUTCOMES", "Invalid allowed outcomes")
    settings = {"paths": paths, "args": args}
    from .domain import check_identity
    return {"kind": "command", "argv": ["python3", "-I", "/opt/harness-runtime/runner.py", "/opt/harness-runtime/packages.zip",
                                          ".harness-cases.jsonl", json.dumps(settings, ensure_ascii=False, separators=(",", ":"))],
            "cwd": ".", "expectedExitCode": 0, "timeout_seconds": integer(value.get("timeout_seconds", 30), 1, 120, "timeout_seconds"),
            "adapter": {"id": "pytest-cases-v1", "paths": paths, "args": args,
                        "rules": {"required_case_ids": list(required), "allowed_outcomes": list(allowed), "minimum_selected": 1,
                                  "require_complete_session": True}},
            **check_identity(value, "pytest", {"paths": paths, "args": args})}


def apply_required_scenarios(parameters, coverage):
    result = copy.deepcopy(parameters)
    if not coverage or coverage.get("schema_version") != "develop-coverage-v2":
        return result
    by_id = {"domain." + item["id"]: item for item in result.get("pytest_checks", [])}
    for scenario in coverage["scenarios"]:
        if scenario.get("required", False):
            require(scenario["check_id"] in by_id, "COVERAGE_CHECK", "Required pytest scenario must bind a pytest check")
            check = by_id[scenario["check_id"]]
            case = node_id(scenario["test_ref"]["case_id"])
            check["required_cases"] = list(dict.fromkeys([*check.get("required_cases", []), case]))
    return result


def requirement_report(contract, checks, case_observations):
    inventory = contract.get("coverage_inventory") or {}
    if inventory.get("schema_version") != "develop-coverage-inventory-v2":
        return None
    by_check = {item["check_id"]: item for item in checks if item["subject_role"] == "candidate"}
    by_case = {(item["check_id"], item["case"]["case_id"]): item for item in case_observations if item["subject_role"] == "candidate"}
    requirements = []
    for requirement in inventory["requirements"]:
        scenarios = []
        for declared in inventory["scenarios"]:
            if declared["requirement_id"] != requirement["requirement_id"]:
                continue
            found = by_case.get((declared["check_id"], declared["test_ref"]["case_id"]))
            measured = by_check.get(declared["check_id"])
            summary = (measured or {}).get("case_summary") or {}
            status = found["case"]["outcome"] if found else "missing" if summary.get("collection_complete") else "unconfirmed"
            scenarios.append({"scenario_id": declared["id"], "check_id": declared["check_id"], "test_ref": declared["test_ref"],
                              "required": declared.get("required", False), "status": status,
                              "executed": found["case"]["executed"] if found else False if status == "missing" else None,
                              "observation_id": found["observation_id"] if found else None,
                              "check_observation_id": measured.get("observation_id") if measured else None})
        failed_checks = [key for key in requirement["check_ids"] if by_check.get(key, {}).get("comparison_status") == "failed"]
        all_seen = bool(scenarios) and all(s["status"] not in {"missing", "unconfirmed", "not_run", "not_selected", "incomplete"} for s in scenarios)
        all_passed = bool(scenarios) and all(s["executed"] is True and s["status"] == "passed" for s in scenarios)
        linked_checks_passed = bool(requirement["check_ids"]) and all(by_check.get(key, {}).get("comparison_status") == "passed" for key in requirement["check_ids"])
        requirements.append({"requirement_id": requirement["requirement_id"], "scenarios": scenarios,
                             "declared_scenarios_observed": all_seen, "linked_cases_passed": all_passed,
                             "linked_checks_passed": linked_checks_passed,
                             "known_gap_ids": requirement["known_gap_ids"], "failed_check_ids": failed_checks,
                             "status": "no_declared_scenarios" if not scenarios else "linked_checks_failed" if failed_checks else
                                       "linked_checks_passed" if all_passed and linked_checks_passed else
                                       "declared_scenarios_observed" if all_seen else "declared_scenarios_unconfirmed"})
    return {"schema_version": "develop-requirement-observations-v1", "requirements": requirements,
            "meaning": "declared_links_and_observed_execution_not_goal_proof"}
