"""Develop semantics behind an explicit Domain interface."""
from __future__ import annotations

import copy
from contextvars import ContextVar
import re
from importlib.resources import files

from harness.json_codec import decode
from harness.common import canonical_bytes, canonical_hash
from .errors import HarnessError, ValidationIssues, require, fields, relative_path, integer, limits, validate_contract
from . import develop_cases, develop_requirements
from .check_preparation import AdapterCheckPreparation, CheckPreparationPort
from .registry import effective_config


COVERAGE_KINDS = {"positive", "negative", "boundary", "state_transition", "concurrency", "recovery", "fault_injection"}
DEVELOP_REVISION = "develop-external-11"
PARAMETERS = {"inputs", "artifacts", "expectations", "execution_checks", "validation_profile", "profile", "requirements", "coverage"}
_COMPILATION = ContextVar("develop_compilation", default=None)


def _memo(kind, value, inputs=(), preparation=None):
    cache = _COMPILATION.get()
    if cache is None:
        return None, None
    try:
        key = (kind, canonical_hash(value), tuple(inputs), id(preparation))
    except (TypeError, ValueError):
        return None, None  # Invalid inputs still go through normal validation.
    return cache, key


def _parameters(parameters, preparation):
    cache, key = _memo("parameters", parameters, preparation=preparation)
    if cache is not None and key in cache:
        return copy.deepcopy(cache[key])
    result = preparation.parameters(parameters)
    if cache is not None:
        cache[key] = copy.deepcopy(result)
        _, normalized_key = _memo("parameters", result, preparation=preparation)
        cache[normalized_key] = copy.deepcopy(result)
    return result


def input_scope(inputs, *, partial=False):
    """Compile membership and detect prefixes by path components, not all pairs."""
    require(len(inputs) <= 128 and len({p.casefold() for p in inputs}) == len(inputs),
            "INVALID_PATH", "Invalid partial input file scope" if partial else "Duplicate/case-colliding input paths")
    ordered = sorted(tuple(path.split("/")) for path in inputs)
    require(not any(current[:len(previous)] == previous for previous, current in zip(ordered, ordered[1:])),
            "INVALID_PATH", "Invalid partial input file scope" if partial else "A file cannot also be a directory")
    return frozenset(inputs)


def normalize_file(expectation, inputs):
    fields(expectation, {"id", "path", "operator", "expected"}, {"path", "operator", "expected"})
    target = relative_path(expectation["path"])
    require(target in inputs, "INVALID_PARAMETERS", "Expectation target must be an input")
    operator, expected = expectation["operator"], expectation["expected"]
    require(isinstance(operator, str) and operator in {"equals", "contains", "sha256"} and isinstance(expected, str) and len(expected) <= 64000,
            "INVALID_PARAMETERS", "Use equals, contains or sha256 with a bounded string")
    require(operator != "contains" or bool(expected), "INVALID_PARAMETERS", "Empty substring checks are vacuous")
    require(operator != "sha256" or bool(re.fullmatch(r"[a-f0-9]{64}", expected)), "INVALID_PARAMETERS", "Expected sha256 is invalid")
    cache, key = _memo("file", expectation, sorted(inputs))
    if cache is not None and key in cache:
        return copy.deepcopy(cache[key])
    result = {"kind": "file", **{k: v for k, v in copy.deepcopy(expectation).items() if k != "id"},
              **check_identity(expectation, "file", {"path": target, "operator": operator})}
    if cache is not None:
        cache[key] = copy.deepcopy(result)
    return result


def check_identity(value, kind, semantic_key):
    if "id" in value:
        require(isinstance(value["id"], str) and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", value["id"])),
                "INVALID_CHECK_ID", "Check id must be a bounded logical name")
        return {"check_key": "named:" + value["id"], "check_id": "domain." + value["id"]}
    return {"check_key": kind + ":" + canonical_hash(semantic_key)}


