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
COLLECTIONS = ("interpretations", "check_records", "assessments", "activity", "observations", "logical_tasks", "needs")
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

    def track(self, refs):
        self._dirty_fields, self._dirty_collections = set(), {}
        self._loaded_refs = refs
        for key, value in list(self.items()):
            dict.__setitem__(self, key, self._wrap(key, value))

    def _wrap(self, key, value):
        if key in COLLECTIONS:
            return _Collection(value, lambda position: self._dirty_collections.setdefault(key, set()).add(position))
        return _tracked(value, lambda: self._dirty_fields.add(key))

    def __setitem__(self, key, value):
        if hasattr(self, "_dirty_fields"):
            value = self._wrap(key, value)
            if key in COLLECTIONS:
                self._dirty_collections[key] = set(range(len(value)))
            else:
                self._dirty_fields.add(key)
        dict.__setitem__(self, key, value)

    def __delitem__(self, key):
        if hasattr(self, "_dirty_fields"):
            self._dirty_fields.add(key)
        dict.__delitem__(self, key)

    def update(self, *args, **kwargs):
        for key, value in dict(*args, **kwargs).items():
            self[key] = value

    def setdefault(self, key, default=None):
        if key not in self:
            self[key] = default
        return self[key]

    def pop(self, key, *default):
        if key not in self and default:
            return default[0]
        value = self[key]
        del self[key]
        return value

    def clear(self):
        for key in list(self):
            del self[key]

    def popitem(self):
        if not self:
            raise KeyError("popitem(): dictionary is empty")
        key = next(reversed(self))
        return key, self.pop(key)

    def __ior__(self, other):
        self.update(other)
        return self

    def __deepcopy__(self, memo):
        # Detached application copies must not retain callbacks into this Run.
        return copy.deepcopy(dict(self), memo)


def _tracked(value, changed):
    if isinstance(value, dict):
        return _Dict(value, changed)
    if isinstance(value, list):
        return _List(value, changed)
    return value


class _Dict(dict):
    def __init__(self, value, changed):
        self._changed = changed
        dict.__init__(self, ((key, _tracked(item, changed)) for key, item in value.items()))

    def __setitem__(self, key, value):
        dict.__setitem__(self, key, _tracked(value, self._changed))
        self._changed()

    def __delitem__(self, key):
        dict.__delitem__(self, key)
        self._changed()

    update = RunData.update
    setdefault = RunData.setdefault
    pop = RunData.pop

    def clear(self):
        dict.clear(self)
        self._changed()

    def popitem(self):
        value = dict.popitem(self)
        self._changed()
        return value

    def __ior__(self, other):
        self.update(other)
        return self

    def __deepcopy__(self, memo):
        return copy.deepcopy(dict(self), memo)


class _List(list):
    def __init__(self, value, changed):
        self._changed = changed
        list.__init__(self, (_tracked(item, changed) for item in value))

    def __setitem__(self, key, value):
        value = [_tracked(item, self._changed) for item in value] if isinstance(key, slice) else _tracked(value, self._changed)
        list.__setitem__(self, key, value)
        self._changed()

    def __delitem__(self, key):
        list.__delitem__(self, key)
        self._changed()

    def append(self, value):
        list.append(self, _tracked(value, self._changed))
        self._changed()

    def extend(self, values):
        list.extend(self, (_tracked(value, self._changed) for value in list(values)))
        self._changed()

    def insert(self, index, value):
        list.insert(self, index, _tracked(value, self._changed))
        self._changed()

    def pop(self, index=-1):
        value = list.pop(self, index)
        self._changed()
        return value

    def remove(self, value):
        list.remove(self, value)
        self._changed()

    def clear(self):
        list.clear(self)
        self._changed()

    def reverse(self):
        list.reverse(self)
        self._changed()

    def sort(self, *args, **kwargs):
        list.sort(self, *args, **kwargs)
        self._changed()

    def __iadd__(self, values):
        self.extend(values)
        return self

    def __imul__(self, count):
        self[:] = list(self) * count
        return self

    def __deepcopy__(self, memo):
        return copy.deepcopy(list(self), memo)


