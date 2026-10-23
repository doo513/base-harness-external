"""Small recovery views and bounded history pages for external callers."""
from pathlib import Path
import time
from harness.common import canonical_hash
from . import semantics, need_records
from . import read_core
from .read_core import COLLECTIONS, RECENT_JOB_LIMIT, page_bounds, records
from .views import job_view, run_view, operation_constraints, verification_scope, diagnostic_view
from .errors import integer, require
from .workspace_constraints import view as workspace_constraints_view


def job_summary(api, run_id, job_id, *, compact=False):
    with api._read_run(run_id) as (connection, run):
        job = read_core.checked_job(api, connection, run_id, job_id)
        return {"run_id": run_id, "view": "compact" if compact else "summary", "job": job_view(job, compact=compact),
                "verification_scope": verification_scope(run),
                "measurement_count": connection.execute("SELECT count(*) FROM measurements WHERE run_id=? AND job_id=?", (run_id, job_id)).fetchone()[0]}


def list_runs(api, *, offset=0, limit=20, allowed_workspace=None):
    offset, limit = page_bounds(offset, limit)
    if allowed_workspace is not None:
        with api.store.transaction(write=False) as connection:
            all_ids = [row[0] for row in connection.execute(
                "SELECT run_id FROM (SELECT run_id,0 AS f,rowid AS n FROM runs UNION ALL SELECT run_id,1 AS f,rowid AS n FROM run_heads) ORDER BY f,n")]
        all_items = []
        for key in all_ids:
            with api._read_run(key) as (_, run):
                if allowed_workspace(Path(run["workspace"])):
                    all_items.append(run_view(run))
        page = all_items[offset:offset + limit]
        return {"items": page, "total": len(all_items),
                "next_offset": offset + len(page) if offset + len(page) < len(all_items) else None}
    with api.store.transaction(write=False) as connection:
        ids = [row[0] for row in connection.execute("SELECT run_id FROM (SELECT run_id,0 AS f,rowid AS n FROM runs UNION ALL SELECT run_id,1 AS f,rowid AS n FROM run_heads) ORDER BY f,n LIMIT ? OFFSET ?", (limit, offset))]
    with api.store.transaction(write=False) as connection:
        total = connection.execute("SELECT (SELECT count(*) FROM runs)+(SELECT count(*) FROM run_heads)").fetchone()[0]
    items = []
    for key in ids:
        with api._read_run(key) as (_, run):
            items.append(run_view(run))
    return {"items": items, "total": total, "next_offset": offset + len(items) if offset + len(items) < total else None}


def attention(api, run_id, *, job_id=None, limit=20, analysis_selection=None):
    """Join only the selected execution and its exceptions on one read snapshot.

    References are validated before filtering. A diagnostic or historical Job
    may be inspected explicitly but cannot replace current verification facts.
    """
    from . import API_VERSION, ASSURANCE
    from .attention import ATTENTION_OUTCOMES, attention_report
    from .context import cursor
    from .views import compact_resume
    limit = integer(limit, 1, 100, "attention_limit")
    with api._read_run(run_id) as (connection, run):
        if run["record"]:
            require(run["record"].get("record_hash") == canonical_hash({k: v for k, v in run["record"].items() if k != "record_hash"}),
                    "RECORD_CORRUPT", "Local completion record digest mismatch")
        selected = job_id or run["verification"].get("job_id")
        job = read_core.checked_job(api, connection, run_id, selected) if selected else None
        gates = semantics.gates(run, api.store.gate_measurements(connection, run))
        head = getattr(run, "head", None)
        current = compact_resume(run, [], {}, gates, cursor(head) if head else None)
        current.update(api_version=API_VERSION, assurance=ASSURANCE, ready=False,
                       workspace_constraints=workspace_constraints_view(run), diagnostic=diagnostic_view(run))
        cases = {"items": [], "total": 0, "next_offset": None}
        if job:
            where = "run_id=? AND job_id=? AND json_extract(data,'$.subject_role')=? AND json_extract(data,'$.case.outcome') IN (" + ",".join("?" for _ in ATTENTION_OUTCOMES) + ")"
            total = connection.execute("SELECT count(*) FROM case_observations WHERE " + where,
                                       [run_id, selected, "candidate", *ATTENTION_OUTCOMES]).fetchone()[0]
            values = api.store.cases(connection, run_id, selected, limit=limit, outcomes=ATTENTION_OUTCOMES, subject_role="candidate")
            cases = {"items": values, "total": total, "next_offset": len(values) if len(values) < total else None}
        result = attention_report(current, job_view(job) if job else None, cases, limit=limit)
        current_job = bool(job and job.get("purpose", "verification") == "verification"
                           and job["job_id"] == run["verification"].get("job_id") and run["candidate"]
                           and job.get("candidate_hash") == run["candidate"]["candidate_hash"]
                           and job.get("contract_hash") == run["contract"]["contract_hash"]
                           and job.get("check_set_hash") == semantics.check_set_hash(semantics.current_checks(run)))
        relevance = "current" if current_job else "diagnostic" if job and job.get("purpose") == "diagnostic" else "historical" if job else "not_run"
        if result["selected_job"]:
            result["selected_job"].update(is_current_verification=current_job, relevance=relevance)
            result["selected_job"]["contract_hash"] = job.get("contract_hash")
        for item in result["attention"]:
            item["scope"] = "current_declaration" if item["kind"] == "verification_gap" else relevance
            if item["kind"] == "check" and item.get("observation_id"):
                measured = read_core.record_value(api, connection, run, "measurements", item["observation_id"])
                digest = canonical_hash(measured)
                item["detail"] = {"kind": "measurements", "detail": {"record_id": item["observation_id"],
                    "record_digest": digest, "path": [], "sha256": digest}}
        result["budget_remaining"] = {"actions": max(0, run["limits"]["max_actions"] - run["actions"]),
            "verifications": max(0, run["limits"]["max_verifications"] - run["verification_attempts"]),
            "seconds": max(0, int(run["deadline_at"] - time.time()))}
        result["active_job_id"] = run["active_job"]
        result["attention_limit"] = limit
        result["snapshot"] = cursor(head) if head else None
        result["meaning"] = "observed_facts_and_known_constraints_not_rework_instructions_or_goal_proof"
        # The bound caller already has intent/policy IDs. Keep one current scope
        # and one recovery entrypoint instead of retransmitting identical refs.
        result.pop("intent_ref", None)
        result.pop("policy_ref", None)
        result["details"] = {"operation": "context", "run_id": run_id}
        result["run"] = {key: value for key, value in result["run"].items()
                         if key in {"run_id", "domain_id", "status", "phase", "lifecycle", "termination_reason"}}
        if result["candidate"]:
            result["candidate"].pop("subject", None)
        if current_job:
            result["selected_job"].pop("measurement_scope", None)
            result["selected_job"]["measurement_scope_source"] = "measurement.measurement_scope"
        if analysis_selection is not None:
            result["domain_preparation"] = api._analysis_view(run["intent"]["domain_id"], run["domain_preparation"], analysis_selection)
        return result


