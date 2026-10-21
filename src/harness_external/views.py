"""Presentation only. No admission, evidence validation or state writes."""
from harness.common import canonical_hash
from . import semantics


def verification_scope(run, gates=None):
    """Bounded projection of Domain declarations and independently selected gates."""
    plan = run["domain_preparation"].get("verification_plan")
    gaps = (plan or {}).get("diagnostics", [])
    count = (plan or {}).get("diagnostic_count", 0)
    return {"meaning": "current_run_declarations_not_execution_or_goal_proof",
            "interpretation_ref": run["interpretations"][-1]["ref"],
            "domain_plan_status": plan.get("status") if plan else "not_reported",
            "declared_requirement_count": plan.get("requirement_count") if plan else None,
            "declared_scenario_count": plan.get("scenario_count") if plan else None,
            "referenced_requirement_count": plan.get("referenced_requirement_count") if plan else None,
            "gate_status": gates["status"] if gates is not None else ("not_evaluated" if run["gate_bindings"] else "not_required"),
            "required_check_count": len(run["gate_bindings"]),
            "plan_hash": canonical_hash(plan) if plan else None,
            "details": {"operation": "context", "run_id": run["run_id"], "field": "domain_preparation.verification_plan"},
            "gap_count": count, "gaps": gaps[:8], "omitted_gap_count": max(0, count - 8),
            "coverage_conclusion": "not_established"}


def operation_constraints(run, gates):
    """Cheap read-only reasons, not permission or a successful admission promise.

    File integrity, runtime availability and concurrent revision checks still
    occur when the chosen operation is invoked. No action is selected here.
    """
    common = ["RUN_CLOSED"] if run["status"] == "finished" else ["RUN_NOT_ACTIVE"] if run["status"] != "active" else []
    if run["active_job"]:
        common = [*common, "VERIFICATION_ACTIVE"]
    operations = {}
    available = run["domain_preparation"]["available_operations"]
    for operation in ("submit", "verify", "finish_completed"):
        reasons = list(common)
        if operation not in available:
            reasons.append("NEEDS_INPUT")
        if operation in {"submit", "verify"} and run["actions"] >= run["limits"]["max_actions"]:
            reasons.append("ACTION_BUDGET_EXHAUSTED")
        if operation == "verify":
            if run["candidate"] is None:
                reasons.append("SUBMIT_REQUIRED")
            if run["verification_attempts"] >= run["limits"]["max_verifications"]:
                reasons.append("VERIFICATION_BUDGET_EXHAUSTED")
            if not semantics.current_checks(run):
                reasons.append("CHECKS_REQUIRED")
        if operation == "finish_completed":
            if gates["status"] not in {"passed", "not_required"} or (
                    run["policy"]["mode"] == "strict" and run["verification"]["status"] != "passed"):
                reasons.append("VERIFICATION_REQUIRED")
            if run["contract"]["rules"].get("snapshot_required", False) and run["candidate"] is None:
                reasons.append("SUBMIT_REQUIRED")
        operations[operation] = {"blocked_by": list(dict.fromkeys(reasons))}
    return {"meaning": "known_preconditions_not_authorization", "operations": operations}


def job_view(job, *, compact=False):
    value = {"purpose": job.get("purpose", "verification"), **{key: job.get(key) for key in ("job_id", "run_id", "status", "cleanup", "heartbeat_at",
            "deadline_at", "completed_checks", "total_checks", "observed_cases", "completed_cases", "error", "check_set_hash", "candidate_hash", "timings")},
            "result": {key: job["result"].get(key) for key in ("status", "result_hash", "check_set_hash", "measurement_scope", "baseline_comparison", "requirement_observations", "diagnostic_comparison")} if job.get("result") else None}
    if compact and value["result"]:
        result = value["result"]
        scope = result.get("measurement_scope") or {}
        result["measurement_scope"] = {key: body for key, body in scope.items() if key != "observations"}
        result["measurement_scope"]["observation_count"] = len(scope.get("observations", []))
        comparison = result.get("baseline_comparison") or {}
        result["baseline_comparison"] = {key: body for key, body in comparison.items() if key not in {"changes", "limitations"}}
        result["baseline_comparison"]["change_count"] = len(comparison.get("changes", []))
        links = result.pop("requirement_observations", None)
        result["requirement_count"] = len(links["requirements"]) if links else 0
        digest = canonical_hash(job_view(job))
        value["detail_source"] = {"kind": "jobs", "record_id": job["job_id"],
                                  "record_digest": digest, "path": [], "sha256": digest}
    return value


def diagnostic_view(run):
    return {key: value for key, value in run.get("diagnostic", {"status": "not_requested"}).items()
            if key in {"status", "job_id", "result_hash", "diagnostic_comparison"}}


def run_view(run):
    return {"run_id": run["run_id"], "domain_id": run["contract"]["domain_id"], "goal": run["goal"],
            "workspace": run["workspace"], "status": run["status"], "phase": run["phase"],
            "lifecycle": semantics.lifecycle(run), "created_at": run["created_at"], "deadline_at": run["deadline_at"],
            "interpretation_ref": run["interpretations"][-1]["ref"], "active_job": run["active_job"],
            "termination_reason": run.get("termination_reason")}


def compact_resume(run, jobs, counts, gates, snapshot):
    """Polling projection; context restores all omitted text/history losslessly."""
    state = run_view(run)
    state.pop("goal")
    assessment = semantics.assessment_view(run)
    verification = run["verification"]
    scope = verification.get("measurement_scope", {})
    measurement = {key: body for key, body in verification.items() if key not in {"measurement_scope", "baseline_comparison"}}
    measurement["measurement_scope"] = {key: body for key, body in scope.items() if key != "observations"}
    measurement["measurement_scope"]["observation_count"] = len(scope.get("observations", []))
    return {"view": "compact", "run": state, "intent_ref": run["intent"]["ref"], "policy_ref": run["policy"]["ref"],
            "verification_scope": verification_scope(run, gates),
            "operation_constraints": operation_constraints(run, gates),
            "interpretation_ref": run["interpretations"][-1]["ref"],
            "usage": {"actions": run["actions"], "verification_attempts": run["verification_attempts"]},
            "limits": run["limits"], "candidate": {key: run["candidate"][key] for key in ("candidate_id", "candidate_hash", "subject")} if run["candidate"] else None,
            "measurement": measurement,
            "assessment": {"status": assessment["status"], "uncertainty_count": len(assessment["uncertainties"])},
            "open_question_count": len(run["interpretations"][-1]["open_questions"]) + len(run["domain_questions"]),
            "unresolved_work": [item["task_id"] for item in run["logical_tasks"] if item["state"] != "settled"],
            "gates": {"status": gates["status"], "required_check_count": len(gates["results"]),
                      "unsatisfied_check_ids": [item["check_id"] for item in gates["results"] if item["status"] != "passed"]},
            "recent_jobs": [job_view(job, compact=True) for job in jobs], "history_counts": counts,
            "details": {"operation": "context", "run_id": run["run_id"], "snapshot": snapshot,
                        "field_refs": {key: run.head["fields"][key] for key in
                                       ("intent", "policy", "domain_preparation", "domain_questions", "contract", "verification", "record")}
                        if snapshot else None,
                        "meaning": "read_projection_not_a_delivered_context_cursor"}}
