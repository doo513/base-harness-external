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
        result.update(test_case_count=sum(c["executed_count"] for c in cases), test_case_scope="observed_case_runs_in_case_aware_checks",
                      discovered_case_count=sum(c["discovered_count"] for c in cases), selected_case_count=sum(c["selected_count"] for c in cases),
                      test_case_count_complete=len(cases) == kinds["command"] and all(c["collection_complete"] and c["session_complete"] for c in cases))
    return result


def unmeasured():
    return scope([])


def compare(items, job, run):
    """Paired check outcomes, not a score or instruction to the Host."""
    if not job.get("baseline"):
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
