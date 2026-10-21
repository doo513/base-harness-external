"""Describe observed scope, never infer goal correctness from a passing process."""
from collections import Counter


def execution_status(report, sandbox=None):
    result = report["result"]
    if result["execution"] == "completed":
        return "completed"
    reason = str((sandbox or {}).get("reason", ""))
    code = result.get("error", {}).get("code", "")
    if "TIMEOUT" in code or "TIMEOUT" in reason or "timeoutMs" in reason:
        return "timed_out"
    if reason.startswith("SANDBOX_LIMIT_EXCEEDED"):
        return "error"
    if result["execution"] == "not_run":
        return "unavailable" if reason.startswith(("SANDBOX_ADAPTER_UNAVAILABLE", "SANDBOX_SETUP_FAILED", "SANDBOX_UNAVAILABLE", "ADAPTER_UNAVAILABLE")) else "not_run"
    return "error"


def scope(items):
    kinds = Counter(item["kind"] for item in items)
    statuses = Counter(item["execution_status"] for item in items)
    completed_commands = sum(item["kind"] == "command" and item["execution_status"] == "completed" for item in items)
    result = {"file_checks": kinds["file"], "command_checks": kinds["command"],
            "completed_commands": completed_commands, "execution_statuses": dict(statuses),
            "test_case_count": None, "test_case_scope": "not_collected_by_generic_command_exit_measurement",
            "goal_satisfaction": "not_measured", "check_validity": "not_independently_established",
            "observations": items}
    cases = [item["case_summary"] for item in items if item.get("case_summary")]
    if cases:
        external = [c for c in cases if c.get("observation_boundary") == "external_capture"]
        reported = [c for c in cases if c.get("observation_boundary") != "external_capture"]
        result.update(test_case_count=sum(c["executed_count"] for c in external) if external else None,
                      test_case_scope="externally_observed_predicates" if external else "framework_reported_not_independently_observed",
                      reported_test_case_count=sum(c["executed_count"] for c in reported),
                      discovered_case_count=sum(c["discovered_count"] for c in cases), selected_case_count=sum(c["selected_count"] for c in cases),
                      test_case_count_complete=len(external) == kinds["command"] and all(c["collection_complete"] and c["session_complete"] for c in external))
    return result


def unmeasured():
    return scope([])


def compare(items, job, run):
    """Paired check outcomes, not a score or instruction to the Host."""
    if job.get("purpose") == "diagnostic" or not job.get("baseline"):
        return {"status": "not_requested", "changes": []}
    by_key = {(item["subject_role"], item["check_id"]): item for item in items}
    same_input_scope = ({f["path"] for f in job["baseline"]["manifest"]["files"]}
                        == {f["path"] for f in job["candidate"]["manifest"]["files"]})
    changes = []
    for check in job["checks"]:
        key = check["check_id"]
        before, after = by_key.get(("baseline", key)), by_key.get(("candidate", key))
        is_file = check["spec"]["parameters"]["kind"] == "file"
        pinned = is_file or bool(run.get("acceptance") and key in run["acceptance"]["definition"]["required_check_ids"])
        reasons = []
        if not before or not after:
            reasons.append("measurement_side_missing")
        if not pinned:
            reasons.append("test_bundle_not_pinned")
        if not is_file and not same_input_scope:
            reasons.append("input_scope_changed")
        if before and after:
            if before["environment_hash"] != after["environment_hash"]:
                reasons.append("recorded_environment_changed")
            if before["execution_status"] != "completed" or after["execution_status"] != "completed":
                reasons.append("execution_not_completed")
            prior_cases, current_cases = before.get("case_summary"), after.get("case_summary")
            if prior_cases and current_cases and prior_cases.get("case_scope_hash") != current_cases.get("case_scope_hash"):
                reasons.append("observed_case_scope_changed")
        comparable = not reasons
        state = "inconclusive"
        if comparable:
            pair = before["comparison_status"], after["comparison_status"]
            state = {("failed", "passed"): "improved", ("passed", "passed"): "already_passing",
                     ("passed", "failed"): "regressed", ("failed", "failed"): "still_failing"}.get(pair, "inconclusive")
        changes.append({"check_id": key, "check_ref": check["ref"], "status": state, "comparable": comparable,
                        "incomparable_reasons": reasons,
                        "baseline_observation_id": before["observation_id"] if before else None,
                        "candidate_observation_id": after["observation_id"] if after else None,
                        "comparison_basis": "identical_file_predicate" if check["spec"]["parameters"]["kind"] == "file" else
                                            "configured_command_and_pinned_declared_test_bundle" if pinned else "test_bundle_not_pinned"})
    return {"status": "compared" if all(x["comparable"] for x in changes) else "incomplete",
            "baseline_hash": job["baseline"]["candidate_hash"], "candidate_hash": job["candidate_hash"],
            "check_set_hash": job["check_set_hash"], "acceptance_binding_hash": job.get("acceptance_binding_hash"),
            "same_input_scope": same_input_scope,
            "changes": changes, "meaning": "check_improvement_not_full_goal_proof",
            "limitations": ["Environment identity covers the recorded runtime, not every transitive dependency or nondeterministic input.",
                            "Pinned declared tests are not proof of independent authorship or sufficient goal coverage."]}


