"""Lossless, versioned read projections. References are not new observations."""
import copy
import hashlib

from harness.common import canonical_bytes, canonical_hash

SCHEMA = "record-projection-v1"
MAX_INLINE_BYTES = 16384
MAX_LEAF_BYTES = 1024
MAX_INLINE_ITEMS = 32


def project(value, source):
    refs = []
    def reference(item, path):
        encoded = canonical_bytes(item)
        refs.append({"path": path, "sha256": hashlib.sha256(encoded).hexdigest(), "byte_count": len(encoded), "encoding": "canonical-json"})
        return None
    def walk(item, path):
        if isinstance(item, str) and len(item.encode("utf-8")) > MAX_LEAF_BYTES:
            return reference(item, path)
        if isinstance(item, (dict, list)) and len(item) > MAX_INLINE_ITEMS:
            return reference(item, path)
        if isinstance(item, dict):
            return {key: walk(body, path + [key]) for key, body in item.items()}
        if isinstance(item, list):
            return [walk(body, path + [index]) for index, body in enumerate(item)]
        return item
    data = walk(value, [])
    result = {"schema_version": SCHEMA, "source": {**source, "record_digest": canonical_hash(value)},
              "data": data, "detail_refs": refs}
    if len(canonical_bytes(result)) > MAX_INLINE_BYTES:
        refs.clear()
        result["data"] = reference(value, [])
    return result


def restore(projection, details):
    """Consumer reference decoder; never mutates Core state or admits evidence."""
    from .errors import require
    require(projection.get("schema_version") == SCHEMA, "DETAIL_CORRUPT", "Unknown record projection")
    value = copy.deepcopy(projection["data"])
    for ref in projection["detail_refs"]:
        item = details[ref["sha256"]]
        require(canonical_hash(item) == ref["sha256"], "DETAIL_CORRUPT", "Detail digest mismatch")
        if not ref["path"]:
            value = copy.deepcopy(item)
        else:
            target = value
            for key in ref["path"][:-1]:
                target = target[key]
            target[ref["path"][-1]] = copy.deepcopy(item)
    require(canonical_hash(value) == projection["source"]["record_digest"], "DETAIL_CORRUPT", "Record projection changed")
    return value
