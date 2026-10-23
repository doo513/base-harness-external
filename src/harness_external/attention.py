"""Bounded evidence presentation. No Domain inference, writes or next-action choice."""
import copy

from harness.common import canonical_hash as digest

ATTENTION_OUTCOMES = ["failed", "error", "skipped", "xfail", "xpass", "not_selected", "not_run", "incomplete"]


def attention_report(current, job, cases, *, limit=20):
    """Project observed exceptions, never infer root causes or goal satisfaction."""
    result = (job or {}).get("result") or {}
    linked = {}
    requirements = (result.get("requirement_observations") or {}).get("requirements", [])
    for requirement in requirements:
        for case in requirement["scenarios"]:
            linked.setdefault((case["check_id"], case["case_id"]), []).append(requirement["requirement_id"])
    declared_scope = current.get("verification_scope") or {}
    items = [{"kind": "verification_gap", **copy.deepcopy(gap)} for gap in declared_scope.get("gaps", [])]
    for record in cases.get("items", []):
        # Baseline records remain accessible but cannot become Candidate issues.
        if record.get("subject_role") != "candidate":
            continue
        case = record["case"]
        errors = []
        for index, phase in enumerate(case["phases"][:3]):
            if isinstance(phase, dict) and isinstance(phase.get("message"), str) and phase["message"]:
                message = phase["message"]
                errors.append({"phase": phase.get("phase"), "outcome": phase.get("outcome"), "excerpt": message[:600],
                    "truncated": len(message) > 600, "detail": {"kind": "cases", "detail": {
                        "record_id": record["observation_id"], "record_digest": digest(record),
                        "path": ["case", "phases", index, "message"], "sha256": digest(message)}}})
            elif phase:
                errors.append({"phase": phase.get("phase") if isinstance(phase, dict) else index,
                    "detail": {"kind": "cases", "detail": {"record_id": record["observation_id"], "record_digest": digest(record),
                        "path": ["case", "phases", index], "sha256": digest(phase)}}})
        items.append({"kind": "case", "status": case["outcome"], "check_id": record["check_id"],
                      "observation_boundary": record.get("observation_boundary", "unknown"), "trust": record.get("trust", "unknown"),
                      "case_id": case["case_id"], "requirement_ids": linked.get((record["check_id"], case["case_id"]), []),
                      "observation_id": record["observation_id"], "executed": case["executed"],
                      "finished": case["finished"], "errors": errors,
                      "omitted_phase_count": max(0, len(case["phases"]) - 3),
                      **({"detail": {"kind": "cases", "detail": {"record_id": record["observation_id"],
                          "record_digest": digest(record), "path": [], "sha256": digest(record)}}}
                         if len(case["phases"]) > 3 else {})})
    seen = {(item.get("check_id"), item.get("case_id")) for item in items}
    for requirement in requirements:
        for case in requirement["scenarios"]:
            identity = (case["check_id"], case["case_id"])
            if case["status"] != "passed" and identity not in seen:
                seen.add(identity)
                items.append({"kind": "declared_scenario", "status": case["status"], "check_id": case["check_id"],
                              "case_id": case["case_id"], "requirement_ids": linked[identity],
                              "executed": case["executed"], "observation_id": case["observation_id"]})
    scope = result.get("measurement_scope") or {}
    for check in scope.get("observations", []):
        if check.get("comparison_status") != "passed":
            items.append({"kind": "check", **{key: check.get(key) for key in
                ("check_id", "comparison_status", "execution_status", "observation_id")},
                "requirement_ids": [r["requirement_id"] for r in requirements if check["check_id"] in r.get("failed_check_ids", [])],
                "case_summary": {key: value for key, value in (check.get("case_summary") or {}).items()
                                 if key in {"framework", "outcomes", "executed_count", "selected_count", "session_complete", "observation_boundary"}}})
    if job and job.get("error"):
        items.append({"kind": "job_error", "error": job["error"]})
    returned = items[:limit]
    shown_cases = sum(item["kind"] == "case" for item in returned)
    case_next = shown_cases if shown_cases < cases.get("total", 0) else None
    complete = (bool(job and job["status"] not in {"queued", "running"}) and cases.get("next_offset") is None
                and len(returned) == len(items) and declared_scope.get("omitted_gap_count", 0) == 0)
    payload = {"api_version": current["api_version"], "ok": True, "schema_version": "harness-attention-v1", "view": "attention",
        "assurance": current["assurance"], "ready": current["ready"],
        "run_id": current.get("run_id") or current.get("run", {}).get("run_id"),
        **{key: current[key] for key in ("run", "intent_ref", "policy_ref", "candidate", "gates", "assessment", "limits", "usage", "details", "operation_constraints") if key in current},
        "measurement": current.get("measurement"),
        "workspace_constraints": current.get("workspace_constraints", {"status": "not_queried"}),
        "diagnostic": current.get("diagnostic", {"status": "not_queried"}),
        "verification_scope": {key: value for key, value in declared_scope.items() if key != "gaps"},
        "attention": returned, "attention_page_complete": complete, "listed_attention_count": len(returned),
        "total_known_attention_count": len(items), "omitted_attention_count": len(items) - len(returned),
        "case_query": {"job_id": (job or {}).get("job_id"), "outcomes": ATTENTION_OUTCOMES,
                       "subject_role": "candidate", "total": cases.get("total", 0), "next_offset": case_next,
                       "operation": "records", "kind": "cases", "view": "references"},
        "selected_job": {key: job.get(key) for key in ("job_id", "status", "candidate_hash", "check_set_hash", "completed_checks", "total_checks")} if job else None,
        "requirement_count": len(requirements),
        "framework_reported_requirement_ids": [item["requirement_id"] for item in requirements if item.get("status") == "framework_reported_only"],
        "known_gaps": [{"requirement_id": item["requirement_id"], "known_gap_ids": item["known_gap_ids"]} for item in requirements if item["known_gap_ids"]]}
    if job:
        payload["selected_job"]["purpose"] = job.get("purpose", "verification")
        if result.get("diagnostic_comparison") is not None:
            payload["selected_job"]["diagnostic_comparison"] = result["diagnostic_comparison"]
        payload["selected_job"]["result_status"] = result.get("status")
        measured = current.get("measurement")
        payload["selected_job"]["is_current_verification"] = measured.get("job_id") == job["job_id"] if measured is not None else None
        whole = digest(job)
        payload["job_detail"] = {"kind": "jobs", "detail": {"record_id": job["job_id"], "record_digest": whole, "path": [], "sha256": whole}}
        payload["selected_job"]["measurement_scope"] = {key: value for key, value in scope.items() if key != "observations"}
    if "wait_status" in current:
        payload["wait_status"] = current["wait_status"]
    if "current_workspace_matches_submitted_files" in current:
        payload["current_workspace_matches_submitted_files"] = current["current_workspace_matches_submitted_files"]
    payload["not_queried"] = [key for key in ("gates", "assessment", "limits", "usage") if key not in current]
    if "run" in payload:
        payload["run"] = {key: value for key, value in payload["run"].items() if key != "goal"}
    if payload["measurement"] is not None:
        measurement = payload["measurement"]
        payload["measurement"] = {key: value for key, value in measurement.items() if key not in {"measurement_scope", "baseline_comparison"}}
        payload["measurement"]["measurement_scope"] = {key: value for key, value in measurement.get("measurement_scope", {}).items() if key != "observations"}
    payload["context_operation"] = {"operation": "context", "arguments": {}}
    return payload
