"""Small read projection of existing observations; no new gate or model verdict."""
import copy
import json


def _size(value):
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))


def brief(attention, *, maximum=6144):
    source = attention
    gate = source.get("gates", {})
    scope = source.get("verification_scope", {})
    selected = source.get("selected_job")
    result = {"api_version": source["api_version"], "ok": source.get("ok", True),
              "schema_version": "harness-checkpoint-v1", "view": "brief",
              "assurance": source["assurance"], "ready": source["ready"], "run_id": source["run_id"],
              "run": copy.deepcopy(source.get("run")), "candidate": copy.deepcopy(source.get("candidate")),
              "selected_job": {k: selected.get(k) for k in ("job_id", "status", "result_status", "purpose", "relevance",
                                  "candidate_hash", "contract_hash", "check_set_hash", "completed_checks", "total_checks")} if selected else None,
              "gates": {"status": gate.get("status", "not_evaluated"), "required_check_count": gate.get("required_check_count", 0),
                        "unsatisfied_check_count": len(gate.get("unsatisfied_check_ids", [])),
                        "unsatisfied_check_ids": gate.get("unsatisfied_check_ids", [])[:8]},
              "measurement": {k: copy.deepcopy(v) for k, v in (source.get("measurement") or {}).items()
                              if k in {"status", "job_id", "result_hash", "measurement_scope"}},
              "assessment": copy.deepcopy(source.get("assessment")),
              "verification_scope": {k: copy.deepcopy(v) for k, v in scope.items()
                                     if k in {"domain_plan_status", "gap_count", "omitted_gap_count", "declared_requirement_count",
                                              "declared_scenario_count", "coverage_conclusion", "interpretation_ref"}},
              "budget_remaining": copy.deepcopy(source.get("budget_remaining")),
              "issues": [], "known_gaps": [], "framework_reported_requirement_ids": source.get("framework_reported_requirement_ids", [])[:8],
              "omitted": {"issues": source.get("total_known_attention_count", 0),
                          "known_gaps": len(source.get("known_gaps", [])),
                          "unsatisfied_check_ids": max(0, len(gate.get("unsatisfied_check_ids", [])) - 8),
                          "framework_reported_requirements": max(0, len(source.get("framework_reported_requirement_ids", [])) - 8)},
              "details": {"context": {"operation": "context", "run_id": source["run_id"]},
                          "attention": {"operation": "status", "arguments": {"run_id": source["run_id"], "view": "attention",
                                                **({"job_id": selected["job_id"]} if selected else {})}},
                          "job": copy.deepcopy(source.get("job_detail"))},
              "not_queried": ["current_workspace_freshness", "complete_context"],
              "meaning": "measured_facts_and_declared_scope_not_goal_proof_or_rework_instruction"}
    for item in source.get("attention", []):
        value = copy.deepcopy(item)
        if "requirement_ids" in value:
            value["omitted_requirement_ids"] = max(0, len(value["requirement_ids"]) - 8)
            value["requirement_ids"] = value["requirement_ids"][:8]
        if "errors" in value:
            value["omitted_errors"] = max(0, len(value["errors"]) - 1)
            value["errors"] = value["errors"][:1]
            for error in value["errors"]:
                if len(error.get("excerpt", "")) > 400:
                    error["excerpt"] = error["excerpt"][:400]
                    error["truncated"] = True
        result["issues"].append(value)
        if _size(result) > maximum:
            result["issues"].pop()
            break
        result["omitted"]["issues"] -= 1
    for item in source.get("known_gaps", []):
        result["known_gaps"].append(copy.deepcopy(item))
        if _size(result) > maximum:
            result["known_gaps"].pop()
            break
        result["omitted"]["known_gaps"] -= 1
    result["issue_page_complete"] = bool(source.get("attention_page_complete")) and result["omitted"]["issues"] == 0
    return result
