"""Lossless context transfers; cursors are read positions, never authority."""
import base64
import copy
import json

from harness.common import canonical_bytes, canonical_hash
from . import journal, read_core, semantics
from .errors import integer, require

VERSION = "harness-context-v1"


def encode(value):
    return base64.urlsafe_b64encode(canonical_bytes(value)).decode().rstrip("=")


def decode(value):
    if not isinstance(value, str) or len(value) > 8192:
        return None
    try:
        result = json.loads(base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True))
        return result if isinstance(result, dict) else None
    except (ValueError, UnicodeError, RecursionError):
        return None


def cursor(snapshot):
    return {"version": VERSION, "run_id": snapshot["run_id"], "seq": snapshot["seq"], "hash": canonical_hash(snapshot)}


def resolve(connection, run_id, token):
    if not isinstance(token, dict) or token.get("version") != VERSION or token.get("run_id") != run_id:
        return None
    if type(token.get("seq")) is not int or token["seq"] < 1:
        return None
    value = journal.head(connection, run_id, token["seq"])
    return value if value and canonical_hash(value) == token.get("hash") else None


def pack(value, texts):
    refs = []
    def walk(item, path):
        if isinstance(item, str) and len(item) >= 80:
            digest = canonical_hash(item)
            texts[digest] = item
            refs.append({"path": path, "text_ref": digest})
            return None
        if isinstance(item, dict):
            return {key: walk(body, path + [key]) for key, body in item.items()}
        if isinstance(item, list):
            return [walk(body, path + [index]) for index, body in enumerate(item)]
        return item
    return {"data": walk(value, []), "text_refs": refs}


def unpack(item, texts):
    value = copy.deepcopy(item["data"])
    for ref in item["text_refs"]:
        text = texts[ref["text_ref"]]
        require(canonical_hash(text) == ref["text_ref"], "CONTEXT_CORRUPT", "Text digest mismatch")
        if not ref["path"]:
            value = text
        else:
            target = value
            for key in ref["path"][:-1]:
                target = target[key]
            target[ref["path"][-1]] = text
    require(canonical_hash(value) == item["digest"], "CONTEXT_CORRUPT", "Context record digest mismatch")
    return value


def apply_page(document, page):
    """Reference decoder for consumers/tests. No Core state is modified."""
    result = {} if page["delivery"] == "full" and page["offset"] == 0 else copy.deepcopy(document)
    for item in page["items"]:
        result[item["key"]] = unpack(item, page["texts"])
    return result


def inventory(connection, snapshot, base):
    items = []
    for key, ref in snapshot["fields"].items():
        if base is None or base["fields"].get(key) != ref:
            items.append(("field:" + key, ref))
    for kind in (*journal.COLLECTIONS, "measurements"):
        if base is not None and snapshot["seq"] == base["seq"]:
            continue
        if base is None:
            refs = journal.collection_refs(connection, snapshot, kind)
            items.extend((kind + ":" + str(index), ref) for index, ref in enumerate(refs))
        else:
            rows = connection.execute("""SELECT position,ref,created_seq FROM run_records
                WHERE run_id=? AND kind=? AND created_seq>? AND created_seq<=? ORDER BY position,created_seq""",
                (snapshot["run_id"], kind, base["seq"], snapshot["seq"]))
            changed = {row[0]: row[1] for row in rows}
            items.extend((kind + ":" + str(index), ref) for index, ref in changed.items())
    for key, ref in snapshot["jobs"].items():
        if base is None or base["jobs"].get(key) != ref:
            items.append(("job:" + key, ref))
    return items


def facts_at(api, connection, snapshot):
    facts = []
    for ref in journal.collection_refs(connection, snapshot, "measurements"):
        link = journal.get(connection, snapshot["run_id"], ref)
        rows = connection.execute("SELECT data FROM measurements WHERE run_id=? AND observation_id=? AND job_id=?",
                                  (snapshot["run_id"], link["observation_id"], link["job_id"]))
        items = api.store._measurement_rows(rows, snapshot["run_id"])
        require(len(items) == 1 and items[0]["record_hash"] == link["record_hash"], "RESULT_BINDING", "Context measurement is missing or changed")
        facts.extend(items)
    return facts


