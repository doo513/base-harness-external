"""Develop semantics behind an explicit Domain interface."""
from __future__ import annotations

import copy
import re
from importlib.resources import files

from harness.json_codec import decode
from harness.common import canonical_bytes, canonical_hash
from .errors import HarnessError, require, fields, relative_path, integer, limits, validate_contract
from . import develop_cases


COVERAGE_KINDS = {"positive", "negative", "boundary", "state_transition", "concurrency", "recovery", "fault_injection"}


def normalize_file(expectation, inputs):
    fields(expectation, {"id", "path", "operator", "expected"}, {"path", "operator", "expected"})
    target = relative_path(expectation["path"])
    require(target in inputs, "INVALID_PARAMETERS", "Expectation target must be an input")
    operator, expected = expectation["operator"], expectation["expected"]
    require(isinstance(operator, str) and operator in {"equals", "contains", "sha256"} and isinstance(expected, str) and len(expected) <= 64000,
            "INVALID_PARAMETERS", "Use equals, contains or sha256 with a bounded string")
    require(operator != "contains" or bool(expected), "INVALID_PARAMETERS", "Empty substring checks are vacuous")
    require(operator != "sha256" or bool(re.fullmatch(r"[a-f0-9]{64}", expected)), "INVALID_PARAMETERS", "Expected sha256 is invalid")
    return {"kind": "file", **{k: v for k, v in copy.deepcopy(expectation).items() if k != "id"},
            **check_identity(expectation, "file", {"path": target, "operator": operator})}


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


def compile_coverage_inventory(coverage, requirements, mandatory_check_ids, check_index, bundle_paths):
    """Validate and summarize an operator-declared Develop test inventory.

    This records where checks are intended to be exercised. It does not inspect
    test code, count executed test cases, or assert that a test proves its label.
    """
    requirement_by_id = {item["id"]: item for item in requirements}
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
            require(isinstance(check_id, str) and check_id in mandatory_check_ids
                    and check_id in requirement_by_id[requirement_id]["check_ids"] and check_id in check_index,
                    "COVERAGE_CHECK", "Coverage scenario check must be mandatory and mapped to its requirement")
            require(isinstance(kind, str) and kind in COVERAGE_KINDS,
                    "COVERAGE_KIND", "Unknown Develop coverage scenario kind")
            reference = fields(item["test_ref"], {"path", "case_id"} | ({"framework"} if version2 else set()), {"path", "case_id"})
            path = relative_path(reference["path"])
            case_id = reference["case_id"]
            require(path in bundle_paths, "COVERAGE_TEST_REFERENCE", "Coverage test reference must point into the pinned bundle")
            if version2:
                develop_cases.node_id(case_id)
                require(reference.get("framework") == "pytest" and case_id.split("::", 1)[0] == path
                        and check_index[check_id].get("adapter", {}).get("id") == "pytest-cases-v1"
                        and path in check_index[check_id]["adapter"]["paths"], "COVERAGE_TEST_REFERENCE", "Pytest reference must match its declared collection file/check")
                require(type(item.get("required", False)) is bool, "COVERAGE_TEST_REFERENCE", "required must be boolean")
            else:
                require(isinstance(case_id, str) and bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.:-]{0,199}", case_id)),
                        "COVERAGE_TEST_REFERENCE", "Coverage test reference needs a bounded logical case ID")
            scenarios.append({"id": scenario_id, "requirement_id": requirement_id, "check_id": check_id,
                              "kind": kind, "test_ref": {"path": path, "case_id": case_id}})
            if version2:
                scenarios[-1].update(required=item.get("required", False))
                scenarios[-1]["test_ref"]["framework"] = "pytest"
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
              "limitations": ["Test references are checked against the pinned bundle path; test names and assertions are not inspected.",
                              "The verifier measures configured checks as a whole; this inventory does not create per-scenario observations.",
                              "Declared scenarios and known gaps are not proof of test validity, independence or goal completeness."]}
    if coverage is not None:
        report["source_hash"] = canonical_hash(coverage)
        if coverage.get("schema_version") == "develop-coverage-v2":
            report["limitations"][0] = "Exact node IDs are declared here; discovered/selected/executed cases are measured only during verification."
            report["limitations"][1] = "Only explicit required scenarios become normalized Gate conditions; other links remain advisory."
    summary = {"schema_version": report["schema_version"], "status": status, "profile": report["profile"],
               "scenario_count": len(scenarios), "known_gap_count": len(gaps),
               "requirements": requirement_inventory, "unlisted_requirement_ids": unlisted,
               "assurance": assurance}
    if coverage is not None:
        summary["source_hash"] = report["source_hash"]
        report["summary_hash"] = canonical_hash(summary)
    return report, summary