def diagnostic_pair(items, job):
    """Generic paired facts, not a judgment about tests, regressions or goals."""
    if job.get("purpose") != "diagnostic":
        return None
    from .paired_inputs import validate
    validate(job)
    indexed = {(item["subject_role"], item["check_id"]): item for item in items}
    changes = []
    same_scope = {f["path"] for f in job["baseline"]["manifest"]["files"]} == {f["path"] for f in job["candidate"]["manifest"]["files"]}
    for check in job["checks"]:
        before, after = indexed.get(("baseline", check["check_id"])), indexed.get(("candidate", check["check_id"]))
        reasons = [] if same_scope else ["input_scope_changed"]
        if not before or not after:
            reasons.append("measurement_side_missing")
        else:
            if before["environment_hash"] != after["environment_hash"]:
                reasons.append("recorded_environment_changed")
            if before["execution_status"] != "completed" or after["execution_status"] != "completed":
                reasons.append("execution_not_completed")
            for item in (before, after):
                summary = item.get("case_summary")
                if summary and not (summary.get("collection_complete") and summary.get("session_complete")):
                    reasons.append("case_observation_incomplete")
            if before.get("case_summary") and after.get("case_summary") and before["case_summary"].get("case_scope_hash") != after["case_summary"].get("case_scope_hash"):
                reasons.append("observed_case_scope_changed")
        status = "inconclusive"
        if not reasons:
            status = {("failed", "passed"): "before_failed_after_passed", ("passed", "passed"): "both_passed",
                      ("passed", "failed"): "before_passed_after_failed", ("failed", "failed"): "both_failed"}.get(
                          (before["comparison_status"], after["comparison_status"]), "inconclusive")
        changes.append({"check_id": check["check_id"], "check_ref": check["ref"], "status": status,
                        "incomparable_reasons": list(dict.fromkeys(reasons)),
                        "before_observation_id": before["observation_id"] if before else None,
                        "after_observation_id": after["observation_id"] if after else None})
    binding = job["comparison_binding"]
    return {"status": "compared" if all(c["status"] != "inconclusive" for c in changes) else "incomplete",
            "binding_hash": binding["binding_hash"], "reference_hash": binding["reference"]["candidate_hash"],
            "current_hash": binding["current"]["candidate_hash"], "shared_input_hash": binding["shared_input_hash"],
            "changes": changes, "gate_eligible": False, "meaning": "paired_observations_not_defect_or_goal_proof",
            "limitations": ["Caller-selected shared inputs are not independently authored criteria.",
                            "A failing command need not have reached the intended fault or assertion."]}
