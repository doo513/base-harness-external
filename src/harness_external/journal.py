"""Versioned heads and immutable records in the SAME authoritative Run database.

Legacy JSON rows are not migrated. Heads contain references, never another copy
of historical record bodies. An event is a small immutable head snapshot; it also
provides a stable pagination boundary while workers continue making progress.
"""
import copy
from harness.json_codec import decode

from harness.common import canonical_bytes, canonical_hash
from .errors import require


SCHEMA = "run-store-v2"
COLLECTIONS = ("interpretations", "check_records", "assessments", "activity", "observations", "logical_tasks")
DDL = """
CREATE TABLE IF NOT EXISTS run_heads(run_id TEXT PRIMARY KEY, seq INTEGER NOT NULL, data TEXT NOT NULL, digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS run_records(run_id TEXT NOT NULL, ref TEXT NOT NULL, kind TEXT NOT NULL,
    position INTEGER NOT NULL, created_seq INTEGER NOT NULL, digest TEXT NOT NULL, data TEXT NOT NULL,
    PRIMARY KEY(run_id,ref));
CREATE INDEX IF NOT EXISTS run_records_kind ON run_records(run_id,kind,position,created_seq);
CREATE INDEX IF NOT EXISTS run_records_sequence ON run_records(run_id,created_seq);
CREATE TABLE IF NOT EXISTS run_events(run_id TEXT NOT NULL, seq INTEGER NOT NULL, data TEXT NOT NULL, digest TEXT NOT NULL,
    PRIMARY KEY(run_id,seq));
"""


class RunData(dict):
    """Internal read metadata is not serialized into API records."""
    head = None
    current_only = False


def head(connection, run_id, seq=None):
    if seq is None:
        row = connection.execute("SELECT seq,data,digest FROM run_heads WHERE run_id=?", (run_id,)).fetchone()
    else:
        row = connection.execute("SELECT seq,data,digest FROM run_events WHERE run_id=? AND seq=?", (run_id, seq)).fetchone()
    if row is None:
        return None
    value = decode(row[1])
    require(value.get("run_id") == run_id and value.get("seq") == row[0] and canonical_hash(value) == row[2],
            "STATE_CORRUPT", "Run head/event digest mismatch")
    if seq is None:
        event = connection.execute("SELECT digest FROM run_events WHERE run_id=? AND seq=?", (run_id, row[0])).fetchone()
        require(event is not None and event[0] == row[2], "STATE_CORRUPT", "Current head has no matching immutable event")
    return value


def put(connection, run_id, seq, kind, position, value):
    digest = canonical_hash(value)
    ref = canonical_hash({"kind": kind, "position": position, "digest": digest})
    existing = connection.execute("SELECT digest FROM run_records WHERE run_id=? AND ref=?", (run_id, ref)).fetchone()
    if existing is None:
        connection.execute("INSERT INTO run_records VALUES(?,?,?,?,?,?,?)",
                           (run_id, ref, kind, position, seq, digest, canonical_bytes(value).decode()))
    else:
        require(existing[0] == digest, "SEMANTIC_RECORD_CORRUPT", "Immutable record reference changed")
    return ref


def get(connection, run_id, ref):
    row = connection.execute("SELECT kind,position,digest,data FROM run_records WHERE run_id=? AND ref=?", (run_id, ref)).fetchone()
    require(row is not None, "SEMANTIC_RECORD_CORRUPT", "Missing referenced Run record")
    value = decode(row[3])
    require(canonical_hash(value) == row[2] and canonical_hash({"kind": row[0], "position": row[1], "digest": row[2]}) == ref,
            "SEMANTIC_RECORD_CORRUPT", "Run record digest mismatch")
    return value


def collection_refs(connection, snapshot, kind, *, offset=0, limit=None):
    # Position is stable; task updates append a version instead of overwriting it.
    sql = """SELECT r.ref FROM run_records r JOIN
        (SELECT position,MAX(created_seq) AS last FROM run_records
         WHERE run_id=? AND kind=? AND created_seq<=? GROUP BY position) latest
        ON r.position=latest.position AND r.created_seq=latest.last
        WHERE r.run_id=? AND r.kind=? ORDER BY r.position"""
    args = [snapshot["run_id"], kind, snapshot["seq"], snapshot["run_id"], kind]
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        args += [limit, offset]
    return [row[0] for row in connection.execute(sql, args)]