def build_contract(domain_id: str, goal: str, parameters: dict, verifier: dict) -> dict:
    require(domain_id == "develop", "DOMAIN_UNSUPPORTED", "Only develop is supported")
    # Packaged extraction of the existing Develop manifest, not a second model
    # policy loader. Execution permissions from that manifest are NOT granted.
    source_domain = decode(files("harness_external").joinpath("develop_manifest.json").read_text(encoding="utf-8"))
    require(source_domain["id"] == domain_id and {"artifact", "command_exit", "behavior"} <= set(source_domain["verification"]["criterionTemplates"]),
            "DOMAIN_POLICY_MISMATCH", "Unsupported source domain criteria")
    require(isinstance(goal, str) and 0 < len(goal.strip()) <= 16000, "GOAL_REQUIRED", "Provide the original task goal")
    fields(parameters, {"inputs", "artifacts", "expectations", "test_commands", "pytest_checks", "validation_profile", "profile"}, {"inputs", "artifacts", "expectations"})
    inputs = parameters["inputs"]
    artifacts = parameters["artifacts"]
    require(isinstance(inputs, list) and 1 <= len(inputs) <= 128, "INPUTS_REQUIRED", "List 1..128 input files, including tests/fixtures")
    inputs = [relative_path(item) for item in inputs]
    require(len({p.casefold() for p in inputs}) == len(inputs), "INVALID_PATH", "Duplicate/case-colliding input paths")
    require(not any(b.startswith(a + "/") for a in inputs for b in inputs if a != b), "INVALID_PATH", "A file cannot also be a directory")
    require(isinstance(artifacts, list) and artifacts and all(isinstance(p, str) and p in inputs for p in artifacts),
            "ARTIFACTS_REQUIRED", "Artifacts must be included in inputs")
    require(len(set(artifacts)) == len(artifacts), "INVALID_PARAMETERS", "Duplicate artifacts")
    profile = parameters.get("profile", "execution")
    require(isinstance(profile, str) and profile in {"structural", "execution"}, "INVALID_PARAMETERS", "Profile is structural or execution")
    expectations = parameters["expectations"]
    require(isinstance(expectations, list) and 1 <= len(expectations) <= 128, "NEEDS_INPUT", "Concrete file expectations are required; goal text alone is not a test")
    checks = []
    covered = set()
    for expectation in expectations:
        check = normalize_file(expectation, inputs)
        covered.add(check["path"])
        checks.append(check)
    require(set(artifacts) <= covered, "NEEDS_INPUT", "Every artifact needs at least one explicit expectation")
    commands = parameters.get("test_commands", [])
    pytest_checks = parameters.get("pytest_checks", [])
    require(isinstance(pytest_checks, list) and len(pytest_checks) <= 8, "INVALID_PARAMETERS", "At most eight pytest checks are supported")
    require(isinstance(commands, list) and len(commands) <= 8, "INVALID_PARAMETERS", "At most eight test commands are supported")
    require((profile == "execution" and bool(commands or pytest_checks)) or (profile == "structural" and not commands and not pytest_checks),
            "NEEDS_INPUT", "Execution requires test_commands; structural explicitly excludes command execution")
    for command in commands:
        checks.append(normalize_command(command))
    for item in pytest_checks:
        checks.append(develop_cases.normalize(item, inputs))
    require(len({c["check_key"] for c in checks}) == len(checks), "CHECK_ID_AMBIGUOUS",
            "Checks with the same target/operator or command need distinct explicit id values")
    body = {
        "schema_version": "external-goal-contract-v1", "domain_id": "develop", "domain_revision": "develop-external-4",
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
    return {**body, "contract_hash": canonical_hash(body)}


class DevelopModule:
    identity_files = (str(files("harness_external").joinpath("develop_manifest.json")), str(files("harness_external").joinpath("develop_cases.py")))
    identity_file_ids = ("develop-manifest", "develop-case-semantics")
    domain_id = "develop"
    revision = "develop-external-5"

    def prepare_acceptance(self, goal, parameters, verifier, *, policy, intent=None):
        """Compile configured acceptance plus revisable caller exploration.

        Requirements and evidence kinds are Develop semantics. Core receives
        normalized checks/IDs and pins them without deciding what the goal means.
        """
        base = copy.deepcopy(policy["parameters"])
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
        for key in required:
            if indexed[key].get("adapter"):
                require(set(indexed[key]["adapter"]["paths"]) <= bundle_paths, "ACCEPTANCE_SCOPE", "Case-aware acceptance test files must belong to the pinned bundle")
        require(bundle_paths <= set(base["inputs"]) and not bundle_paths.intersection(base["artifacts"]),
                "ACCEPTANCE_SCOPE", "Test bundle files must be inputs, not implementation artifacts")
        require(not any(indexed[key]["kind"] == "command" for key in required) or bool(bundle_paths),
                "ACCEPTANCE_BUNDLE_REQUIRED", "Command acceptance requires an explicitly pinned test/fixture bundle")
        requirements = policy["requirements"]
        require(isinstance(requirements, list) and 1 <= len(requirements) <= 100,
                "REQUIREMENTS_REQUIRED", "Acceptance requires a bounded requirement-to-check mapping")
        ids, mapped = set(), set()
        for requirement in requirements:
            fields(requirement, {"id", "statement", "check_ids", "minimum_evidence"}, {"id", "statement", "check_ids"})
            key = requirement["id"]
            require(isinstance(key, str) and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", key)) and key not in ids,
                    "REQUIREMENT_ID", "Requirements need unique logical IDs")
            ids.add(key)
            require(isinstance(requirement["statement"], str) and 0 < len(requirement["statement"]) <= 2000,
                    "REQUIREMENT_INVALID", "A bounded requirement statement is required")
            references = requirement["check_ids"]
            require(isinstance(references, list) and len(references) <= 128 and all(isinstance(x, str) for x in references)
                    and len(set(references)) == len(references) and set(references) <= set(required),
                    "REQUIREMENT_REFERENCE", "Requirements may refer only to defined mandatory checks")
            minimum = requirement.get("minimum_evidence", "file")
            require(isinstance(minimum, str) and minimum in {"file", "command", "testcase"}, "REQUIREMENT_INVALID", "minimum_evidence is file, command or testcase")
            require(minimum not in {"command", "testcase"} or any(indexed[x]["kind"] == "command" for x in references),
                    "REQUIREMENT_EVIDENCE", "A command requirement cannot be represented by file-only checks")
            require(minimum != "testcase" or any(indexed[x].get("adapter") for x in references), "REQUIREMENT_EVIDENCE", "A testcase requirement needs a case-aware check")
            mapped.update(references)
        require(mapped == set(required), "REQUIREMENT_REFERENCE", "Every mandatory check must map to a requirement")
        coverage_inventory, coverage_summary = compile_coverage_inventory(
            policy.get("coverage"), requirements, set(required), indexed, bundle_paths)
        base = develop_cases.apply_required_scenarios(base, policy.get("coverage"))
        if policy.get("coverage", {}).get("schema_version") == "develop-coverage-v2":
            checks = self.prepare(goal, base, verifier, exploratory=False, intent=intent)["contract"]["checks"]
        fields(parameters, {"inputs", "artifacts", "expectations", "test_commands", "pytest_checks", "validation_profile", "profile"})
        require(not (base["profile"] == "execution" and parameters.get("profile") == "structural"),
                "ACCEPTANCE_SCOPE", "Configured command acceptance cannot be downgraded to structural checks")
        merged = copy.deepcopy(base)
        for key in ("inputs", "artifacts"):
            extra = parameters.get(key, [])
            require(isinstance(extra, list), "INVALID_PARAMETERS", key + " must be a list")
            for path in extra:
                relative_path(path)
            merged[key] = list(dict.fromkeys(base[key] + extra))
        for key in ("expectations", "test_commands", "pytest_checks"):
            extra = parameters.get(key, [])
            require(isinstance(extra, list), "INVALID_PARAMETERS", key + " must be a list")
            originals = list(base.get(key, []))
            for proposal in extra:
                normalized = normalize_file(proposal, merged["inputs"]) if key == "expectations" else develop_cases.normalize(proposal, merged["inputs"]) if key == "pytest_checks" else normalize_command(proposal)
                old = next((c for c in checks if c["check_key"] == normalized["check_key"]), None)
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
        result["contract"]["coverage_inventory"] = coverage_inventory
        result["contract"]["acceptance_requirements"] = copy.deepcopy(requirements)
        result["contract"]["limitations"].append("Requirement and scenario links express configured inventory, not proof that tests adequately represent the goal.")
        result["contract"]["contract_hash"] = canonical_hash({k: v for k, v in result["contract"].items() if k != "contract_hash"})
        return result

    def prepare(self, goal, parameters, verifier, *, exploratory=False, intent=None):
        if intent is not None:
            require(intent["original_goal"] == goal and intent["domain_id"] == self.domain_id, "DOMAIN_INTENT_BINDING", "Domain preparation received the wrong intent")
        require(isinstance(goal, str) and 0 < len(goal.strip()) <= 16000, "GOAL_REQUIRED", "Provide the original task goal")
        fields(parameters, {"inputs", "artifacts", "expectations", "test_commands", "pytest_checks", "validation_profile", "profile"})
        perspective = parameters.get("validation_profile")
        require(perspective is None or isinstance(perspective, str) and perspective in develop_cases.PERSPECTIVES, "VALIDATION_PROFILE", "Unknown Develop perspective profile")
        profile = parameters.get("profile", "execution")
        require(isinstance(profile, str) and profile in {"structural", "execution"}, "INVALID_PARAMETERS", "Unknown Develop profile")
        for key in ("inputs", "artifacts", "expectations", "test_commands", "pytest_checks"):
            if key in parameters:
                require(isinstance(parameters[key], list), "INVALID_PARAMETERS", "Develop parameter must be a list: " + key)
        for item in parameters.get("inputs", []) + parameters.get("artifacts", []):
            relative_path(item)
        inputs = parameters.get("inputs", [])
        require(len(inputs) <= 128 and len({p.casefold() for p in inputs}) == len(inputs)
                and not any(b.startswith(a + "/") for a in inputs for b in inputs if a != b), "INVALID_PATH", "Invalid partial input file scope")
        missing = [key for key in ("inputs", "artifacts", "expectations") if not parameters.get(key)]
        if profile == "execution" and not (parameters.get("test_commands") or parameters.get("pytest_checks")):
            missing.append("test_commands")
        if not exploratory or not missing:
            contract = build_contract(self.domain_id, goal, parameters, verifier)
            result = {"status": "proceed", "questions": [], "contract": contract, "available_operations": ["submit", "verify", "finish_completed"]}
            if perspective:
                result["validation_perspectives"] = {"profile_id": perspective, "revision": "1", "perspectives": develop_cases.PERSPECTIVES[perspective], "meaning": "advisory_not_mandatory_tests"}
            return result
        # Missing domain data is a durable clarification state, never invented criteria.
        questions = [{"id": "develop:" + key, "parameter": key, "reason": "missing_domain_parameter"} for key in missing]
        source = decode(files("harness_external").joinpath("develop_manifest.json").read_text())
        contract = {"schema_version": "external-goal-contract-v1", "domain_id": self.domain_id, "domain_revision": self.revision,
                    "source_domain": {"id": source["id"], "revision": source["revision"], "manifest_hash": canonical_hash(source)},
                    "original_goal": goal, "profile": profile, "inputs": parameters.get("inputs", []),
                    "artifacts": parameters.get("artifacts", []), "checks": [], "verifier": verifier,
                    "rules": {"all_checks_required": False, "snapshot_required": True, "command_execution": "strict-sandbox-only",
                              "caller_observations_are_evidence": False, "ready_attestation": False},
                    "limitations": ["Local unsigned record; same-user tampering is outside this assurance.",
                                    "Goal interpretation and caller-authored tests do not prove goal coverage or independence."]}
        contract["contract_hash"] = canonical_hash(contract)
        return {"status": "needs_input", "questions": questions, "contract": contract,
                "available_operations": ["submit", "verify"] if inputs else []}

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
            adapter = fields(parameters["adapter"], {"id", "paths", "args", "rules"}, {"id", "paths", "args", "rules"})
            require(adapter["id"] == "pytest-cases-v1", "CHECK_UNSUPPORTED", "Unknown case adapter")
            rules = fields(adapter["rules"], {"required_case_ids", "allowed_outcomes", "minimum_selected", "require_complete_session"},
                           {"required_case_ids", "allowed_outcomes", "minimum_selected", "require_complete_session"})
            result = develop_cases.normalize({"id": "normalized", "paths": adapter["paths"], "args": adapter["args"],
                         "required_cases": rules["required_case_ids"], "allowed_outcomes": rules["allowed_outcomes"],
                         "timeout_seconds": parameters.get("timeout_seconds", 30)}, contract["inputs"])
            require(result["argv"] == parameters["argv"] and result["cwd"] == parameters["cwd"] and result["adapter"] == adapter,
                    "CHECK_SCOPE_CONFLICT", "Case-aware command changed")
            return result
        return normalize_command({key: value for key, value in parameters.items() if key not in {"kind", "expectedExitCode"}})
