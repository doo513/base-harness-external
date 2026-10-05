"""Develop compiles selectors and declared links, never execution commands or facts."""
import copy

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
    from .domain import check_identity
    return {"kind": "command", "expectedExitCode": 0,
            "timeout_seconds": integer(value.get("timeout_seconds", 30), 1, 120, "timeout_seconds"),
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


def compile_observation_links(inventory):
    """Translate Develop coverage into the common, framework-opaque link contract.

    The Core need not load Develop or understand its coverage schema, scenario
    kinds, paths, profiles or pytest node ID syntax to join these references.
    """
    if inventory.get("schema_version") != "develop-coverage-inventory-v2":
        return None
    return {"schema_version": "requirement-case-links-v1",
            "requirements": [{key: copy.deepcopy(item[key]) for key in ("requirement_id", "check_ids", "known_gap_ids")}
                             for item in inventory["requirements"]],
            "scenarios": [{"scenario_id": item["id"], "requirement_id": item["requirement_id"],
                           "check_id": item["check_id"], "case_id": item["test_ref"]["case_id"],
                           "required": item.get("required", False), "test_ref": copy.deepcopy(item["test_ref"])}
                          for item in inventory["scenarios"]]}
