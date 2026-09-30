"""Develop semantics behind an explicit Domain interface."""
from __future__ import annotations

import copy
import re
from importlib.resources import files

from harness.measurement_v5 import decode
from harness.common import canonical_bytes, canonical_hash
from .errors import HarnessError, require, fields, relative_path, integer, limits, validate_contract


def normalize_file(expectation, inputs):
    fields(expectation, {"path", "operator", "expected"}, {"path", "operator", "expected"})
    target = relative_path(expectation["path"])
    require(target in inputs, "INVALID_PARAMETERS", "Expectation target must be an input")
    operator, expected = expectation["operator"], expectation["expected"]
    require(isinstance(operator, str) and operator in {"equals", "contains", "sha256"} and isinstance(expected, str) and len(expected) <= 64000,
            "INVALID_PARAMETERS", "Use equals, contains or sha256 with a bounded string")
    require(operator != "contains" or bool(expected), "INVALID_PARAMETERS", "Empty substring checks are vacuous")
    require(operator != "sha256" or bool(re.fullmatch(r"[a-f0-9]{64}", expected)), "INVALID_PARAMETERS", "Expected sha256 is invalid")
    return {"kind": "file", **copy.deepcopy(expectation)}


def normalize_command(command):
    fields(command, {"argv", "cwd", "timeout_seconds"}, {"argv"})
    argv = command["argv"]
    require(isinstance(argv, list) and 1 <= len(argv) <= 64 and all(
        isinstance(a, str) and len(a) <= 8192 and "\x00" not in a for a in argv) and bool(argv[0].strip()),
        "INVALID_PARAMETERS", "Test command must be a bounded argv array")
    require(len(canonical_bytes(argv)) <= 64000, "INVALID_PARAMETERS", "Test argv exceeds 64 KiB")
    return {"kind": "command", "argv": copy.deepcopy(argv), "cwd": relative_path(command.get("cwd", "."), directory=True),
            "timeout_seconds": integer(command.get("timeout_seconds", 30), 1, 120, "timeout_seconds"), "expectedExitCode": 0}


def build_contract(domain_id: str, goal: str, parameters: dict, verifier: dict) -> dict:
    require(domain_id == "develop", "DOMAIN_UNSUPPORTED", "Only develop is supported")
    # Packaged extraction of the existing Develop manifest, not a second model
    # policy loader. Execution permissions from that manifest are NOT granted.
    source_domain = decode(files("harness_external").joinpath("develop_manifest.json").read_text(encoding="utf-8"))
    require(source_domain["id"] == domain_id and {"artifact", "command_exit", "behavior"} <= set(source_domain["verification"]["criterionTemplates"]),
            "DOMAIN_POLICY_MISMATCH", "Unsupported source domain criteria")
    require(isinstance(goal, str) and 0 < len(goal.strip()) <= 16000, "GOAL_REQUIRED", "Provide the original task goal")
    fields(parameters, {"inputs", "artifacts", "expectations", "test_commands", "profile"}, {"inputs", "artifacts", "expectations"})
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
    require(isinstance(commands, list) and len(commands) <= 8, "INVALID_PARAMETERS", "At most eight test commands are supported")
    require((profile == "execution" and bool(commands)) or (profile == "structural" and not commands),
            "NEEDS_INPUT", "Execution requires test_commands; structural explicitly excludes command execution")
    for command in commands:
        checks.append(normalize_command(command))
    body = {
        "schema_version": "external-goal-contract-v1", "domain_id": "develop", "domain_revision": "develop-external-1",
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
    domain_id = "develop"
    revision = "develop-external-2"

    def prepare(self, goal, parameters, verifier, *, exploratory=False, intent=None):
        if intent is not None:
            require(intent["original_goal"] == goal and intent["domain_id"] == self.domain_id, "DOMAIN_INTENT_BINDING", "Domain preparation received the wrong intent")
        require(isinstance(goal, str) and 0 < len(goal.strip()) <= 16000, "GOAL_REQUIRED", "Provide the original task goal")
        fields(parameters, {"inputs", "artifacts", "expectations", "test_commands", "profile"})
        profile = parameters.get("profile", "execution")
        require(isinstance(profile, str) and profile in {"structural", "execution"}, "INVALID_PARAMETERS", "Unknown Develop profile")
        for key in ("inputs", "artifacts", "expectations", "test_commands"):
            if key in parameters:
                require(isinstance(parameters[key], list), "INVALID_PARAMETERS", "Develop parameter must be a list: " + key)
        for item in parameters.get("inputs", []) + parameters.get("artifacts", []):
            relative_path(item)
        inputs = parameters.get("inputs", [])
        require(len(inputs) <= 128 and len({p.casefold() for p in inputs}) == len(inputs)
                and not any(b.startswith(a + "/") for a in inputs for b in inputs if a != b), "INVALID_PATH", "Invalid partial input file scope")
        missing = [key for key in ("inputs", "artifacts", "expectations") if not parameters.get(key)]
        if profile == "execution" and not parameters.get("test_commands"):
            missing.append("test_commands")
        if not exploratory or not missing:
            contract = build_contract(self.domain_id, goal, parameters, verifier)
            return {"status": "proceed", "questions": [], "contract": contract, "available_operations": ["submit", "verify", "finish_completed"]}
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
        fields(parameters, {"kind", "path", "operator", "expected", "argv", "cwd", "timeout_seconds", "expectedExitCode"}, {"kind"})
        if parameters["kind"] == "file":
            fields(parameters, {"kind", "path", "operator", "expected"}, {"kind", "path", "operator", "expected"})
            return normalize_file({key: value for key, value in parameters.items() if key != "kind"}, contract["inputs"])
        require(parameters["kind"] == "command" and contract["profile"] == "execution", "CHECK_UNSUPPORTED", "Command checks require the execution profile")
        require(bool(contract["inputs"]), "NEEDS_INPUT", "Declare input scope before command measurements")
        require(type(parameters.get("expectedExitCode", 0)) is int and parameters.get("expectedExitCode", 0) == 0,
                "CHECK_UNSUPPORTED", "Develop command checks expect exit zero")
        return normalize_command({key: value for key, value in parameters.items() if key not in {"kind", "expectedExitCode"}})
