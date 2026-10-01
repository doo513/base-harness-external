"""Small recovery views and bounded history pages for external callers."""
from . import semantics
from .errors import integer, require
from harness.common import canonical_hash


def page_bounds(offset, limit):
    return integer(offset, 0, 2**31 - 1, "offset"), integer(limit, 1, 100, "limit")


def job_view(job):
    return {**{key: job.get(key) for key in ("job_id", "run_id", "status", "cleanup", "heartbeat_at",
            "deadline_at", "completed_checks", "total_checks", "error", "check_set_hash", "candidate_hash")},
            "result": {key: job["result"].get(key) for key in ("status", "result_hash", "check_set_hash")} if job.get("result") else None}


def job_summary(api, run_id, job_id):
    with api.store.transaction(write=False) as connection:
        job = api.store.job(connection, job_id)
        require(job["run_id"] == run_id, "JOB_RUN_MISMATCH", "Job belongs to another Run")
        api.store.validate_result(connection, job)
        return {"run_id": run_id, "view": "summary", "job": job_view(job),
                "measurement_count": connection.execute("SELECT count(*) FROM measurements WHERE run_id=? AND job_id=?", (run_id, job_id)).fetchone()[0]}


def run_view(run):
    return {"run_id": run["run_id"], "domain_id": run["contract"]["domain_id"], "goal": run["goal"],
            "workspace": run["workspace"], "status": run["status"], "phase": run["phase"],
            "lifecycle": semantics.lifecycle(run), "created_at": run["created_at"], "deadline_at": run["deadline_at"],
            "interpretation_ref": run["interpretations"][-1]["ref"], "active_job": run["active_job"],
            "termination_reason": run.get("termination_reason")}


def list_runs(api, *, offset=0, limit=20):
    offset, limit = page_bounds(offset, limit)
    with api.store.transaction(write=False) as connection:
        ids = [row[0] for row in connection.execute("SELECT run_id FROM runs ORDER BY rowid LIMIT ? OFFSET ?", (limit, offset))]
    for key in ids:
        api._refresh_for_read(key)
    with api.store.transaction(write=False) as connection:
        total = connection.execute("SELECT count(*) FROM runs").fetchone()[0]
        ids = [row[0] for row in connection.execute("SELECT run_id FROM runs ORDER BY rowid LIMIT ? OFFSET ?", (limit, offset))]
        items = []
        for key in ids:
            run = api.store.run(connection, key)
            items.append(run_view(run))
    return {"items": items, "total": total, "next_offset": offset + len(items) if offset + len(items) < total else None}


def resume(api, run_id):
    api._refresh_for_read(run_id)
    with api.store.transaction(write=False) as connection:
        run = api.store.run(connection, run_id)
        if run["record"]:
            require(run["record"].get("record_hash") == canonical_hash({k: v for k, v in run["record"].items() if k != "record_hash"}),
                    "RECORD_CORRUPT", "Local completion record digest mismatch")
        latest = run["interpretations"][-1]
        checks = semantics.current_checks(run)
        jobs = []
        for row in connection.execute("SELECT job_id FROM jobs WHERE run_id=? ORDER BY rowid DESC", (run_id,)):
            job = api.store.job(connection, row[0])
            api.store.validate_result(connection, job)
            if len(jobs) < 5:
                jobs.append(job_view(job))
        counts = {name: len(run[key]) for name, key in COLLECTIONS.items()}
        counts["measurements"] = connection.execute("SELECT count(*) FROM measurements WHERE run_id=?", (run_id,)).fetchone()[0]
        counts["jobs"] = connection.execute("SELECT count(*) FROM jobs WHERE run_id=?", (run_id,)).fetchone()[0]
        gate_results = semantics.gates(run, api.store.gate_measurements(connection, run))
        return {"view": "summary", "run": run_view(run), "intent": run["intent"], "policy": run["policy"],
                "resolution": semantics.resolution(run, gate_results), "check_set_hash": semantics.check_set_hash(checks),
                "interpretation": {key: latest[key] for key in ("ref", "goal_summary", "assumptions", "open_questions")},
                "domain_questions": run["domain_questions"], "domain_preparation": run["domain_preparation"],
                "limits": run["limits"], "usage": {"actions": run["actions"], "verification_attempts": run["verification_attempts"]},
                "candidate": {key: run["candidate"][key] for key in ("candidate_id", "candidate_hash", "subject")} if run["candidate"] else None,
                "checks": [{"check_id": c["check_id"], "ref": c["ref"], "kind": c["spec"]["parameters"]["kind"],
                            "gated": c["check_id"] in run["gate_bindings"]} for c in checks],
                "measurement": run["verification"], "assessment": semantics.assessment_view(run),
                "gates": gate_results,
                "unresolved_work": [t["task_id"] for t in run["logical_tasks"] if t["state"] != "settled"],
                "recent_jobs": jobs, "history_counts": counts, "record": run["record"]}


COLLECTIONS = {"checks": "check_records", "interpretations": "interpretations", "activity": "activity",
               "assessments": "assessments", "tasks": "logical_tasks", "notes": "observations"}


def records(api, run_id, kind, *, offset=0, limit=20, job_id=None):
    offset, limit = page_bounds(offset, limit)
    require(isinstance(kind, str) and kind in {*COLLECTIONS, "measurements", "jobs"}, "INVALID_PARAMETERS", "Unknown history kind")
    require(job_id is None or kind == "measurements", "INVALID_PARAMETERS", "job_id only filters measurements")
    with api.store.transaction(write=False) as connection:
        run = api.store.run(connection, run_id)
        if job_id:
            require(api.store.job(connection, job_id)["run_id"] == run_id, "JOB_RUN_MISMATCH", "Job belongs to another Run")
        if kind in COLLECTIONS:
            data = run[COLLECTIONS[kind]]
            total, items = len(data), data[offset:offset + limit]
        elif kind == "jobs":
            total = connection.execute("SELECT count(*) FROM jobs WHERE run_id=?", (run_id,)).fetchone()[0]
            ids = connection.execute("SELECT job_id FROM jobs WHERE run_id=? ORDER BY rowid LIMIT ? OFFSET ?", (run_id, limit, offset))
            items = []
            for row in ids:
                job = api.store.job(connection, row[0])
                api.store.validate_result(connection, job)
                items.append(job_view(job))
        else:
            where, args = "run_id=?", [run_id]
            if job_id:
                where += " AND job_id=?"
                args.append(job_id)
            total = connection.execute("SELECT count(*) FROM measurements WHERE " + where, args).fetchone()[0]
            items = api.store.measurements(connection, run_id, job_id, limit=limit, offset=offset)
    return {"run_id": run_id, "kind": kind, "items": items, "total": total,
            "next_offset": offset + len(items) if offset + len(items) < total else None}