class _Collection(_List):
    """Stable record positions; appending never dirties historical bodies."""
    def __init__(self, values, changed):
        self._position_changed = changed
        self._changed = self._all_changed
        list.__init__(self, (self._record(value, index) for index, value in enumerate(values)))

    def _record(self, value, index):
        return _tracked(value, lambda: self._position_changed(index))

    def _all_changed(self):
        for index, value in enumerate(list(self)):
            list.__setitem__(self, index, self._record(value, index))
            self._position_changed(index)

    def append(self, value):
        index = len(self)
        list.append(self, self._record(value, index))
        self._position_changed(index)

    def extend(self, values):
        for value in list(values):
            self.append(value)

    def __setitem__(self, key, value):
        if isinstance(key, slice):
            list.__setitem__(self, key, value)
            self._all_changed()
        else:
            index = key if key >= 0 else len(self) + key
            list.__setitem__(self, key, self._record(value, index))
            self._position_changed(index)


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
    bounds = " AND position>=? AND position<?" if limit is not None else ""
    sql = """SELECT r.ref FROM run_records r JOIN
        (SELECT position,MAX(created_seq) AS last FROM run_records
         WHERE run_id=? AND kind=? AND created_seq<=?""" + bounds + """ GROUP BY position) latest
        ON r.position=latest.position AND r.created_seq=latest.last
        WHERE r.run_id=? AND r.kind=? ORDER BY r.position"""
    args = [snapshot["run_id"], kind, snapshot["seq"]]
    if limit is not None:
        args += [offset, offset + limit]
    args += [snapshot["run_id"], kind]
    return [row[0] for row in connection.execute(sql, args)]


def collection_head(snapshot, kind):
    # Only the additive Need collection may be absent in older v2 snapshots.
    require(kind == "needs" or kind in snapshot["collections"], "STATE_CORRUPT", "Missing Run collection")
    return snapshot["collections"].get(kind, {"count": 0, "current": []})


def load(connection, snapshot, *, current=False):
    run_id = snapshot["run_id"]
    value = RunData({key: get(connection, run_id, ref) for key, ref in snapshot["fields"].items()})
    loaded_refs = {}
    for kind in COLLECTIONS:
        metadata = collection_head(snapshot, kind)
        refs = metadata["current"] if current else collection_refs(connection, snapshot, kind)
        loaded_refs[kind] = refs
        value[kind] = [get(connection, run_id, ref) for ref in refs]
    value.head = snapshot
    value.current_only = current
    if not current:
        value.track(loaded_refs)
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
    incremental = isinstance(run, RunData) and hasattr(run, "_dirty_fields") and run.head is not None
    if incremental:
        require(previous is not None and previous["fields"] == run.head["fields"]
                and all(collection_head(previous, kind) == collection_head(run.head, kind) for kind in COLLECTIONS),
                "STALE_RUN_WRITE", "Run state changed since this complete read")
    snapshot = copy.deepcopy(previous) if previous else {"schema_version": SCHEMA, "run_id": run_id, "seq": 0,
                                                        "fields": {}, "collections": {}, "jobs": {}, "measurement_count": 0}
    snapshot["seq"] += 1
    seq = snapshot["seq"]
    if incremental:
        snapshot["fields"] = dict(previous["fields"])
        for key in run._dirty_fields:
            if key not in run:
                snapshot["fields"].pop(key, None)
            else:
                snapshot["fields"][key] = put(connection, run_id, seq, "field:" + key, 0, run[key])
    else:
        snapshot["fields"] = {key: put(connection, run_id, seq, "field:" + key, 0, value)
                              for key, value in run.items() if key not in COLLECTIONS}
    saved_refs = {}
    for kind in COLLECTIONS:
        values = run.get(kind, []) if kind == "needs" else run[kind]
        require(previous is None or len(values) >= collection_head(previous, kind)["count"],
                "HISTORY_IMMUTABLE", "Historical records cannot be removed")
        if incremental:
            refs = list(run._loaded_refs[kind])
            changed = run._dirty_collections.get(kind, set()) | set(range(len(refs), len(values)))
            for index in sorted(changed):
                require(index < len(values), "HISTORY_IMMUTABLE", "Historical records cannot be removed")
                ref = put(connection, run_id, seq, kind, index, values[index])
                if index < len(refs):
                    refs[index] = ref
                else:
                    refs.append(ref)
        else:
            refs = [put(connection, run_id, seq, kind, index, item) for index, item in enumerate(values)]
        saved_refs[kind] = refs
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
    if incremental:
        run.head = snapshot
        run._loaded_refs = saved_refs
        run._dirty_fields.clear()
        run._dirty_collections.clear()


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