def normalize_command(command):
    fields(command, {"id", "argv", "cwd", "timeout_seconds"}, {"argv"})
    argv = command["argv"]
    require(isinstance(argv, list) and 1 <= len(argv) <= 64 and all(
        isinstance(a, str) and len(a) <= 8192 and "\x00" not in a for a in argv) and bool(argv[0].strip()),
        "INVALID_PARAMETERS", "Test command must be a bounded argv array")
    require(len(canonical_bytes(argv)) <= 64000, "INVALID_PARAMETERS", "Test argv exceeds 64 KiB")
    cwd = relative_path(command.get("cwd", "."), directory=True)
    return {"kind": "command", "argv": copy.deepcopy(argv), "cwd": cwd,
            "timeout_seconds": integer(command.get("timeout_seconds", 30), 1, 120, "timeout_seconds"), "expectedExitCode": 0,
            **check_identity(command, "command", {"argv": argv, "cwd": cwd})}


def compile_coverage_inventory(coverage, requirements, eligible_check_ids, check_index, reference_paths, preparation):
    """Validate and summarize a declared Develop test inventory.

    This records where checks are intended to be exercised. It does not inspect
    test code, count executed test cases, or assert that a test proves its label.
    """
    requirement_by_id = {item["id"]: item for item in requirements}
    requirement_checks = {key: frozenset(item["check_ids"]) for key, item in requirement_by_id.items()}
    entries = {key: {"scenario_ids": [], "known_gap_ids": []} for key in requirement_by_id}
    scenarios, gaps, entry_ids = [], [], set()
    profile_id = profile_revision = None
    status = "not_provided"
    if coverage is not None:
        coverage = fields(coverage, {"schema_version", "profile_id", "profile_revision", "scenarios", "known_gaps"},
                          {"profile_id", "profile_revision", "scenarios", "known_gaps"})
        version2 = coverage.get("schema_version") == "develop-coverage-v2"
        require(coverage.get("schema_version") in (None, "develop-coverage-v1", "develop-coverage-v2"), "COVERAGE_SCHEMA", "Unknown coverage version")
        profile_id = coverage["profile_id"]
        profile_revision = coverage["profile_revision"]
        require(isinstance(profile_id, str) and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", profile_id)),
                "COVERAGE_PROFILE_INVALID", "Coverage profile needs a stable logical ID")
        require(isinstance(profile_revision, str) and 0 < len(profile_revision) <= 128,
                "COVERAGE_PROFILE_INVALID", "Coverage profile needs a bounded revision")
        source_scenarios, source_gaps = coverage["scenarios"], coverage["known_gaps"]
        require(isinstance(source_scenarios, list) and len(source_scenarios) <= 128,
                "COVERAGE_SCENARIOS_INVALID", "A coverage profile may declare at most 128 scenarios")
        require(isinstance(source_gaps, list) and len(source_gaps) <= 128,
                "COVERAGE_GAPS_INVALID", "A coverage profile may declare at most 128 known gaps")
        status = "declared"
        for item in source_scenarios:
            fields(item, {"id", "requirement_id", "check_id", "kind", "test_ref"} | ({"required"} if version2 else set()),
                   {"id", "requirement_id", "check_id", "kind", "test_ref"})
            scenario_id = item["id"]
            require(isinstance(scenario_id, str) and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", scenario_id))
                    and scenario_id not in entry_ids, "COVERAGE_SCENARIO_ID", "Coverage scenario IDs must be unique and stable")
            entry_ids.add(scenario_id)
            requirement_id, check_id, kind = item["requirement_id"], item["check_id"], item["kind"]
            require(isinstance(requirement_id, str) and requirement_id in requirement_by_id,
                    "COVERAGE_REQUIREMENT", "Coverage scenario references an unknown requirement")
            require(isinstance(check_id, str) and check_id in eligible_check_ids
                    and check_id in requirement_checks[requirement_id] and check_id in check_index,
                    "COVERAGE_CHECK", "Coverage scenario check must be admitted and mapped to its requirement")
            require(isinstance(kind, str) and kind in COVERAGE_KINDS,
                    "COVERAGE_KIND", "Unknown Develop coverage scenario kind")
            reference = fields(item["test_ref"], {"path", "case_id"} | ({"framework"} if version2 else set()), {"path", "case_id"})
            path = relative_path(reference["path"])
            case_id = reference["case_id"]
            require(path in reference_paths, "COVERAGE_TEST_REFERENCE", "Coverage test reference is outside the declared verification source scope")
            if version2:
                develop_cases.case_id(case_id)
                adapter = check_index[check_id].get("adapter", {})
                require(bool(adapter) and path in adapter["source_paths"], "COVERAGE_TEST_REFERENCE", "Case reference must match its declared source/check")
                preparation.reference(adapter, reference)
                require(type(item.get("required", False)) is bool, "COVERAGE_TEST_REFERENCE", "required must be boolean")
            else:
                require(isinstance(case_id, str) and bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.:-]{0,199}", case_id)),
                        "COVERAGE_TEST_REFERENCE", "Coverage test reference needs a bounded logical case ID")
            scenarios.append({"id": scenario_id, "requirement_id": requirement_id, "check_id": check_id,
                              "kind": kind, "test_ref": {"path": path, "case_id": case_id}})
            if version2:
                scenarios[-1].update(required=item.get("required", False))
                if "framework" in reference:
                    require(isinstance(reference["framework"], str) and 0 < len(reference["framework"]) <= 128,
                            "COVERAGE_TEST_REFERENCE", "Framework metadata must be bounded text")
                    scenarios[-1]["test_ref"]["framework"] = reference["framework"]
            entries[requirement_id]["scenario_ids"].append(scenario_id)
        for item in source_gaps:
            fields(item, {"id", "requirement_id", "reason"}, {"id", "requirement_id", "reason"})
            gap_id, requirement_id, reason = item["id"], item["requirement_id"], item["reason"]
            require(isinstance(gap_id, str) and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", gap_id))
                    and gap_id not in entry_ids, "COVERAGE_GAP_ID", "Coverage scenario and gap IDs must be unique")
            entry_ids.add(gap_id)
            require(isinstance(requirement_id, str) and requirement_id in requirement_by_id,
                    "COVERAGE_REQUIREMENT", "Known gap references an unknown requirement")
            require(isinstance(reason, str) and 0 < len(reason) <= 2000,
                    "COVERAGE_GAP_INVALID", "Known gap requires a bounded explanation")
            gaps.append({"id": gap_id, "requirement_id": requirement_id, "reason": reason})
            entries[requirement_id]["known_gap_ids"].append(gap_id)
    requirement_inventory = []
    unlisted = []
    for requirement in requirements:
        declared = entries[requirement["id"]]
        if declared["scenario_ids"] and declared["known_gap_ids"]:
            inventory_status = "scenarios_and_known_gaps_declared"
        elif declared["scenario_ids"]:
            inventory_status = "scenarios_declared"
        elif declared["known_gap_ids"]:
            inventory_status = "known_gaps_declared"
        else:
            inventory_status = "not_listed"
            unlisted.append(requirement["id"])
        requirement_inventory.append({"requirement_id": requirement["id"],
                                     "check_ids": copy.deepcopy(requirement["check_ids"]),
                                     **copy.deepcopy(declared), "inventory_status": inventory_status})
    assurance = "declared_inventory_not_semantically_verified"
    report = {"schema_version": "develop-coverage-inventory-v2" if coverage and coverage.get("schema_version") == "develop-coverage-v2" else "develop-coverage-inventory-v1", "status": status,
              "profile": {"id": profile_id, "revision": profile_revision} if coverage is not None else None,
              "scenarios": scenarios, "known_gaps": gaps, "requirements": requirement_inventory,
              "unlisted_requirement_ids": unlisted, "assurance": assurance,
              "limitations": ["Test references are checked against declared source paths; test names and assertions are not inspected.",
                              "The verifier measures configured checks as a whole; this inventory does not create per-scenario observations.",
                              "Declared scenarios and known gaps are not proof of test validity, independence or goal completeness."]}
    if coverage is not None:
        report["source_hash"] = canonical_hash(coverage)
        if coverage.get("schema_version") == "develop-coverage-v2":
            report["limitations"][0] = "Exact case IDs are declared here; discovered/selected/executed cases are measured only during verification."
            report["limitations"][1] = "Explicit required scenarios become Check conditions; completion gates follow the selected policy."
    summary = {"schema_version": report["schema_version"], "status": status, "profile": report["profile"],
               "scenario_count": len(scenarios), "known_gap_count": len(gaps),
               "requirements": requirement_inventory, "unlisted_requirement_ids": unlisted,
               "assurance": assurance}
    if coverage is not None:
        summary["source_hash"] = report["source_hash"]
        report["summary_hash"] = canonical_hash(summary)
    return report, summary


