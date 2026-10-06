"""Validated read boundary; rendering cannot waive evidence/reference checks."""
from harness.common import canonical_hash
from . import journal, semantics
from .errors import integer, require

COLLECTIONS = {"checks": "check_records", "interpretations": "interpretations", "activity": "activity",
               "assessments": "assessments", "tasks": "logical_tasks", "notes": "observations", "needs": "needs"}
RECENT_JOB_LIMIT = 5


def page_bounds(offset, limit):
    return integer(offset, 0, 2**31 - 1, "offset"), integer(limit, 1, 100, "limit")


def checked_job(api, connection, run_id, job_id):
    job = api.store.job(connection, job_id)
    require(job["run_id"] == run_id, "JOB_RUN_MISMATCH", "Job belongs to another Run")
    api.store.validate_result(connection, job)
    return job


def summary(api, connection, run):
    if run["record"]:
        require(run["record"].get("record_hash") == canonical_hash({k: v for k, v in run["record"].items() if k != "record_hash"}),
                "RECORD_CORRUPT", "Local completion record digest mismatch")
    run_id = run["run_id"]
    jobs = [checked_job(api, connection, run_id, row[0]) for row in connection.execute(
        "SELECT job_id FROM jobs WHERE run_id=? ORDER BY rowid DESC LIMIT ?", (run_id, RECENT_JOB_LIMIT))]
    snapshot = getattr(run, "head", None)
    counts = {name: journal.collection_head(snapshot, key)["count"] if snapshot else
              len(run.get(key, []) if key == "needs" else run[key]) for name, key in COLLECTIONS.items()}
    for name in ("measurements", "jobs"):
        counts[name] = connection.execute("SELECT count(*) FROM " + name + " WHERE run_id=?", (run_id,)).fetchone()[0]
    counts["cases"] = connection.execute("SELECT count(*) FROM case_observations WHERE run_id=?", (run_id,)).fetchone()[0]
    return jobs, counts, semantics.gates(run, api.store.gate_measurements(connection, run))


def records(api, run_id, kind, *, offset=0, limit=20, job_id=None):
    offset, limit = page_bounds(offset, limit)
    require(isinstance(kind, str) and kind in {*COLLECTIONS, "measurements", "jobs", "cases"}, "INVALID_PARAMETERS", "Unknown history kind")
    require(job_id is None or kind in {"measurements", "cases"}, "INVALID_PARAMETERS", "job_id only filters observations")
    with api._read_run(run_id) as (connection, run):
        if job_id:
            checked_job(api, connection, run_id, job_id)
        if kind in COLLECTIONS:
            key = COLLECTIONS[kind]
            snapshot = getattr(run, "head", None)
            if snapshot:
                total = journal.collection_head(snapshot, key)["count"]
                items = [journal.get(connection, run_id, ref) for ref in journal.collection_refs(connection, snapshot, key, offset=offset, limit=limit)]
                for item in items:
                    if key in {"interpretations", "check_records", "assessments", "activity", "needs"}:
                        semantics.validate(item)
            else:
                data = run.get(key, []) if key == "needs" else run[key]
                total, items = len(data), data[offset:offset + limit]
        elif kind == "jobs":
            total = connection.execute("SELECT count(*) FROM jobs WHERE run_id=?", (run_id,)).fetchone()[0]
            ids = connection.execute("SELECT job_id FROM jobs WHERE run_id=? ORDER BY rowid LIMIT ? OFFSET ?", (run_id, limit, offset))
            from .views import job_view
            items = [job_view(checked_job(api, connection, run_id, row[0])) for row in ids]
        else:
            where, args = "run_id=?", [run_id]
            if job_id:
                where += " AND job_id=?"
                args.append(job_id)
            table = "case_observations" if kind == "cases" else "measurements"
            total = connection.execute("SELECT count(*) FROM " + table + " WHERE " + where, args).fetchone()[0]
            items = api.store.cases(connection, run_id, job_id, limit=limit, offset=offset) if kind == "cases" else api.store.measurements(connection, run_id, job_id, limit=limit, offset=offset)
    return {"run_id": run_id, "kind": kind, "items": items, "total": total,
            "next_offset": offset + len(items) if offset + len(items) < total else None}
