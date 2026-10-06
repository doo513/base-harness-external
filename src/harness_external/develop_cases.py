"""Develop compiles selectors and declared links, never execution commands or facts."""
import copy

from .errors import fields, require, integer

PERSPECTIVES = {
    "stateful-cli": ["positive", "defaults_equivalence", "replay_idempotency", "recovery"],
    "concurrent-queue": ["positive", "boundary", "concurrency", "retry", "recovery"],
    "pure-function": ["positive", "invalid_input", "boundary"],
}


def case_id(value):
    require(isinstance(value, str) and 0 < len(value.encode("utf-8")) <= 4096 and "\x00" not in value and "\n" not in value,
            "CASE_ID", "Use a bounded, opaque case ID")
    return value


def normalize(value, inputs, preparation):
    fields(value, {"kind", "id", "adapter_id", "selector", "required_cases", "allowed_outcomes", "timeout_seconds"},
           {"kind", "id", "adapter_id", "selector"})
    require(value["kind"] == "cases" and isinstance(value["adapter_id"], str) and isinstance(value["selector"], dict),
            "INVALID_PARAMETERS", "A cases check needs an adapter ID and selector object")
    required = value.get("required_cases", [])
    require(isinstance(required, list) and len(required) <= 128 and all(isinstance(p, str) for p in required)
            and len(set(required)) == len(required), "CASE_ID", "Invalid required case list")
    for item in required:
        case_id(item)
    allowed = value.get("allowed_outcomes", ["passed"])
    require(isinstance(allowed, list) and allowed and all(isinstance(p, str) for p in allowed) and len(set(allowed)) == len(allowed)
            and set(allowed) <= {"passed", "skipped", "xfail", "xpass"}, "CASE_OUTCOMES", "Invalid normalized outcomes")
    selected = preparation.selection(value["adapter_id"], value["selector"], inputs, required)
    from .domain import check_identity
    return {"kind": "command", "expectedExitCode": 0,
            "timeout_seconds": integer(value.get("timeout_seconds", 30), 1, 120, "timeout_seconds"),
            "adapter": {"id": value["adapter_id"], **selected,
                        "rules": {"required_case_ids": list(required), "allowed_outcomes": list(allowed), "minimum_selected": 1,
                                  "require_complete_session": True}},
            **check_identity(value, "cases", {"adapter_id": value["adapter_id"], "selector": selected["selector"]})}


def apply_required_scenarios(parameters, coverage):
    result = copy.deepcopy(parameters)
    if not coverage or coverage.get("schema_version") != "develop-coverage-v2":
        return result
    by_id = {"domain." + item["id"]: item for item in result.get("execution_checks", []) if item["kind"] == "cases"}
    for scenario in coverage["scenarios"]:
        if scenario.get("required", False):
            require(scenario["check_id"] in by_id, "COVERAGE_CHECK", "Required scenario must bind a case-aware check")
            check = by_id[scenario["check_id"]]
            case = case_id(scenario["test_ref"]["case_id"])
            check["required_cases"] = list(dict.fromkeys([*check.get("required_cases", []), case]))
    return result


def compile_observation_links(inventory):
    """Translate Develop coverage into the common, framework-opaque link contract.

    The Core need not load Develop or understand its coverage schema, scenario
    kinds, paths, profiles or framework-specific case syntax to join references.
    """
    # Legacy logical test names remain advisory. Check-only links can still
    # report actual Check outcomes without claiming case discovery/execution.
    scenarios = inventory["scenarios"] if inventory.get("schema_version") == "develop-coverage-inventory-v2" else []
    return {"schema_version": "requirement-case-links-v1",
            "requirements": [{key: copy.deepcopy(item[key]) for key in ("requirement_id", "check_ids", "known_gap_ids")}
                             for item in inventory["requirements"]],
            "scenarios": [{"scenario_id": item["id"], "requirement_id": item["requirement_id"],
                           "check_id": item["check_id"], "case_id": item["test_ref"]["case_id"],
                           "required": item.get("required", False), "test_ref": copy.deepcopy(item["test_ref"])}
                          for item in scenarios]}