def load(connection, snapshot, *, current=False):
    run_id = snapshot["run_id"]
    value = RunData({key: get(connection, run_id, ref) for key, ref in snapshot["fields"].items()})
    for kind in COLLECTIONS:
        refs = snapshot["collections"][kind]["current"] if current else collection_refs(connection, snapshot, kind)
        value[kind] = [get(connection, run_id, ref) for ref in refs]
    value.head = snapshot
    value.current_only = current
    return value


def publish(connection, snapshot):
    digest = canonical_hash(snapshot)
    data = canonical_bytes(snapshot).decode()
    connection.execute("INSERT INTO run_events VALUES(?,?,?,?)", (snapshot["run_id"], snapshot["seq"], data, digest))
    connection.execute("INSERT INTO run_heads VALUES(?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET seq=excluded.seq,data=excluded.data,digest=excluded.digest",
                       (snapshot["run_id"], snapshot["seq"], data, digest))


def save(connection, run):
    require(not getattr(run, "current_only", False), "INCOMPLETE_RUN_WRITE", "A summary cannot replace complete Run state")
    run_id = run["run_id"]
    previous = head(connection, run_id)
    snapshot = copy.deepcopy(previous) if previous else {"schema_version": SCHEMA, "run_id": run_id, "seq": 0,
                                                        "fields": {}, "collections": {}, "jobs": {}, "measurement_count": 0}
    snapshot["seq"] += 1
    seq = snapshot["seq"]
    snapshot["fields"] = {key: put(connection, run_id, seq, "field:" + key, 0, value)
                          for key, value in run.items() if key not in COLLECTIONS}
    for kind in COLLECTIONS:
        require(previous is None or len(run[kind]) >= previous["collections"][kind]["count"],
                "HISTORY_IMMUTABLE", "Historical records cannot be removed")
        refs = [put(connection, run_id, seq, kind, index, item) for index, item in enumerate(run[kind])]
        current = refs
        if kind == "interpretations":
            current = refs[-1:]
        elif kind == "check_records":
            current = list({item["check_id"]: ref for item, ref in zip(run[kind], refs)}.values())
        elif kind == "assessments":
            subject = run["candidate"]["subject"] if run.get("candidate") else None
            current = [ref for item, ref in zip(run[kind], refs) if item["interpretation_ref"] == run["interpretations"][-1]["ref"]
                       and item["subject"] == subject][-1:]
        elif kind in {"observations", "activity"}:
            current = []
        snapshot["collections"][kind] = {"count": len(refs), "current": current}
    publish(connection, snapshot)


def save_job(connection, job):
    snapshot = head(connection, job["run_id"])
    if snapshot is None:
        return
    # Heartbeats/PIDs are liveness transport, not new reasoning or evidence.
    from .views import job_view
    projected = job_view(job)
    projected.pop("heartbeat_at", None)
    existing = snapshot["jobs"].get(job["job_id"])
    if existing and get(connection, job["run_id"], existing) == projected:
        return
    snapshot["seq"] += 1
    ref = put(connection, job["run_id"], snapshot["seq"], "job:" + job["job_id"], 0, projected)
    snapshot["jobs"][job["job_id"]] = ref
    publish(connection, snapshot)


def measurement(connection, item):
    snapshot = head(connection, item["run_id"])
    if snapshot is None:
        return
    snapshot["seq"] += 1
    put(connection, item["run_id"], snapshot["seq"], "measurements", snapshot["measurement_count"],
        {"observation_id": item["observation_id"], "job_id": item["job_id"], "record_hash": item["record_hash"]})
    snapshot["measurement_count"] += 1
    publish(connection, snapshot)


def case_observation(connection, item):
    snapshot = head(connection, item["run_id"])
    if snapshot is None:
        return
    snapshot["seq"] += 1
    put(connection, item["run_id"], snapshot["seq"], "cases", snapshot.get("case_count", 0),
        {"observation_id": item["observation_id"], "job_id": item["job_id"], "record_hash": item["record_hash"]})
    snapshot["case_count"] = snapshot.get("case_count", 0) + 1
    publish(connection, snapshot)
