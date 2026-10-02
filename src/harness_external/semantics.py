"""Revisioned data, provenance and gate binding. No model or domain decisions."""
from __future__ import annotations

import copy
import re
import time

from harness.common import canonical_hash
from .errors import fields, integer, require


SEMANTIC_VERSION = "closeout-semantics-v1"


def named(value: str) -> str:
    require(isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", value)), "INVALID_REFERENCE", "Invalid logical record ID")
    return value


def record(record_id: str, revision: int, body: dict) -> dict:
    return {**copy.deepcopy(body), "ref": {"id": record_id, "revision": revision, "sha256": canonical_hash(body)}}


def validate(record: dict) -> None:
    require(isinstance(record, dict) and isinstance(record.get("ref"), dict), "SEMANTIC_RECORD_CORRUPT", "Record has no revision reference")
    ref = record["ref"]
    require(set(ref) == {"id", "revision", "sha256"} and isinstance(ref["id"], str)
            and type(ref["revision"]) is int and ref["revision"] > 0
            and ref["sha256"] == canonical_hash({k: v for k, v in record.items() if k != "ref"}),
            "SEMANTIC_RECORD_CORRUPT", "Record digest mismatch")


def parameters_only(parameters):
    return {k: copy.deepcopy(v) for k, v in parameters.items() if k not in {"check_key", "check_id"}}


def provenance(value=None):
    value = fields({} if value is None else value, {"declared_author", "approval_reference"})
    author = value.get("declared_author", "unknown")
    require(isinstance(author, str) and author in {"user", "model", "application", "unknown"}, "INVALID_PROVENANCE", "Invalid declared author")
    reference = value.get("approval_reference")
    require(reference is None or isinstance(reference, str) and 0 < len(reference) <= 2000, "INVALID_PROVENANCE", "Invalid approval reference")
    return {"declared_author": author, "author_trust": "caller_claim",
            "approval": {"status": "unverified" if reference else "not_provided", "claimed_reference": reference,
                         "authenticated_by": None}}


def check_record(run_id: str, check_id: str, revision: int, parameters: dict, interpretation_ref: dict, *,
                 origin="caller_proposal", author="unknown", generator=None, role="exploratory", authority=None) -> dict:
    named(check_id)
    definition_key = parameters.get("check_key") if origin == "domain_preparation" else None
    parameters = parameters_only(parameters)
    spec = {"schemaVersion": "check-spec-v1", "author": author, "executorId": "python-measurement",
            "supportedSubjects": ["candidate"], "parameters": copy.deepcopy(parameters),
            "requiredCapabilities": ["execute"] if parameters["kind"] == "command" else ["read"],
            "timeoutMs": parameters.pop("timeout_seconds", 30) * 1000 if parameters["kind"] == "command" else 1000}
    # timeout_seconds is operational metadata, not a v5 command parameter.
    spec["parameters"].pop("timeout_seconds", None)
    return record("check:" + run_id + ":" + check_id, revision,
                  {"check_id": check_id, "active": True, "origin": origin, "trust": "untrusted", "interpretation_ref": interpretation_ref,
                   "role": role, "authority": authority or {"source": "caller_proposal", "approval_status": "not_established"},
                   "definition_key": definition_key, "authored_by": {"declared_kind": author,
                       "trust": "configuration_claim" if authority and authority.get("source") == "operator_configuration" else "caller_claim"},
                   "generated_by": generator or {"kind": "domain_normalization"}, "spec": spec})


def check_equivalent(existing: dict, parameters: dict) -> bool:
    normalized = parameters_only(parameters)
    timeout = normalized.pop("timeout_seconds", 30) * 1000 if normalized["kind"] == "command" else 1000
    return existing["spec"]["parameters"] == normalized and existing["spec"]["timeoutMs"] == timeout


def ensure(run: dict, *, mode="strict", required_checks=None, deferred_checks=None, parameters=None, questions=None,
           domain_revision=None, domain_identity=None, policy_provenance=None, projected=False, initial_intent=None,
           acceptance_binding=None) -> None:
    if run.get("semantic_schema_version"):
        return
    run_id, contract = run["run_id"], run["contract"]
    params = parameters if parameters is not None else {
        "inputs": contract["inputs"], "artifacts": contract["artifacts"], "profile": contract["profile"],
        "expectations": [{k: v for k, v in c.items() if k != "kind"} for c in contract["checks"] if c["kind"] == "file"],
        "test_commands": [{k: v for k, v in c.items() if k not in {"kind", "expectedExitCode"}} for c in contract["checks"] if c["kind"] == "command"],
    }
    intent = initial_intent or record("intent:" + run_id, 1, {"original_goal": run["goal"], "domain_id": contract["domain_id"], "constraints": []})
    interpretation = record("interpretation:" + run_id, 1, {"goal_summary": run["goal"], "parameters": params,
                           "assumptions": [], "open_questions": [], "origin": "caller", "trust": "untrusted"})
    checks, ids = [], {"file": 0, "command": 0}
    module = domain_identity or {"id": contract["domain_id"], "revision": domain_revision or contract["domain_revision"]}
    sources = provenance(policy_provenance)
    for item in contract["checks"]:
        key = item.get("check_id", item["kind"] + "-" + str(ids[item["kind"]]))
        ids[item["kind"]] += 1
        mandatory = mode == "strict" or key in (required_checks or [])
        configured = acceptance_binding and key in acceptance_binding["definition"]["required_check_ids"]
        checks.append(check_record(run_id, key, 1, copy.deepcopy(item), interpretation["ref"], origin="domain_preparation",
                                   author=acceptance_binding["authority"]["authorship"]["criteria"] if configured else sources["declared_author"],
                                   role="mandatory" if mandatory else "exploratory",
                                   authority=acceptance_binding["authority"] if configured else None,
                                   generator={"kind": "domain", "module": module}))
    require(len({c["check_id"] for c in checks}) == len(checks), "CHECK_ID_AMBIGUOUS", "Duplicate check IDs")
    gate_ids = [item["check_id"] for item in checks] if mode == "strict" else list(required_checks or []) + list(deferred_checks or [])
    for key in gate_ids:
        named(key)
    gate_ids = list(dict.fromkeys(gate_ids))
    missing = set(gate_ids) - {item["check_id"] for item in checks}
    require(projected or missing <= set(deferred_checks or []), "UNKNOWN_GATE_CHECK",
            "Unknown required check IDs: " + ", ".join(sorted(missing)) + "; explicitly declare deferred_checks for future definitions")
    policy_body = {"mode": mode, "required_check_ids": gate_ids,
                    "origin": "initial_configuration", "domain_revision": domain_revision or contract["domain_revision"],
                    "action": "finish_completed", "ready_attestation": False, "provenance": sources,
                    "deferred_check_ids": list(deferred_checks or []),
                    "rule": "all_registered_checks" if mode == "strict" else "explicit_gates"}
    if acceptance_binding:
        policy_body.update(origin="operator_configuration", acceptance_binding_hash=acceptance_binding["binding_hash"],
                           authority=acceptance_binding["authority"], caller_proposal_provenance=sources,
                           provenance={"declared_author": acceptance_binding["authority"]["authorship"]["criteria"],
                                       "author_trust": "configuration_claim",
                                       "approval": {"status": "configured_not_authenticated", "authenticated_by": None,
                                                    "claimed_reference": acceptance_binding["definition"]["approval"]["reference"],
                                                    "configured_approver": acceptance_binding["definition"]["approval"]["declared_by"]}})
    policy = record("policy:" + run_id, 1, policy_body)
    run.update(semantic_schema_version=SEMANTIC_VERSION, intent=intent, policy=policy,
               semantic_projection_origin="legacy_strict_projection" if projected else "native",
               domain_module=module,
               interpretations=[interpretation], check_records=checks, assessments=[], activity=[], logical_tasks=[],
               gate_bindings={item["check_id"]: item["ref"] for item in checks if item["check_id"] in gate_ids},
               domain_questions=questions or [])
    run["domain_preparation"] = {"status": "needs_input" if questions else "proceed",
                                 "available_operations": [] if questions else ["submit", "verify", "finish_completed"]}


def validate_run(run):
    validate(run["intent"])
    validate(run["policy"])
    if run.get("acceptance"):
        from .acceptance import validate as validate_acceptance, compiled_check_ids
        validate_acceptance(run["acceptance"])
        require(run["policy"].get("acceptance_binding_hash") == run["acceptance"]["binding_hash"],
                "ACCEPTANCE_BINDING_CORRUPT", "Completion policy no longer references its acceptance bundle")
        compiled_check_ids({"acceptance_check_ids": run["policy"]["required_check_ids"]}, run["acceptance"]["definition"])
        from .acceptance import validate_coverage_binding
        validate_coverage_binding(run["contract"], run.get("domain_preparation", {}), run["acceptance"]["definition"])
    require(run["intent"]["original_goal"] == run["goal"], "INTENT_CHANGED", "Original goal is immutable")
    require(run["contract"]["original_goal"] == run["goal"] and run["contract"]["domain_id"] == run["intent"]["domain_id"],
            "INTENT_CHANGED", "Compiled contract changed the original intent identity")
    for collection in ("interpretations", "check_records", "assessments", "activity"):
        for item in run[collection]:
            validate(item)
    require([item["ref"]["revision"] for item in run["interpretations"]] == list(range(1, len(run["interpretations"]) + 1)),
            "INTERPRETATION_CORRUPT", "Interpretation revision history is not consecutive")
    for key, ref in run["gate_bindings"].items():
        require(any(item["check_id"] == key and item["ref"] == ref for item in run["check_records"]), "GATE_BINDING_CORRUPT", "Gate references unknown check revision")


def current_checks(run):
    return [item for item in latest_checks(run).values() if item.get("active", True)]


def latest_checks(run):
    return {item["check_id"]: item for item in run["check_records"]}


def next_check_revision(run, key):
    # max also handles historical v0.2 records whose reactivation reset to 1.
    return 1 + max((item["ref"]["revision"] for item in run["check_records"] if item["check_id"] == key), default=0)


def retire_check(run, previous, interpretation_ref, reason):
    require(previous["check_id"] not in run["gate_bindings"], "GATE_POLICY_CHANGED", "Pinned gate checks cannot be retired")
    require(len(run["check_records"]) < 256, "CHECK_LIMIT", "Check revision limit reached")
    body = {k: copy.deepcopy(v) for k, v in previous.items() if k != "ref"}
    body.update(active=False, interpretation_ref=interpretation_ref, retirement_reason=reason)
    result = record(previous["ref"]["id"], next_check_revision(run, previous["check_id"]), body)
    run["check_records"].append(result)
    return result


def domain_check(item):
    return item.get("origin") == "domain_preparation" or item["check_id"].startswith(("file-", "command-", "domain."))


def sync_checks(run, compiled_checks: list, interpretation_ref: dict):
    history = latest_checks(run)
    existing = {key: item for key, item in history.items() if domain_check(item)}
    definitions = {}
    seen = set()
    for item in compiled_checks:
        definition = item.get("check_key", "exact:" + canonical_hash(parameters_only(item)))
        require(definition not in definitions, "CHECK_ID_AMBIGUOUS", "Domain must provide unique stable check keys")
        definitions[definition] = True
        old = next((c for c in existing.values() if c.get("definition_key") == definition), None)
        if old is None:
            # Historical records have no definition key. Only match identical
            # specifications; never infer that a changed list position is identity.
            old = next((c for c in existing.values() if not c.get("definition_key") and c["check_id"] not in seen and check_equivalent(c, item)), None)
        key = old["check_id"] if old else item.get("check_id")
        if key is None:
            number = 0
            while item["kind"] + "-" + str(number) in history or item["kind"] + "-" + str(number) in seen:
                number += 1
            key = item["kind"] + "-" + str(number)
        require(key not in seen and (key not in history or old is not None), "CHECK_ID_AMBIGUOUS", "Domain check ID collision")
        seen.add(key)
        if old and old.get("active", True) and check_equivalent(old, item):
            continue
        require(key not in run["gate_bindings"], "GATE_POLICY_CHANGED", "Pinned gate check cannot be weakened or changed by interpretation revision")
        require(len(run["check_records"]) < 256, "CHECK_LIMIT", "Check revision limit reached")
        created = check_record(run["run_id"], key, next_check_revision(run, key), {**item, "check_key": definition}, interpretation_ref,
                               origin="domain_preparation", author=run["policy"].get("caller_proposal_provenance", run["policy"].get("provenance", {})).get("declared_author", "unknown"),
                               role="mandatory" if key in run["policy"]["required_check_ids"] or run["policy"]["rule"] == "all_registered_checks" else "exploratory",
                               generator={"kind": "domain", "module": run["domain_module"]})
        run["check_records"].append(created)
        if key in run["policy"]["required_check_ids"] or run["policy"]["rule"] == "all_registered_checks":
            run["gate_bindings"][key] = created["ref"]
    require(set(run["gate_bindings"]) <= seen | {key for key, c in history.items() if not domain_check(c)},
            "GATE_POLICY_CHANGED", "Revision removed a required gate check")
    for key, previous in existing.items():
        if previous.get("active", True) and key not in seen:
            retire_check(run, previous, interpretation_ref, "Removed by Domain preparation")


def gates(run, measurements):
    candidate = run.get("candidate")
    checks = {item["check_id"]: item for item in current_checks(run)}
    keys = set(run["policy"]["required_check_ids"]) | set(run["gate_bindings"])
    results = []
    for key in sorted(keys):
        ref = run["gate_bindings"].get(key)
        state, observed_id = "not_defined" if ref is None else "not_measured", None
        if ref and key in checks and checks[key]["ref"] != ref:
            state = "binding_mismatch"
        elif ref and candidate:
            matching = [item for item in measurements if item["check_ref"] == ref and item["subject"] == candidate["subject"]]
            if matching:
                latest = matching[-1]
                state, observed_id = latest["comparison_status"], latest["observation_id"]
        results.append({"check_id": key, "check_ref": ref, "status": state, "observation_id": observed_id,
                        "definition_status": "defined" if ref else "deferred" if key in run["policy"].get("deferred_check_ids", []) else "legacy_unspecified",
                        "action": run["policy"].get("action", "finish_completed"),
                        "origin": run["policy"]["origin"], "policy_ref": run["policy"]["ref"]})
    return {"status": "not_required" if not results else "passed" if all(r["status"] == "passed" for r in results) else "unsatisfied", "results": results}


def comparison_status(report):
    result = report["result"]
    if result["execution"] != "completed":
        return "incomplete"
    findings = [item for item in result.get("findings", []) if item.get("kind") == "comparison"]
    return "passed" if findings and all(item.get("result") == "pass" for item in findings) else "failed"


def lifecycle(run):
    if run["status"] == "finished":
        return "closed"
    if run["status"] != "active":
        return run["status"]
    if run["active_job"]:
        return "verifying"
    return "waiting_input" if run["domain_questions"] else "active"


def assessment_view(run):
    interpretation = run["interpretations"][-1]["ref"]
    subject = run["candidate"]["subject"] if run.get("candidate") else None
    for item in reversed(run["assessments"]):
        if item["interpretation_ref"] == interpretation and item["subject"] == subject:
            return item
    return {"status": "not_assessed", "origin": "caller", "trust": "untrusted", "summary": "", "uncertainties": [], "cited_observation_ids": []}


def check_set_hash(checks):
    return canonical_hash({"schema_version": "check-set-v1", "checks": [{"ref": c["ref"], "spec": c["spec"]} for c in checks]})


def resolution(run, gate_results):
    measurement = run["verification"]["status"]
    closed = run["status"] == "finished"
    label = "closed_unverified" if measurement in {"not_run", "queued", "running", "cancelled", "interrupted", "error"} else "closed_checks_" + measurement
    from .acceptance import view as acceptance_view
    from .evidence import unmeasured
    result = {"lifecycle": lifecycle(run), "display_status": label if closed else lifecycle(run),
            "requested_outcome": run["record"]["outcome"] if run.get("record") else None,
            "measurement_status": measurement, "assessment_status": assessment_view(run)["status"],
            "gate_status": gate_results["status"], "policy_approval": run["policy"].get("provenance", {}).get("approval", {"status": "unknown"}),
            "certification": "not_issued", "acceptance": acceptance_view(run.get("acceptance")),
            "measurement_scope": run["verification"].get("measurement_scope", unmeasured())}
    preparation = run.get("domain_preparation") or {}
    inventory = preparation.get("coverage_inventory") if isinstance(preparation, dict) else None
    if inventory is not None:
        # Coverage is a Domain declaration shown to callers, never a gate or
        # verifier observation.
        result["coverage_inventory"] = copy.deepcopy(inventory)
    return result
