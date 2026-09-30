"""Develop's invocation contract; callers supply parameters, never verifier policy."""
from __future__ import annotations

import re
from importlib.resources import files
from typing import Any

from harness.measurement_v5 import decode
from harness.common import canonical_bytes, canonical_hash


class HarnessError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def require(condition: bool, code: str, message: str) -> None:
    if not condition:
        raise HarnessError(code, message)


def fields(value: Any, allowed: set[str], required: set[str] | None = None) -> dict:
    require(isinstance(value, dict) and set(value) <= allowed and (required or set()) <= set(value),
            "INVALID_PARAMETERS", "Unexpected or missing object fields")
    return value


def relative_path(value: Any, *, directory: bool = False) -> str:
    if directory and value == ".":
        return value
    require(isinstance(value, str) and 0 < len(value) <= 512, "INVALID_PATH", "A relative path is required")
    parts = value.split("/")
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    require(not re.search(r"[\x00-\x1f\x7f\\:]", value) and all(
        part not in {"", ".", ".."} and not part.endswith((" ", ".")) and part.split(".")[0].upper() not in reserved
        for part in parts), "INVALID_PATH", "Paths must stay inside the explicit workspace (no links, devices or traversal)")
    return value


def integer(value: Any, low: int, high: int, name: str) -> int:
    require(type(value) is int and low <= value <= high, "INVALID_PARAMETERS", f"{name} must be {low}..{high}")
    return value


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
    require(profile in {"structural", "execution"}, "INVALID_PARAMETERS", "Profile is structural or execution")
    expectations = parameters["expectations"]
    require(isinstance(expectations, list) and 1 <= len(expectations) <= 128, "NEEDS_INPUT", "Concrete file expectations are required; goal text alone is not a test")
    checks = []
    covered = set()
    for expectation in expectations:
        fields(expectation, {"path", "operator", "expected"}, {"path", "operator", "expected"})
        target = relative_path(expectation["path"])
        require(target in inputs, "INVALID_PARAMETERS", "Expectation target must be an input")
        operator, expected = expectation["operator"], expectation["expected"]
        require(operator in {"equals", "contains", "sha256"} and isinstance(expected, str) and len(expected) <= 64000,
                "INVALID_PARAMETERS", "Use equals, contains or sha256 with a bounded string")
        require(operator != "contains" or bool(expected), "INVALID_PARAMETERS", "Empty substring checks are vacuous")
        require(operator != "sha256" or bool(re.fullmatch(r"[a-f0-9]{64}", expected)), "INVALID_PARAMETERS", "Expected sha256 is invalid")
        covered.add(target)
        checks.append({"kind": "file", **expectation})
    require(set(artifacts) <= covered, "NEEDS_INPUT", "Every artifact needs at least one explicit expectation")
    commands = parameters.get("test_commands", [])
    require(isinstance(commands, list) and len(commands) <= 8, "INVALID_PARAMETERS", "At most eight test commands are supported")
    require((profile == "execution" and bool(commands)) or (profile == "structural" and not commands),
            "NEEDS_INPUT", "Execution requires test_commands; structural explicitly excludes command execution")
    for command in commands:
        fields(command, {"argv", "cwd", "timeout_seconds"}, {"argv"})
        argv = command["argv"]
        require(isinstance(argv, list) and 1 <= len(argv) <= 64 and all(
            isinstance(a, str) and len(a) <= 8192 and "\x00" not in a for a in argv) and bool(argv[0].strip()),
            "INVALID_PARAMETERS", "Test command must be a bounded argv array")
        require(len(canonical_bytes(argv)) <= 64000, "INVALID_PARAMETERS", "Test argv exceeds 64 KiB")
        checks.append({"kind": "command", "argv": argv, "cwd": relative_path(command.get("cwd", "."), directory=True),
                       "timeout_seconds": integer(command.get("timeout_seconds", 30), 1, 120, "timeout_seconds"), "expectedExitCode": 0})
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


def validate_contract(contract: dict) -> None:
    body = {key: value for key, value in contract.items() if key != "contract_hash"}
    require(contract.get("contract_hash") == canonical_hash(body), "CONTRACT_CORRUPT", "Contract digest mismatch")


def limits(value: dict | None) -> dict:
    value = fields({} if value is None else value, {"max_actions", "max_verifications", "timeout_seconds"})
    return {"max_actions": integer(value.get("max_actions", 50), 1, 1000, "max_actions"),
            "max_verifications": integer(value.get("max_verifications", 3), 1, 20, "max_verifications"),
            "timeout_seconds": integer(value.get("timeout_seconds", 3600), 10, 86400, "timeout_seconds")}