def resume(api, run_id, *, view="summary", analysis_selection=None, attention_limit=20):
    require(view in {"summary", "compact", "attention"}, "INVALID_VIEW", "Resume view is summary, compact or attention")
    if view == "attention":
        return attention(api, run_id, limit=attention_limit, analysis_selection=analysis_selection)
    require(attention_limit == 20, "INVALID_PARAMETERS", "attention_limit requires view=attention")
    with api._read_run(run_id) as (connection, run):
        latest = run["interpretations"][-1]
        checks = semantics.current_checks(run)
        checked, counts, gate_results = read_core.summary(api, connection, run)
        preparation = api._analysis_view(run["intent"]["domain_id"], run["domain_preparation"], analysis_selection)
        if view == "compact":
            from .context import cursor
            from .views import compact_resume
            snapshot = getattr(run, "head", None)
            result = compact_resume(run, checked, counts, gate_results, cursor(snapshot) if snapshot else None)
            if analysis_selection is not None:
                result["domain_preparation"] = preparation
            result["workspace_constraints"] = workspace_constraints_view(run)
            result["diagnostic"] = diagnostic_view(run)
            return result
        jobs = [job_view(job) for job in checked]
        return {"view": "summary", "run": run_view(run), "intent": run["intent"], "policy": run["policy"],
                "workspace_constraints": workspace_constraints_view(run),
                "diagnostic": diagnostic_view(run),
                "verification_scope": verification_scope(run, gate_results),
                "operation_constraints": operation_constraints(run, gate_results),
                "resolution": semantics.resolution(run, gate_results), "check_set_hash": semantics.check_set_hash(checks),
                "interpretation": {key: latest[key] for key in ("ref", "goal_summary", "assumptions", "open_questions")},
                "domain_questions": run["domain_questions"], "domain_preparation": preparation,
                "limits": run["limits"], "usage": {"actions": run["actions"], "verification_attempts": run["verification_attempts"]},
                "candidate": {key: run["candidate"][key] for key in ("candidate_id", "candidate_hash", "subject")} if run["candidate"] else None,
                "baseline": {key: run["baseline"][key] for key in ("candidate_id", "candidate_hash", "subject")} if run.get("baseline") else None,
                "predecessor": run.get("predecessor"),
                "checks": [{"check_id": c["check_id"], "ref": c["ref"], "kind": c["spec"]["parameters"]["kind"],
                            "gated": c["check_id"] in run["gate_bindings"],
                            "role": "mandatory" if c["check_id"] in run["gate_bindings"] else "exploratory"} for c in checks],
                "measurement": run["verification"], "assessment": semantics.assessment_view(run),
                "gates": gate_results,
                "unresolved_work": [t["task_id"] for t in run["logical_tasks"] if t["state"] != "settled"],
                "needs": need_records.summary(run),
                "recent_jobs": jobs, "history_counts": counts, "record": run["record"]}
