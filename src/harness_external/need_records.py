"""Generic advisory records; Domain vocabularies and sufficiency stay outside Core."""
import copy

from harness.common import canonical_bytes, canonical_hash, redact
from . import semantics
from .errors import fields, require


STATES = ("open", "addressed", "deferred")


def record(api, run, observation):
    proposal = fields(observation["need"], {"id", "expected_revision", "state", "details", "conclusion", "check_ids", "activity_ids"},
                      {"id", "expected_revision", "details"})
    need_id = semantics.named(proposal["id"])
    current = run.setdefault("needs", [])
    previous = next((item for item in current if item["need_id"] == need_id), None)
    revision = proposal["expected_revision"]
    require(type(revision) is int and revision == (previous["ref"]["revision"] if previous else 0),
            "STALE_NEED", "Need revision changed; read the current record before updating")
    require(previous is not None or len(current) < 100, "NEED_LIMIT", "At most 100 Needs per Run")
    state = proposal.get("state", "open")
    require(isinstance(state, str) and state in STATES, "NEED_STATE", "Needs are open, addressed or deferred caller claims")
    conclusion = proposal.get("conclusion", "")
    require(isinstance(conclusion, str) and len(conclusion) <= 4000 and (state == "open" or bool(conclusion.strip())),
            "NEED_CONCLUSION", "Addressed/deferred Needs require a bounded explanation, not proof")
    details = proposal["details"]
    require(isinstance(details, dict), "NEED_DETAILS", "Domain Need details must be an object")
    module = api._module(run)
    normalize = getattr(module, "normalize_need", None)
    require(callable(normalize), "NEED_UNSUPPORTED", "This Domain does not provide Need analysis")
    details = normalize(copy.deepcopy(details), copy.deepcopy(run["contract"]))
    try:
        valid = isinstance(details, dict) and len(canonical_bytes(details)) <= 16000
    except (TypeError, ValueError, RecursionError):
        valid = False
    require(valid, "DOMAIN_NEED_RESULT", "Domain returned invalid or oversized Need details")
    check_ids = proposal.get("check_ids", [])
    checks = {item["check_id"]: item["ref"] for item in semantics.current_checks(run)}
    require(isinstance(check_ids, list) and len(check_ids) <= 32 and all(isinstance(key, str) for key in check_ids)
            and len(set(check_ids)) == len(check_ids) and set(check_ids) <= checks.keys(),
            "NEED_REFERENCE", "Need checks must belong to the current Run/check set")
    activity_ids = proposal.get("activity_ids", [])
    api._activities_exist(run, activity_ids)
    require(len(set(activity_ids)) == len(activity_ids), "NEED_REFERENCE", "Duplicate activity references")
    activities = {item["ref"]["id"]: item["ref"] for item in run["activity"]}
    # These IDs are checked by observe against canonical verifier observations.
    # Source locators remain caller claims and are never opened here.
    value = semantics.record("need:" + run["run_id"] + ":" + need_id, revision + 1,
                             {"need_id": need_id, "state": state, "details": redact(details), "conclusion": redact(conclusion),
                              "interpretation_ref": copy.deepcopy(run["interpretations"][-1]["ref"]),
                              "subject": copy.deepcopy(run["candidate"]["subject"]) if run.get("candidate") else None,
                              "references": redact(observation.get("references", [])),
                              "observation_ids": list(observation.get("observation_ids", [])),
                              "check_refs": [copy.deepcopy(checks[key]) for key in check_ids],
                              "activity_refs": [copy.deepcopy(activities[key]) for key in activity_ids],
                              "guidance_hash": canonical_hash(run["domain_preparation"]["analysis_guidance"])
                                  if "analysis_guidance" in run.get("domain_preparation", {}) else None,
                              "origin": "caller", "trust": "untrusted"})
    if previous is None:
        current.append(value)
    else:
        current[current.index(previous)] = value
    return value


def summary(run):
    needs = run.get("needs", [])
    interpretation = run["interpretations"][-1]["ref"]
    subject = run["candidate"]["subject"] if run.get("candidate") else None
    return {"total": len(needs), "counts": {state: sum(item["state"] == state for item in needs) for state in STATES},
            "open_ids": [item["need_id"] for item in needs if item["state"] == "open"],
            "deferred_ids": [item["need_id"] for item in needs if item["state"] == "deferred"],
            "context_changed_ids": [item["need_id"] for item in needs
                                    if item["interpretation_ref"] != interpretation or item["subject"] != subject],
            "meaning": "caller_analysis_not_gate_or_measurement"}
