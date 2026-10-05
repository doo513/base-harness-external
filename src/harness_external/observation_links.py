"""Core joins explicit references to accepted facts; no Domain/framework semantics.

Requirement/scenario/case IDs are opaque. The Domain compiles these links;
test_ref is display-only metadata, never parsed or used to select an observation.
Reports do not create gates, measurements or claims of goal completeness.
"""
import copy

from harness.common import canonical_bytes, canonical_hash
from .errors import fields, require


def _id(value):
    require(isinstance(value, str) and 0 < len(value.encode("utf-8")) <= 4096,
            "OBSERVATION_LINK_ID", "Link references must be bounded opaque strings")
    return value


def _ids(value):
    require(isinstance(value, list) and len(value) <= 128, "OBSERVATION_LINK_LIMIT", "Too many link references")
    for item in value:
        _id(item)
    require(len(set(value)) == len(value), "OBSERVATION_LINK_ID", "Duplicate link references")
    return set(value)


def validate(links, check_ids):
    if links is None:
        return
    fields(links, {"schema_version", "requirements", "scenarios"}, {"schema_version", "requirements", "scenarios"})
    require(links["schema_version"] == "requirement-case-links-v1", "OBSERVATION_LINK_SCHEMA", "Unknown link schema")
    require(isinstance(links["requirements"], list) and len(links["requirements"]) <= 100
            and isinstance(links["scenarios"], list) and len(links["scenarios"]) <= 128,
            "OBSERVATION_LINK_LIMIT", "Link declarations exceed their bounds")
    requirements = {}
    for item in links["requirements"]:
        fields(item, {"requirement_id", "check_ids", "known_gap_ids"}, {"requirement_id", "check_ids", "known_gap_ids"})
        key = _id(item["requirement_id"])
        require(key not in requirements, "OBSERVATION_LINK_ID", "Duplicate requirement reference")
        references = _ids(item["check_ids"])
        require(references <= set(check_ids), "OBSERVATION_LINK_BINDING", "Link references an undefined Check")
        _ids(item["known_gap_ids"])
        requirements[key] = references
    seen = set()
    for item in links["scenarios"]:
        fields(item, {"scenario_id", "requirement_id", "check_id", "case_id", "required", "test_ref"},
               {"scenario_id", "requirement_id", "check_id", "case_id", "required"})
        key = _id(item["scenario_id"])
        requirement, check = _id(item["requirement_id"]), _id(item["check_id"])
        _id(item["case_id"])
        require(key not in seen, "OBSERVATION_LINK_ID", "Duplicate scenario reference")
        seen.add(key)
        require(requirement in requirements and check in requirements[requirement],
                "OBSERVATION_LINK_BINDING", "Scenario must reference one of its requirement's Checks")
        require(type(item["required"]) is bool, "OBSERVATION_LINK_SCHEMA", "required must be boolean")
        require(isinstance(item.get("test_ref", {}), dict) and len(canonical_bytes(item.get("test_ref", {}))) <= 16384,
                "OBSERVATION_LINK_LIMIT", "Display metadata must be a bounded object")


def report(links, checks, case_observations, job):
    if links is None:
        return None
    pinned_checks = {item["check_id"]: item["ref"] for item in job["checks"]}
    validate(links, pinned_checks)

    def current(item):
        # Checkpoint admission already validates these references. Repeat the
        # join boundary here so a mixed history can never satisfy current links.
        return (item.get("subject_role") == "candidate" and item.get("origin") == "verifier"
                and item.get("subject") == job["candidate"]["subject"]
                and item.get("check_id") in pinned_checks and item.get("check_ref") == pinned_checks[item["check_id"]]
                and all(item.get(key) == job.get(key) for key in (
                    "run_id", "job_id", "candidate_hash", "contract_hash", "check_set_hash", "attempt",
                    "policy_ref", "interpretation_ref", "domain_identity", "acceptance_binding_hash")))

    by_check = {item["check_id"]: item for item in checks if current(item)}
    by_case = {(item["check_id"], item["case"]["case_id"]): item for item in case_observations if current(item)
               and item.get("adapter_identity") == job.get("adapter_bindings", {}).get(item["check_id"])}
    requirements = []
    for requirement in links["requirements"]:
        scenarios = []
        for declared in links["scenarios"]:
            if declared["requirement_id"] != requirement["requirement_id"]:
                continue
            found = by_case.get((declared["check_id"], declared["case_id"]))
            measured = by_check.get(declared["check_id"])
            if found and measured and found.get("execution_observation_id") != measured["observation_id"]:
                found = None
            summary = (measured or {}).get("case_summary") or {}
            status = found["case"]["outcome"] if found else "missing" if summary.get("collection_complete") is True else "unconfirmed"
            scenarios.append({"scenario_id": declared["scenario_id"], "check_id": declared["check_id"],
                              "case_id": declared["case_id"], "test_ref": copy.deepcopy(declared.get("test_ref", {})),
                              "required": declared["required"], "status": status,
                              "selected": found["case"]["selected"] if found else None,
                              "finished": found["case"]["finished"] if found else False,
                              "executed": found["case"]["executed"] if found else False if status == "missing" else None,
                              "observation_id": found["observation_id"] if found else None,
                              "check_observation_id": measured.get("observation_id") if measured else None})
        failed = [key for key in requirement["check_ids"] if by_check.get(key, {}).get("comparison_status") == "failed"]
        seen = bool(scenarios) and all(s["selected"] is True and s["finished"] is True
                and s["status"] not in {"missing", "unconfirmed", "not_run", "not_selected", "incomplete"} for s in scenarios)
        passed = bool(scenarios) and all(s["selected"] is True and s["executed"] is True and s["finished"] is True and s["status"] == "passed" for s in scenarios)
        checks_passed = bool(requirement["check_ids"]) and all(by_check.get(key, {}).get("comparison_status") == "passed" for key in requirement["check_ids"])
        requirements.append({"requirement_id": requirement["requirement_id"], "scenarios": scenarios,
                             "declared_scenarios_observed": seen, "linked_cases_passed": passed, "linked_checks_passed": checks_passed,
                             "known_gap_ids": list(requirement["known_gap_ids"]), "failed_check_ids": failed,
                             "status": "no_declared_scenarios" if not scenarios else "linked_checks_failed" if failed else
                                       "linked_checks_passed" if passed and checks_passed else
                                       "declared_scenarios_observed" if seen else "declared_scenarios_unconfirmed"})
    return {"schema_version": "requirement-observations-v1", "requirements": requirements,
            "links_hash": canonical_hash(links), "meaning": "declared_links_and_observed_execution_not_goal_proof",
            "scope": {key: job.get(key) for key in ("run_id", "job_id", "candidate_hash", "check_set_hash", "contract_hash", "acceptance_binding_hash")}}