def critical(run, gate_results, jobs):
    assessment = semantics.assessment_view(run)
    return {"lifecycle": semantics.lifecycle(run), "phase": run["phase"], "measurement_status": run["verification"]["status"],
            "gate_status": gate_results["status"], "assessment_status": assessment["status"],
            "termination_reason": run.get("termination_reason"), "deadline_at": run["deadline_at"],
            "actions": run["actions"], "limits": run["limits"],
            "open_question_count": len(run["interpretations"][-1]["open_questions"]) + len(run["domain_questions"]),
            "uncertainty_count": len(assessment["uncertainties"]),
            "unresolved_task_ids": [item["task_id"] for item in run["logical_tasks"] if item["state"] != "settled"],
            "unsatisfied_gate_ids": [item["check_id"] for item in gate_results["results"] if item["status"] != "passed"],
            "jobs": [{key: job.get(key) for key in ("job_id", "status", "completed_checks", "total_checks", "error")} for job in jobs],
            "assurance": "local-advisory", "ready": False}


def read(api, run_id, *, after=None, page=None, limit=20):
    limit = integer(limit, 1, 100, "limit")
    require(after is None or page is None, "CONTEXT_ARGUMENTS", "Use a cursor or a page token, not both")
    with api._read_run(run_id) as (connection, current):
        jobs, _, live_gates = read_core.summary(api, connection, current)
        latest = getattr(current, "head", None)
        if latest is None:
            # No migration or new verification is inferred for historical JSON.
            full = api.store.run(connection, run_id)
            texts = {}
            item = {"key": "legacy_run", "digest": canonical_hash(full), **pack(full, texts)}
            return {"schema_version": VERSION, "run_id": run_id, "delivery": "full", "offset": 0, "items": [item], "texts": texts,
                    "critical": critical(current, live_gates, jobs), "next_page": None, "next_cursor": None,
                    "reset_reason": "legacy_full_only", "snapshot": None}
        snapshot, base, offset, reset = latest, None, 0, None
        if page is not None:
            token = decode(page)
            target = resolve(connection, run_id, token.get("target")) if token else None
            prior = resolve(connection, run_id, token.get("base")) if token and token.get("base") else None
            valid = target and type(token.get("offset")) is int and token["offset"] >= 0 and (token.get("base") is None or prior)
            if valid and (prior is None or prior["seq"] <= target["seq"]):
                snapshot, base, offset = target, prior, token["offset"]
            else:
                reset = "invalid_page"
        elif after is not None:
            base = resolve(connection, run_id, decode(after))
            if base is None or base["seq"] > latest["seq"]:
                base, reset = None, "invalid_cursor"
        run = current if snapshot["seq"] == latest["seq"] else journal.load(connection, snapshot, current=True)
        if run is not current:
            semantics.validate_run(run, current=True)
        # Current polling must not scan historical measurements. Old immutable
        # page boundaries use their own facts, never a newer passing result.
        facts = None if snapshot["seq"] == latest["seq"] else facts_at(api, connection, snapshot)
        gates = live_gates if facts is None else semantics.gates(run, facts)
        selected_jobs = [journal.get(connection, run_id, ref) for ref in list(snapshot["jobs"].values())[-5:]]
        entries = inventory(connection, snapshot, base)
        if offset > len(entries):
            snapshot, base, offset, reset = latest, None, 0, "invalid_page"
            run, gates, selected_jobs = current, live_gates, jobs
            entries = inventory(connection, snapshot, None)
        items, texts = [], {}
        for key, ref in entries[offset:offset + limit]:
            value = journal.get(connection, run_id, ref)
            if key.startswith("measurements:"):
                rows = connection.execute("SELECT data FROM measurements WHERE run_id=? AND observation_id=? AND job_id=?",
                                          (run_id, value["observation_id"], value["job_id"]))
                measured = api.store._measurement_rows(rows, run_id)
                require(len(measured) == 1 and measured[0]["record_hash"] == value["record_hash"], "RESULT_BINDING", "Context measurement changed")
                value = measured[0]
            items.append({"key": key, "ref": ref, "digest": canonical_hash(value), **pack(value, texts)})
        end = offset + len(items)
        more = end < len(entries)
        target = cursor(snapshot)
        return {"schema_version": VERSION, "run_id": run_id, "delivery": "delta" if base else "full",
                "offset": offset, "items": items, "texts": texts, "snapshot": target,
                "critical": critical(current, live_gates, jobs),
                "snapshot_critical": critical(run, gates, selected_jobs) if snapshot["seq"] != latest["seq"] else None,
                "reset_reason": reset,
                "next_page": encode({"target": target, "base": cursor(base) if base else None, "offset": end}) if more else None,
                "next_cursor": None if more else encode(target)}