def normalize_execution(value, inputs, preparation):
    require(isinstance(value, dict), "INVALID_PARAMETERS", "Execution check must be an object")
    cache, key = _memo("execution", value, inputs, preparation)
    if cache is not None and key in cache:
        return copy.deepcopy(cache[key])
    if value.get("kind") == "command":
        result = normalize_command({key: item for key, item in value.items() if key != "kind"})
    else:
        require(value.get("kind") == "cases", "INVALID_PARAMETERS", "Execution check kind must be command or cases")
        result = develop_cases.normalize(value, inputs, preparation)
    if cache is not None:
        cache[key] = copy.deepcopy(result)
    return result


def build_contract(domain_id: str, goal: str, parameters: dict, verifier: dict, *, check_preparation=None) -> dict:
    preparation = check_preparation if check_preparation is not None else AdapterCheckPreparation()
    parameters = _parameters(parameters, preparation)
    require(domain_id == "develop", "DOMAIN_UNSUPPORTED", "Only develop is supported")
    # Packaged extraction of the existing Develop manifest, not a second model
    # policy loader. Execution permissions from that manifest are NOT granted.
    source_domain = decode(files("harness_external").joinpath("develop_manifest.json").read_text(encoding="utf-8"))
    require(source_domain["id"] == domain_id and {"artifact", "command_exit", "behavior"} <= set(source_domain["verification"]["criterionTemplates"]),
            "DOMAIN_POLICY_MISMATCH", "Unsupported source domain criteria")
    require(isinstance(goal, str) and 0 < len(goal.strip()) <= 16000, "GOAL_REQUIRED", "Provide the original task goal")
    basic = ValidationIssues()
    basic.capture("", lambda: fields(parameters, PARAMETERS, {"inputs", "artifacts", "expectations"}))
    for key, maximum, code, message in (
        ("inputs", 128, "INPUTS_REQUIRED", "List 1..128 input files, including tests/fixtures"),
        ("artifacts", 128, "ARTIFACTS_REQUIRED", "Declare at least one artifact from the input scope"),
        ("expectations", 128, "NEEDS_INPUT", "Concrete file expectations are required; goal text alone is not a test"),
    ):
        if key in parameters:
            basic.check("/" + key, isinstance(parameters[key], list) and 1 <= len(parameters[key]) <= maximum, code, message)
    declared_execution = parameters.get("execution_checks", [])
    if basic.check("/execution_checks", isinstance(declared_execution, list) and len(declared_execution) <= 16,
                   "INVALID_PARAMETERS", "At most sixteen execution checks are supported"):
        basic.check("/execution_checks", bool(declared_execution) if parameters.get("profile", "execution") == "execution" else not declared_execution,
                    "NEEDS_INPUT", "Execution requires execution_checks; structural explicitly excludes execution")
    basic.raise_if_any()
    inputs = parameters["inputs"]
    artifacts = parameters["artifacts"]
    require(isinstance(inputs, list) and 1 <= len(inputs) <= 128, "INPUTS_REQUIRED", "List 1..128 input files, including tests/fixtures")
    inputs = [relative_path(item) for item in inputs]
    admitted_inputs = input_scope(inputs)
    require(isinstance(artifacts, list) and artifacts and all(isinstance(p, str) and p in admitted_inputs for p in artifacts),
            "ARTIFACTS_REQUIRED", "Artifacts must be included in inputs")
    require(len(set(artifacts)) == len(artifacts), "INVALID_PARAMETERS", "Duplicate artifacts")
    profile = parameters.get("profile", "execution")
    require(isinstance(profile, str) and profile in {"structural", "execution"}, "INVALID_PARAMETERS", "Profile is structural or execution")
    expectations = parameters["expectations"]
    require(isinstance(expectations, list) and 1 <= len(expectations) <= 128, "NEEDS_INPUT", "Concrete file expectations are required; goal text alone is not a test")
    checks = []
    covered = set()
    issues = ValidationIssues()
    for index, expectation in enumerate(expectations):
        check = issues.capture("/expectations/" + str(index), lambda: normalize_file(expectation, admitted_inputs))
        if check is not None:
            covered.add(check["path"])
            checks.append(check)
    if not issues.count:
        issues.check("/artifacts", set(artifacts) <= covered, "NEEDS_INPUT", "Every artifact needs at least one explicit expectation")
    execution = parameters.get("execution_checks", [])
    valid_execution = issues.check("/execution_checks", isinstance(execution, list) and len(execution) <= 16,
                                   "INVALID_PARAMETERS", "At most sixteen execution checks are supported")
    if valid_execution:
        issues.check("/execution_checks", (profile == "execution" and bool(execution)) or (profile == "structural" and not execution),
                     "NEEDS_INPUT", "Execution requires execution_checks; structural explicitly excludes execution")
        for index, item in enumerate(execution):
            check = issues.capture("/execution_checks/" + str(index), lambda: normalize_execution(item, inputs, preparation))
            if check is not None:
                checks.append(check)
    issues.raise_if_any()
    require(len({c["check_key"] for c in checks}) == len(checks), "CHECK_ID_AMBIGUOUS",
            "Checks with the same target/operator or command need distinct explicit id values")
    body = {
        "schema_version": "external-goal-contract-v1", "domain_id": "develop", "domain_revision": DEVELOP_REVISION,
        "source_domain": {"id": source_domain["id"], "revision": source_domain["revision"], "manifest_hash": canonical_hash(source_domain)},
        "original_goal": goal, "profile": profile, "inputs": sorted(inputs), "artifacts": sorted(artifacts), "checks": checks,
        "verifier": verifier,
        "rules": {"all_checks_required": True, "snapshot_required": True, "command_execution": "strict-sandbox-only",
                  "caller_observations_are_evidence": False, "ready_attestation": False},
        "limitations": ["Caller-supplied expectations are pinned, not independently proven to cover the original goal.",
                        "Only the explicitly listed input files are snapshotted; this is not whole-project certification.",
                        "Structural profile does not establish runtime behavior.",
                        "Local state and verifier run as the same user; reports are unsigned and not tamper-proof."],
    }
    if "requirements" in parameters or "coverage" in parameters:
        requirements = develop_requirements.normalize(parameters.get("requirements", []))
        coverage = parameters.get("coverage")
        require(bool(requirements) or coverage is None, "REQUIREMENTS_REQUIRED", "Coverage needs declared requirements")
        indexed = {item["check_id"]: item for item in checks if "check_id" in item}
        develop_requirements.validate_bindings(requirements, indexed)
        inventory, _ = compile_coverage_inventory(coverage, requirements, set(indexed), indexed, set(inputs), preparation)
        if coverage is not None and coverage.get("schema_version") == "develop-coverage-v2":
            promoted = develop_cases.apply_required_scenarios(parameters, coverage)
            if promoted.get("execution_checks", []) != parameters.get("execution_checks", []):
                body["checks"] = [item for item in checks if item["kind"] == "file"] + [
                    normalize_execution(item, inputs, preparation) for item in promoted.get("execution_checks", [])]
        develop_requirements.attach(body, requirements, inventory, source="caller_proposal")
    return {**body, "contract_hash": canonical_hash(body)}


