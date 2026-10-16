"""Pure Develop planning diagnostics. Declarations never create measurements."""
import copy
import hashlib
import re

from .errors import fields, integer, require


def documents(goal, intent=None):
    originals = [{"id": "goal", "label": "Original goal", "text": goal}, *(intent or {}).get("sources", [])]
    return {item["id"]: {**item, "sha256": hashlib.sha256(item["text"].encode("utf-8")).hexdigest()} for item in originals}


def bind_sources(requirements, goal, intent=None):
    originals = documents(goal, intent)
    for requirement in requirements:
        references = requirement.get("source_refs", [])
        require(isinstance(references, list) and len(references) <= 16, "REQUIREMENT_SOURCE", "At most sixteen source references per requirement")
        for ref in references:
            fields(ref, {"source_id", "start_line", "end_line"}, {"source_id", "start_line", "end_line"})
            key = ref["source_id"]
            require(isinstance(key, str) and key in originals, "REQUIREMENT_SOURCE", "Requirement refers to an unknown original source")
            count = len(originals[key]["text"].splitlines())
            begin = integer(ref["start_line"], 1, max(1, count), "start_line")
            integer(ref["end_line"], begin, count, "end_line")
    return requirements


def review(goal, intent, contract, preparation):
    """Report gaps over explicit IDs/types/observations, not inferred intent."""
    originals = documents(goal, intent)
    requirements = bind_sources(contract.get("requirements", []), goal, intent)
    checks = {item.get("check_id"): item for item in contract.get("checks", []) if item.get("check_id")}
    inventory = contract.get("coverage_inventory", {})
    scenario_requirements = {item["requirement_id"] for item in inventory.get("scenarios", [])}
    ids = {item["id"] for item in requirements}
    explicit_ids = set()
    sources = []
    for key, document in originals.items():
        # Recognize literal numbered requirement labels, not natural-language meaning.
        declared = set(re.findall(r"(?m)^\s*(?:[-*]\s+)?(R[0-9]{1,6})(?=[.\):： \t])", document["text"]))
        explicit_ids.update(declared)
        sources.append({"id": key, "label": document["label"][:80], "label_truncated": len(document["label"]) > 80, "sha256": document["sha256"],
                        "line_count": len(document["text"].splitlines()), "explicit_requirement_id_count": len(declared)})
    diagnostics = []
    def add(code, requirement_id=None, **details):
        diagnostics.append({"code": code, **({"requirement_id": requirement_id} if requirement_id else {}), **details})
    missing = sorted(explicit_ids - ids)
    if missing:
        add("SOURCE_REQUIREMENTS_UNMAPPED", missing_ids=missing[:100], missing_count=len(missing))
    if not requirements:
        add("REQUIREMENTS_NOT_DECLARED")
    for item in requirements:
        key = item["id"]
        linked = [checks[c] for c in item["check_ids"] if c in checks]
        if not item.get("source_refs"):
            add("REQUIREMENT_SOURCE_UNLINKED", key)
        if not linked:
            add("REQUIREMENT_UNMAPPED", key)
            continue
        if item.get("kind", "behavior") != "output" and all(check["kind"] == "file" for check in linked):
            add("BEHAVIOR_HAS_FILE_ONLY_CHECKS", key)
        if key not in scenario_requirements:
            add("SCENARIOS_NOT_DECLARED", key)
        described = []
        for check in linked:
            if check.get("adapter") and callable(getattr(preparation, "observations", None)):
                described.extend(preparation.observations(check["adapter"]))
        if item.get("kind") == "preservation" and not any(
                observed.get("kind") == "file_state" and observed.get("operator") == "unchanged"
                and observed.get("boundary") == "external_capture" for observed in described):
            add("PRESERVATION_NOT_OBSERVED", key)
        if described and all(observed.get("boundary") == "subject_process" for observed in described):
            add("FRAMEWORK_REPORT_ONLY", key)
    return {"schema_version": "develop-verification-plan-v1", "meaning": "declared_scope_review_not_goal_proof",
            "status": "gaps" if diagnostics else "declared", "sources": sources,
            "source_completeness": "caller_supplied_not_authenticated",
            "requirement_count": len(requirements), "scenario_count": len(inventory.get("scenarios", [])),
            "referenced_requirement_count": sum(bool(item.get("source_refs")) for item in requirements),
            "unmapped_source_requirement_count": len(missing), "diagnostic_count": len(diagnostics),
            "diagnostics": copy.deepcopy(diagnostics[:64]), "omitted_diagnostic_count": max(0, len(diagnostics) - 64)}
