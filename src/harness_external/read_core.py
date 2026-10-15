"""Validated read boundary; rendering cannot waive evidence/reference checks."""
import base64
import hashlib

from harness.common import canonical_bytes, canonical_hash
from . import journal, semantics
from .errors import fields, integer, require

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


def records(api, run_id, kind, *, offset=0, limit=20, job_id=None, view="full", detail=None, outcomes=None, subject_role=None):
    offset, limit = page_bounds(offset, limit)
    require(isinstance(kind, str) and kind in {*COLLECTIONS, "measurements", "jobs", "cases"}, "INVALID_PARAMETERS", "Unknown history kind")
    require(job_id is None or kind in {"measurements", "cases"}, "INVALID_PARAMETERS", "job_id only filters observations")
    require(view in {"full", "references"}, "INVALID_VIEW", "Records view is full or references")
    if outcomes is not None:
        require(kind == "cases" and detail is None and isinstance(outcomes, list) and 1 <= len(outcomes) <= 16
                and all(isinstance(item, str) and 0 < len(item) <= 64 for item in outcomes)
                and len(set(outcomes)) == len(outcomes), "INVALID_FILTER", "outcomes is a bounded case-only string filter")
    if subject_role is not None:
        require(kind == "cases" and detail is None and isinstance(subject_role, str) and subject_role in {"candidate", "baseline"},
                "INVALID_FILTER", "subject_role is candidate or baseline on case pages")
    if detail is not None:
        require(view == "full" and job_id is None and offset == 0 and limit == 20,
                "DETAIL_ARGUMENTS", "A detail request cannot include record-page selectors")
        return read_detail(api, run_id, kind, detail)
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
            if outcomes is not None:
                where += " AND json_extract(data,'$.case.outcome') IN (" + ",".join("?" for _ in outcomes) + ")"
                args.extend(outcomes)
            if subject_role is not None:
                where += " AND json_extract(data,'$.subject_role')=?"
                args.append(subject_role)
            table = "case_observations" if kind == "cases" else "measurements"
            total = connection.execute("SELECT count(*) FROM " + table + " WHERE " + where, args).fetchone()[0]
            items = api.store.cases(connection, run_id, job_id, limit=limit, offset=offset, outcomes=outcomes, subject_role=subject_role) if kind == "cases" else api.store.measurements(connection, run_id, job_id, limit=limit, offset=offset)
        if view == "references":
            from .detail_projection import project
            items = [project(item, {"run_id": run_id, "kind": kind,
                      "record_id": item["observation_id"] if kind in {"measurements", "cases"} else
                                   item["job_id"] if kind == "jobs" else offset + index}) for index, item in enumerate(items)]
    result = {"run_id": run_id, "kind": kind, "items": items, "total": total,
              "next_offset": offset + len(items) if offset + len(items) < total else None}
    if view != "full":
        result["view"] = view
    return result


def read_detail(api, run_id, kind, request):
    """Read a bounded JSON byte range from a digest-bound existing record."""
    fields(request, {"record_id", "record_digest", "path", "sha256", "offset", "limit"},
           {"record_id", "record_digest", "path", "sha256"})
    position, limit = integer(request.get("offset", 0), 0, 2**31 - 1, "detail offset"), integer(request.get("limit", 4096), 1, 16384, "detail limit")
    path = request["path"]
    require(isinstance(path, list) and len(path) <= 64 and all(type(key) in {str, int} for key in path),
            "DETAIL_PATH", "Use a bounded JSON field/index path")
    record_id = request["record_id"]
    with api._read_run(run_id) as (connection, run):
        if kind in {"measurements", "cases"}:
            require(isinstance(record_id, str), "DETAIL_REFERENCE", "Observation ID is required")
            table = "measurements" if kind == "measurements" else "case_observations"
            rows = connection.execute("SELECT data FROM " + table + " WHERE run_id=? AND observation_id=?", (run_id, record_id))
            values = api.store._measurement_rows(rows, run_id) if kind == "measurements" else api.store._case_rows(rows, run_id)
            require(len(values) == 1, "DETAIL_REFERENCE", "Observation does not belong to this Run")
            value = values[0]
            job = checked_job(api, connection, run_id, value["job_id"])
            if kind == "cases":
                api.store.validate_case_binding(value, job)
        elif kind == "jobs":
            from .views import job_view
            value = job_view(checked_job(api, connection, run_id, record_id))
        else:
            position_id = integer(record_id, 0, 2**31 - 1, "record position")
            key = COLLECTIONS[kind]
            snapshot = getattr(run, "head", None)
            if snapshot:
                refs = journal.collection_refs(connection, snapshot, key, offset=position_id, limit=1)
                require(len(refs) == 1, "DETAIL_REFERENCE", "Unknown record position")
                value = journal.get(connection, run_id, refs[0])
            else:
                data = run.get(key, [])
                require(position_id < len(data), "DETAIL_REFERENCE", "Unknown record position")
                value = data[position_id]
        require(canonical_hash(value) == request["record_digest"], "DETAIL_CHANGED", "Record changed since projection; read a new projection")
        try:
            for key in path:
                require(isinstance(value, dict) and isinstance(key, str) and key in value or
                        isinstance(value, list) and type(key) is int and 0 <= key < len(value), "DETAIL_PATH", "Unknown JSON field/index")
                value = value[key]
        except (KeyError, IndexError, TypeError) as error:
            raise ValueError("Invalid detail path") from error
        encoded = canonical_bytes(value)
        require(hashlib.sha256(encoded).hexdigest() == request["sha256"], "DETAIL_CHANGED", "Detail digest changed")
        require(position <= len(encoded), "DETAIL_OFFSET", "Detail offset exceeds its byte length")
        end = min(position + limit, len(encoded))
        return {"schema_version": "record-detail-chunk-v1", "run_id": run_id, "kind": kind, "record_id": record_id,
                "record_digest": request["record_digest"], "path": path, "sha256": request["sha256"],
                "encoding": "base64-canonical-json", "data": base64.b64encode(encoded[position:end]).decode("ascii"),
                "offset": position, "total_bytes": len(encoded), "next_offset": end if end < len(encoded) else None}
