"""Develop condition declarations. They describe intent, never create evidence."""
import copy
import re
from collections import Counter

from . import develop_cases
from .errors import fields, require

KINDS = {"input", "output", "behavior", "preservation", "conflict", "failure"}


def normalize(values):
    require(isinstance(values, list) and len(values) <= 100,
            "REQUIREMENTS_REQUIRED", "Declare at most 100 requirements")
    result, identifiers = [], set()
    for value in values:
        fields(value, {"id", "statement", "check_ids", "minimum_evidence", "kind", "when"},
               {"id", "statement", "check_ids"})
        key = value["id"]
        require(isinstance(key, str) and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", key))
                and key not in identifiers, "REQUIREMENT_ID", "Requirements need unique logical IDs")
        identifiers.add(key)
        statement = value["statement"]
        require(isinstance(statement, str) and bool(statement.strip()) and len(statement) <= 2000,
                "REQUIREMENT_INVALID", "A bounded requirement statement is required")
        kind = value.get("kind", "behavior")
        require(isinstance(kind, str) and kind in KINDS, "REQUIREMENT_KIND",
                "Requirement kind is input, output, behavior, preservation, conflict or failure")
        if "when" in value:
            require(isinstance(value["when"], str) and bool(value["when"].strip()) and len(value["when"]) <= 2000,
                    "REQUIREMENT_INVALID", "when must describe a bounded applicability condition")
        references = value["check_ids"]
        require(isinstance(references, list) and len(references) <= 128
                and all(isinstance(item, str) and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", item)) for item in references)
                and len(set(references)) == len(references), "REQUIREMENT_REFERENCE", "Invalid or duplicate Check IDs")
        minimum = value.get("minimum_evidence", "file")
        require(isinstance(minimum, str) and minimum in {"file", "command", "testcase"},
                "REQUIREMENT_INVALID", "minimum_evidence is file, command or testcase")
        result.append({**copy.deepcopy(value), "kind": kind, "minimum_evidence": minimum})
    return result


def validate_bindings(requirements, check_index, *, eligible_ids=None, allow_unmapped=True):
    allowed = set(check_index) if eligible_ids is None else set(eligible_ids)
    mapped = set()
    for item in requirements:
        references = item["check_ids"]
        require(set(references) <= allowed and set(references) <= set(check_index),
                "REQUIREMENT_REFERENCE", "Requirements must reference admitted checks with explicit logical IDs")
        minimum = item["minimum_evidence"]
        if references or not allow_unmapped:
            require(minimum not in {"command", "testcase"} or any(check_index[key]["kind"] == "command" for key in references),
                    "REQUIREMENT_EVIDENCE", "A command requirement cannot be represented by file-only checks")
            require(minimum != "testcase" or any(check_index[key].get("adapter") for key in references),
                    "REQUIREMENT_EVIDENCE", "A testcase requirement needs a case-aware check")
        mapped.update(references)
    return mapped


def attach(contract, requirements, inventory, *, source, pending=False):
    """Bind declarative conditions and an advisory summary to the contract hash.

    The source is assigned by the Domain's input channel, not supplied by a
    requirement author. It makes no authenticated approval or completeness claim.
    """
    summary = {"schema_version": "develop-requirements-v1", "source": source,
               "binding_status": "pending_preparation" if pending else "references_validated",
               "requirement_count": len(requirements),
               "counts_by_kind": dict(sorted(Counter(item["kind"] for item in requirements).items())),
               "unlinked_requirement_ids": [item["id"] for item in requirements if not item["check_ids"]],
               "unlisted_scenario_requirement_ids": None if pending else [item["requirement_id"] for item in inventory["requirements"] if not item["scenario_ids"]],
               "known_gap_ids": None if pending else [gap["id"] for gap in inventory["known_gaps"]],
               "meaning": "declared_conditions_and_links_not_goal_proof"}
    contract["requirements"] = copy.deepcopy(requirements)
    contract["requirement_summary"] = summary
    if not pending:
        contract["coverage_inventory"] = copy.deepcopy(inventory)
        contract["observation_links"] = develop_cases.compile_observation_links(inventory)
    return summary
