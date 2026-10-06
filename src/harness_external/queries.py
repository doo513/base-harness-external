"""Small recovery views and bounded history pages for external callers."""
from . import semantics, need_records
from . import read_core
from .read_core import COLLECTIONS, RECENT_JOB_LIMIT, page_bounds, records
from .views import job_view, run_view


def job_summary(api, run_id, job_id):
    with api._read_run(run_id) as (connection, run):
        job = read_core.checked_job(api, connection, run_id, job_id)
        return {"run_id": run_id, "view": "summary", "job": job_view(job),
                "measurement_count": connection.execute("SELECT count(*) FROM measurements WHERE run_id=? AND job_id=?", (run_id, job_id)).fetchone()[0]}


def list_runs(api, *, offset=0, limit=20):
    offset, limit = page_bounds(offset, limit)
    with api.store.transaction(write=False) as connection:
        ids = [row[0] for row in connection.execute("SELECT run_id FROM (SELECT run_id,0 AS f,rowid AS n FROM runs UNION ALL SELECT run_id,1 AS f,rowid AS n FROM run_heads) ORDER BY f,n LIMIT ? OFFSET ?", (limit, offset))]
    with api.store.transaction(write=False) as connection:
        total = connection.execute("SELECT (SELECT count(*) FROM runs)+(SELECT count(*) FROM run_heads)").fetchone()[0]
    items = []
    for key in ids:
        with api._read_run(key) as (_, run):
            items.append(run_view(run))
    return {"items": items, "total": total, "next_offset": offset + len(items) if offset + len(items) < total else None}


def resume(api, run_id):
    with api._read_run(run_id) as (connection, run):
        latest = run["interpretations"][-1]
        checks = semantics.current_checks(run)
        checked, counts, gate_results = read_core.summary(api, connection, run)
        jobs = [job_view(job) for job in checked]
        return {"view": "summary", "run": run_view(run), "intent": run["intent"], "policy": run["policy"],
                "resolution": semantics.resolution(run, gate_results), "check_set_hash": semantics.check_set_hash(checks),
                "interpretation": {key: latest[key] for key in ("ref", "goal_summary", "assumptions", "open_questions")},
                "domain_questions": run["domain_questions"], "domain_preparation": run["domain_preparation"],
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