class DevelopModule:
    identity_files = tuple(str(files("harness_external").joinpath(name)) for name in
                           ("develop_manifest.json", "develop_cases.py", "develop_requirements.py", "develop_analysis.py", "develop_need_compat.py"))
    identity_file_ids = ("develop-manifest", "develop-case-semantics", "develop-requirements", "develop-analysis", "develop-need-compat")
    domain_id = "develop"
    revision = DEVELOP_REVISION

    def __init__(self, *, check_preparation: CheckPreparationPort | None = None):
        self._check_preparation = check_preparation if check_preparation is not None else AdapterCheckPreparation()

    @property
    def _checks(self):
        # Existing structural/command-only subclasses need not call a new base
        # constructor. Their default bridge is stateless and has no registry.
        selected = getattr(self, "_check_preparation", None)
        return selected if selected is not None else AdapterCheckPreparation()

    def identity_config(self):
        # Adapter code/config is pinned separately when its Check is admitted.
        # Retain inherited/subclass Domain policy configuration in this identity.
        require(callable(getattr(self._checks, "identity", None)), "DOMAIN_IDENTITY_UNAVAILABLE", "Preparation port needs a stable identity")
        return {"effective": effective_config(self, exclude={"_check_preparation"}), "identity_file_ids": list(self.identity_file_ids),
                "check_preparation": self._checks.identity()}

    def prepare_acceptance(self, goal, parameters, verifier, *, policy, intent=None, analysis_selection=None):
        token = _COMPILATION.set({})
        try:
            result = self._compile_acceptance(goal, parameters, verifier, policy=policy, intent=intent)
            if analysis_selection is not None:
                result["analysis_guidance"] = self.select_analysis_guidance(result["analysis_guidance"], analysis_selection)
            return result
        finally:
            _COMPILATION.reset(token)

    def _compile_acceptance(self, goal, parameters, verifier, *, policy, intent=None):
        """Compile configured acceptance plus revisable caller exploration.

        Requirements and evidence kinds are Develop semantics. Core receives
        normalized checks/IDs and pins them without deciding what the goal means.
        """
        base = _parameters(policy["parameters"], self._checks)
        parameters = _parameters(parameters, self._checks)
        require(not {"requirements", "coverage"}.intersection(base), "ACCEPTANCE_DECLARATION_SOURCE",
                "Configured requirements and coverage belong at the policy top level")
        require(not {"requirements", "coverage"}.intersection(parameters), "ACCEPTANCE_DECLARATION_PINNED",
                "Configured requirements and coverage cannot be replaced by caller parameters; revise assumptions or add exploratory checks")
        accepted = self.prepare(goal, base, verifier, exploratory=False, intent=intent)
        base["profile"] = accepted["contract"]["profile"]
        checks = accepted["contract"]["checks"]
        required = policy["required_check_ids"]
        require(isinstance(required, list) and 1 <= len(required) <= 128
                and all(isinstance(key, str) for key in required) and len(set(required)) == len(required),
                "ACCEPTANCE_CHECKS_REQUIRED", "Policy requires a nonempty set of named acceptance check IDs")
        indexed = {c.get("check_id"): c for c in checks}
        require(set(required) <= indexed.keys(), "ACCEPTANCE_CHECKS_REQUIRED", "Acceptance checks require explicit Domain check IDs")
        bundle_paths = set(policy["bundle"]["files"])
        reference_paths = set(bundle_paths)
        for key in required:
            if indexed[key].get("adapter"):
                adapter = indexed[key]["adapter"]
                if adapter.get("criteria_source", "files") == "selector":
                    # Predicates are already in the pinned policy definition;
                    # these paths describe the observed subject, not test code.
                    reference_paths.update(adapter["source_paths"])
                else:
                    require(set(adapter["source_paths"]) <= bundle_paths, "ACCEPTANCE_SCOPE", "Case-aware acceptance test files must belong to the pinned bundle")
        require(bundle_paths <= set(base["inputs"]) and not bundle_paths.intersection(base["artifacts"]),
                "ACCEPTANCE_SCOPE", "Test bundle files must be inputs, not implementation artifacts")
        require(not any(indexed[key]["kind"] == "command" and indexed[key].get("adapter", {}).get("criteria_source") != "selector"
                        for key in required) or bool(bundle_paths),
                "ACCEPTANCE_BUNDLE_REQUIRED", "Command acceptance requires an explicitly pinned test/fixture bundle")
        requirements = develop_requirements.normalize(policy["requirements"])
        require(bool(requirements),
                "REQUIREMENTS_REQUIRED", "Acceptance requires a bounded requirement-to-check mapping")
        mapped = develop_requirements.validate_bindings(requirements, indexed, eligible_ids=required, allow_unmapped=False)
        require(mapped == set(required), "REQUIREMENT_REFERENCE", "Every mandatory check must map to a requirement")
        coverage_inventory, coverage_summary = compile_coverage_inventory(
            policy.get("coverage"), requirements, set(required), indexed, reference_paths, self._checks)
        promoted = develop_cases.apply_required_scenarios(base, policy.get("coverage"))
        if promoted.get("execution_checks", []) != base.get("execution_checks", []):
            checks = [item for item in checks if item["kind"] == "file"] + [
                normalize_execution(item, base["inputs"], self._checks) for item in promoted.get("execution_checks", [])]
        base = promoted
        checks_by_key = {check["check_key"]: check for check in checks}
        fields(parameters, PARAMETERS)
        require(not (base["profile"] == "execution" and parameters.get("profile") == "structural"),
                "ACCEPTANCE_SCOPE", "Configured command acceptance cannot be downgraded to structural checks")
        merged = copy.deepcopy(base)
        for key in ("inputs", "artifacts"):
            extra = parameters.get(key, [])
            require(isinstance(extra, list), "INVALID_PARAMETERS", key + " must be a list")
            for path in extra:
                relative_path(path)
            merged[key] = list(dict.fromkeys(base[key] + extra))
        for key in ("expectations", "execution_checks"):
            extra = parameters.get(key, [])
            require(isinstance(extra, list), "INVALID_PARAMETERS", key + " must be a list")
            originals = list(base.get(key, []))
            admitted_inputs = frozenset(merged["inputs"])
            for proposal in extra:
                normalized = normalize_file(proposal, admitted_inputs) if key == "expectations" else normalize_execution(proposal, merged["inputs"], self._checks)
                old = checks_by_key.get(normalized["check_key"])
                require(old is None or old == normalized, "ACCEPTANCE_CHECK_CHANGED", "Configured checks cannot be replaced by caller proposals")
                if old is None:
                    originals.append(proposal)
            merged[key] = originals
        if parameters.get("profile") == "execution":
            merged["profile"] = "execution"
        if "validation_profile" in parameters:
            merged["validation_profile"] = parameters["validation_profile"]
        result = self.prepare(goal, merged, verifier, exploratory=True, intent=intent)
        result["acceptance_check_ids"] = list(required)
        result["coverage_inventory_summary"] = coverage_summary
        result["requirements_summary"] = develop_requirements.attach(result["contract"], requirements, coverage_inventory,
                                                                     source="operator_configuration")
        result["contract"]["acceptance_requirements"] = copy.deepcopy(policy["requirements"])
        result["contract"]["limitations"].append("Requirement and scenario links express configured inventory, not proof that tests adequately represent the goal.")
        result["contract"]["contract_hash"] = canonical_hash({k: v for k, v in result["contract"].items() if k != "contract_hash"})
        return result

    def prepare(self, goal, parameters, verifier, *, exploratory=False, intent=None, analysis_selection=None):
        from . import develop_analysis
        advice = develop_analysis.guidance()
        if analysis_selection is not None:
            advice = self.select_analysis_guidance(advice, analysis_selection)
        if intent is not None:
            require(intent["original_goal"] == goal and intent["domain_id"] == self.domain_id, "DOMAIN_INTENT_BINDING", "Domain preparation received the wrong intent")
        require(isinstance(goal, str) and 0 < len(goal.strip()) <= 16000, "GOAL_REQUIRED", "Provide the original task goal")
        parameters = _parameters(parameters, self._checks)
        issues = ValidationIssues()
        issues.capture("", lambda: fields(parameters, PARAMETERS))
        requirements = issues.capture("/requirements", lambda: develop_requirements.normalize(parameters.get("requirements", [])))
        issues.check("/coverage", parameters.get("coverage") is None or isinstance(parameters["coverage"], dict),
                     "COVERAGE_SCHEMA", "Coverage must be a declaration object or null")
        if requirements is not None:
            issues.check("/requirements", bool(requirements) or parameters.get("coverage") is None,
                         "REQUIREMENTS_REQUIRED", "Coverage needs declared requirements")
        perspective = parameters.get("validation_profile")
        issues.check("/validation_profile", perspective is None or isinstance(perspective, str) and perspective in develop_cases.PERSPECTIVES,
                     "VALIDATION_PROFILE", "Unknown Develop perspective profile")
        profile = parameters.get("profile", "execution")
        issues.check("/profile", isinstance(profile, str) and profile in {"structural", "execution"}, "INVALID_PARAMETERS", "Unknown Develop profile")
        for key in ("inputs", "artifacts", "expectations", "execution_checks"):
            if key in parameters:
                issues.check("/" + key, isinstance(parameters[key], list), "INVALID_PARAMETERS", "Develop parameter must be a list: " + key)
        issues.raise_if_any()
        for item in parameters.get("inputs", []) + parameters.get("artifacts", []):
            relative_path(item)
        inputs = parameters.get("inputs", [])
        input_scope(inputs, partial=True)
        missing = [key for key in ("inputs", "artifacts", "expectations") if not parameters.get(key)]
        if profile == "execution" and not parameters.get("execution_checks"):
            missing.append("execution_checks")
        if not exploratory or not missing:
            contract = build_contract(self.domain_id, goal, parameters, verifier, check_preparation=self._checks)
            result = {"status": "proceed", "questions": [], "contract": contract, "available_operations": ["submit", "verify", "finish_completed"],
                      "analysis_guidance": advice}
            if "requirement_summary" in contract:
                result["requirements_summary"] = copy.deepcopy(contract["requirement_summary"])
            if perspective:
                result["validation_perspectives"] = {"profile_id": perspective, "revision": "1", "perspectives": develop_cases.PERSPECTIVES[perspective], "meaning": "advisory_not_mandatory_tests"}
            return result
        # Missing domain data is a durable clarification state, never invented criteria.
        questions = [{"id": "develop:" + key, "parameter": key, "reason": "missing_domain_parameter"} for key in missing]
        source = decode(files("harness_external").joinpath("develop_manifest.json").read_text(encoding="utf-8"))
        contract = {"schema_version": "external-goal-contract-v1", "domain_id": self.domain_id, "domain_revision": self.revision,
                    "source_domain": {"id": source["id"], "revision": source["revision"], "manifest_hash": canonical_hash(source)},
                    "original_goal": goal, "profile": profile, "inputs": parameters.get("inputs", []),
                    "artifacts": parameters.get("artifacts", []), "checks": [], "verifier": verifier,
                    "rules": {"all_checks_required": False, "snapshot_required": True, "command_execution": "strict-sandbox-only",
                              "caller_observations_are_evidence": False, "ready_attestation": False},
                    "limitations": ["Local unsigned record; same-user tampering is outside this assurance.",
                                    "Goal interpretation and caller-authored tests do not prove goal coverage or independence."]}
        result = {"status": "needs_input", "questions": questions, "contract": contract, "analysis_guidance": advice,
                  "available_operations": ["submit", "verify"] if inputs else []}
        if "requirements" in parameters or "coverage" in parameters:
            result["requirements_summary"] = develop_requirements.attach(contract, requirements, None,
                                                                         source="caller_proposal", pending=True)
        contract["contract_hash"] = canonical_hash(contract)
        return result

    def select_analysis_guidance(self, guidance, selection):
        """Pure optional view capability; no contract, Run or execution input."""
        from .develop_analysis import select_guidance
        fields(selection, {"perspectives", "knowledge"})
        try:
            return select_guidance(guidance, **selection)
        except ValueError as error:
            raise HarnessError("ANALYSIS_SELECTION_INVALID", str(error)) from error

    def normalize_need(self, details, contract):
        from .develop_need_compat import normalize
        return normalize(details, contract)

    def normalize_check(self, parameters, contract):
        fields(parameters, {"kind", "path", "operator", "expected", "argv", "cwd", "timeout_seconds", "expectedExitCode", "adapter"}, {"kind"})
        if parameters["kind"] == "file":
            fields(parameters, {"kind", "path", "operator", "expected"}, {"kind", "path", "operator", "expected"})
            return normalize_file({key: value for key, value in parameters.items() if key != "kind"}, contract["inputs"])
        require(parameters["kind"] == "command" and contract["profile"] == "execution", "CHECK_UNSUPPORTED", "Command checks require the execution profile")
        require(bool(contract["inputs"]), "NEEDS_INPUT", "Declare input scope before command measurements")
        require(type(parameters.get("expectedExitCode", 0)) is int and parameters.get("expectedExitCode", 0) == 0,
                "CHECK_UNSUPPORTED", "Develop command checks expect exit zero")
        if "adapter" in parameters:
            fields(parameters, {"kind", "expectedExitCode", "timeout_seconds", "adapter"}, {"kind", "adapter"})
            adapter = fields(parameters["adapter"], {"id", "selector", "source_paths", "rules", "criteria_source"}, {"id", "selector", "source_paths", "rules"})
            rules = fields(adapter["rules"], {"required_case_ids", "allowed_outcomes", "minimum_selected", "require_complete_session"},
                           {"required_case_ids", "allowed_outcomes", "minimum_selected", "require_complete_session"})
            result = develop_cases.normalize({"kind": "cases", "id": "normalized", "adapter_id": adapter["id"], "selector": adapter["selector"],
                         "required_cases": rules["required_case_ids"], "allowed_outcomes": rules["allowed_outcomes"],
                         "timeout_seconds": parameters.get("timeout_seconds", 30)}, contract["inputs"], self._checks)
            require(result["adapter"] == adapter, "CHECK_SCOPE_CONFLICT", "Case-aware conditions changed")
            return result
        return normalize_command({key: value for key, value in parameters.items() if key not in {"kind", "expectedExitCode"}})
