"""Operator-configured policy transport and immutable bundle identities.

This is not an authorization server. Configuration is a separate input channel,
not proof that a human approved a task or that same-user tampering is impossible.
Domain modules, not this module, interpret requirements and check definitions.
"""
from __future__ import annotations

import copy
from pathlib import Path
import re

from harness.common import canonical_hash
from harness.json_codec import decode
from .errors import fields, relative_path, require
from .snapshots import capture, no_links, validate_candidate


def policy_name(value):
    require(isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", value)),
            "POLICY_ID_INVALID", "Policy ID must be a bounded registry name")
    return value


def load_document(path):
    no_links(path)
    require(path.is_file(), "POLICY_NOT_FOUND", "Configured policy document is missing")
    with path.open("rb") as stream:
        raw = stream.read(256 * 1024 + 1)
    require(len(raw) <= 256 * 1024, "POLICY_TOO_LARGE", "Policy exceeds 256 KiB")
    return decode(raw.decode("utf-8"))


def compiled_check_ids(preparation, definition):
    """Validate reference preservation, without interpreting any check semantics."""
    expected = definition.get("required_check_ids")
    actual = preparation.get("acceptance_check_ids") if isinstance(preparation, dict) else None
    def valid(value):
        return (isinstance(value, list) and len(value) <= 256
                and all(isinstance(key, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", key) for key in value)
                and len(set(value)) == len(value))
    require(valid(expected) and valid(actual) and set(expected) == set(actual),
            "ACCEPTANCE_POLICY_BINDING", "Domain compilation must preserve the configured mandatory check references")
    return copy.deepcopy(expected)


def validate_coverage_binding(contract, domain_preparation, definition):
    """Ensure a Domain did not silently omit the configured coverage inventory.

    Core binds the declaration bytes only. It does not interpret the Domain's
    scenario labels or claim that the referenced tests prove their requirements.
    """
    if "coverage" not in definition:
        return
    expected = canonical_hash(definition["coverage"])
    inventory = contract.get("coverage_inventory") if isinstance(contract, dict) else None
    summary = domain_preparation.get("coverage_inventory") if isinstance(domain_preparation, dict) else None
    require(isinstance(inventory, dict) and inventory.get("source_hash") == expected
            and isinstance(summary, dict) and summary.get("source_hash") == expected
            and inventory.get("summary_hash") == canonical_hash(summary),
            "ACCEPTANCE_COVERAGE_BINDING", "Domain compilation must preserve the configured coverage inventory")


def validate_preparation(preparation, definition):
    compiled_check_ids(preparation, definition)
    validate_coverage_binding((preparation or {}).get("contract"),
                              {"coverage_inventory": (preparation or {}).get("coverage_inventory_summary")}, definition)


class PolicyRegistry:
    """Read-only configured source, never populated by start/revise/check."""

    def __init__(self, root=None):
        self.root = Path(root).expanduser().absolute() if root is not None else None
        if self.root is not None:
            no_links(self.root)
            require(self.root.is_dir(), "POLICY_ROOT_MISSING", "Configured policy root must exist")
            self.root = self.root.resolve()

    def defaults(self):
        if self.root is None or not (self.root / "registry.json").exists():
            return {}
        config = fields(load_document(self.root / "registry.json"), {"defaults"}, {"defaults"})
        require(isinstance(config["defaults"], dict) and len(config["defaults"]) <= 64,
                "POLICY_CONFIG_INVALID", "defaults must map Domain IDs to policy IDs")
        for domain_id, name in config["defaults"].items():
            require(isinstance(domain_id, str) and 0 < len(domain_id) <= 96, "POLICY_CONFIG_INVALID", "Invalid Domain ID")
            policy_name(name)
        return config["defaults"]

    def select(self, domain_id, requested=None):
        default = self.defaults().get(domain_id)
        require(not default or requested in (None, default), "POLICY_SELECTION_PINNED",
                "The configured Domain policy cannot be replaced by a caller-selected policy")
        name = default or requested
        if name is None:
            return None
        policy_name(name)
        require(self.root is not None, "POLICY_NOT_CONFIGURED", "Selecting a policy requires an operator-configured registry")
        directory = self.root / name
        document = fields(load_document(directory / "policy.json"),
                          {"schema_version", "policy_id", "revision", "domain_id", "parameters", "requirements",
                           "required_check_ids", "bundle", "approval", "authorship", "coverage"},
                          {"schema_version", "policy_id", "revision", "domain_id", "parameters", "requirements",
                           "required_check_ids", "bundle", "approval"})
        require(document["schema_version"] == "acceptance-policy-v1" and document["policy_id"] == name
                and document["domain_id"] == domain_id, "POLICY_IDENTITY", "Policy schema, ID or Domain mismatch")
        require(isinstance(document["revision"], str) and 0 < len(document["revision"]) <= 128,
                "POLICY_IDENTITY", "A bounded policy revision is required")
        require(isinstance(document["parameters"], dict), "POLICY_INVALID", "Domain parameters must be an object")
        # Coverage is an opaque, Domain-owned declaration. Core preserves and
        # hashes it; the selected Domain validates its schema and references.
        require("coverage" not in document or isinstance(document["coverage"], dict),
                "POLICY_INVALID", "Domain coverage declaration must be an object")
        approval = fields(document["approval"], {"declared_by", "reference"}, {"declared_by", "reference"})
        require(all(isinstance(v, str) and 0 < len(v) <= 2000 for v in approval.values()),
                "POLICY_INVALID", "Configured approval needs an author and reference")
        authors = fields(document.get("authorship", {"criteria": "unknown", "tests": "unknown"}),
                         {"criteria", "tests"}, {"criteria", "tests"})
        require(all(isinstance(v, str) and v in {"user", "model", "application", "unknown"} for v in authors.values()),
                "POLICY_INVALID", "Criteria/test authorship must be declared separately from approval")
        bundle = fields(document["bundle"], {"version", "files"}, {"version", "files"})
        require(isinstance(bundle["version"], str) and 0 < len(bundle["version"]) <= 128,
                "POLICY_INVALID", "Bundle version is required")
        paths = bundle["files"]
        require(isinstance(paths, list) and len(paths) <= 128, "POLICY_INVALID", "Bundle files must be a bounded list")
        for path in paths:
            relative_path(path)
        require(len({p.casefold() for p in paths}) == len(paths)
                and not any(b.startswith(a + "/") for a in paths for b in paths if a != b),
                "POLICY_INVALID", "Bundle paths must be unique files")
        return {"definition": copy.deepcopy(document), "definition_hash": canonical_hash(document),
                "bundle_source": str(directory / "bundle"),
                "selection": "operator_default" if default else "caller_selected_registered_policy"}


def pin(selected, run_directory):
    definition = selected["definition"]
    bundle = capture(Path(selected["bundle_source"]), sorted(definition["bundle"]["files"]), run_directory)
    body = {"definition": definition, "definition_hash": selected["definition_hash"], "bundle": bundle,
            "authority": {"source": "operator_configuration", "selection": selected["selection"],
                          "approval_status": "configured_not_authenticated", "approval": definition["approval"],
                          "authorship": definition.get("authorship", {"criteria": "unknown", "tests": "unknown"}),
                          "change_authority": "operator_configuration_and_new_run",
                          "same_user_tamper_protection": False}}
    return {**body, "binding_hash": canonical_hash(body)}


def validate(binding, run_directory=None):
    require(binding["binding_hash"] == canonical_hash({k: v for k, v in binding.items() if k != "binding_hash"})
            and binding["definition_hash"] == canonical_hash(binding["definition"]),
            "ACCEPTANCE_BINDING_CORRUPT", "Acceptance policy identity changed")
    require(set(binding["definition"]["bundle"]["files"]) == {f["path"] for f in binding["bundle"]["manifest"]["files"]},
            "ACCEPTANCE_BINDING_CORRUPT", "Acceptance file scope changed")
    if run_directory is not None:
        validate_candidate(binding["bundle"], bundle_payload(binding, run_directory))


def bundle_payload(binding, run_directory):
    from .store import identifier
    return run_directory / identifier(binding["bundle"]["candidate_id"], "candidate") / "payload"


def validate_subject(binding, candidate):
    expected = binding["bundle"]
    found = {entry["path"]: entry for entry in candidate["manifest"]["files"]}
    require(all(found.get(entry["path"]) == entry
                and candidate["executable"].get(entry["path"]) == expected["executable"][entry["path"]]
                for entry in expected["manifest"]["files"]),
            "ACCEPTANCE_SUBJECT_CHANGED", "Submitted acceptance test/fixture bytes differ from the pinned bundle")


def view(binding):
    if not binding:
        return {"source": "caller_defined", "approval_status": "not_established", "bundle_pinned": False,
                "goal_coverage": "not_established"}
    definition = binding["definition"]
    return {"source": "operator_configuration", "policy_id": definition["policy_id"], "revision": definition["revision"],
            "definition_hash": binding["definition_hash"], "binding_hash": binding["binding_hash"],
            "bundle_hash": binding["bundle"]["candidate_hash"], "bundle_version": definition["bundle"]["version"],
            "bundle_pinned": True, "authority": binding["authority"], "requirements": definition["requirements"],
            "unmapped_requirement_ids": [r["id"] for r in definition["requirements"] if not r["check_ids"]],
            "goal_coverage": "declared_mapping_not_semantically_proven"}
