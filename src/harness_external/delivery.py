"""Lossless caller payloads and exact, same-Run record bindings.

No inference, execution, new evidence or second ledger: values come from the
existing records reader and all destination operations keep their own checks.
"""
import copy
import json
import math
import re

from harness.common import canonical_hash
from .errors import fields, require


REFERENCE_KEYS = {"run_id", "kind", "record_id", "record_digest", "path", "sha256"}
KINDS = {"checks", "interpretations", "activity", "assessments", "tasks", "notes", "needs", "measurements", "cases", "jobs"}
# Only content can be copied. Routing, revisions, approval, citations and outcome
# decisions are explicit operation inputs, never inferred from another field.
TARGETS = {
    "observe": (("data", "note"), ("data", "payload", "fields"), ("data", "payload", "extensions")),
    "revise": (("data", "goal_summary"), ("data", "parameters"), ("data", "assumptions"), ("data", "open_questions")),
    "check": (("data", "parameters"),),
    "assess": (("data", "summary"), ("data", "uncertainties")),
    "finish": (("summary",), ("assessment", "summary"), ("assessment", "uncertainties")),
}


def json_value(value):
    """Bound accepted JSON without redaction, truncation or coercion."""
    count = 0
    def walk(item, depth):
        nonlocal count
        count += 1
        require(depth <= 32 and count <= 4096, "PAYLOAD_LIMIT", "Payload nesting/item limit exceeded")
        require(item is None or type(item) in {str, bool, int, float, list, dict}, "PAYLOAD_TYPE", "Payload must contain JSON values")
        if type(item) is float:
            require(math.isfinite(item), "PAYLOAD_TYPE", "Non-finite numbers are not JSON values")
        if isinstance(item, dict):
            require(all(isinstance(key, str) for key in item), "PAYLOAD_TYPE", "Payload keys must be strings")
            for child in item.values():
                walk(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                walk(child, depth + 1)
    walk(value, 0)
    try:
        size = len(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8"))
    except (ValueError, UnicodeError) as error:
        require(False, "PAYLOAD_TYPE", "Payload must be finite UTF-8 JSON: " + type(error).__name__)
    require(size <= 64000, "PAYLOAD_LIMIT", "Payload exceeds 64000 UTF-8 bytes")
    return copy.deepcopy(value)


def reference(value):
    fields(value, REFERENCE_KEYS, REFERENCE_KEYS)
    require(isinstance(value["run_id"], str) and re.fullmatch(r"run_[a-f0-9]{32}", value["run_id"]),
            "DELIVERY_REFERENCE", "Use an exact Run ID")
    require(isinstance(value["kind"], str) and value["kind"] in KINDS, "DELIVERY_REFERENCE", "Unknown record kind")
    require(type(value["record_id"]) in {str, int} and not isinstance(value["record_id"], bool), "DELIVERY_REFERENCE", "Invalid record identity")
    for name in ("record_digest", "sha256"):
        require(isinstance(value[name], str) and re.fullmatch(r"[a-f0-9]{64}", value[name]), "DELIVERY_REFERENCE", "Use exact record/content digests")
    path(value["path"])
    return value


def path(value):
    require(isinstance(value, list) and len(value) <= 64 and
            all(isinstance(key, str) or type(key) is int and key >= 0 for key in value),
            "DELIVERY_PATH", "Use a bounded JSON field/index path")


def at(value, keys):
    path(keys)
    for key in keys:
        require(isinstance(value, dict) and isinstance(key, str) and key in value or
                isinstance(value, list) and type(key) is int and 0 <= key < len(value),
                "DELIVERY_PATH", "Referenced JSON field/index is missing")
        value = value[key]
    return value


def resolve(api, connection, run, ref):
    from .read_core import record_value
    reference(ref)
    require(ref["run_id"] == run["run_id"], "DELIVERY_RUN_MISMATCH", "Reference belongs to another Run")
    value = record_value(api, connection, run, ref["kind"], ref["record_id"])
    require(canonical_hash(value) == ref["record_digest"], "DETAIL_CHANGED", "Referenced record changed")
    value = at(value, ref["path"])
    require(canonical_hash(value) == ref["sha256"], "DETAIL_CHANGED", "Referenced field changed")
    return value


def payload(value):
    fields(value, {"fields", "extensions", "parents"})
    result = json_value({"fields": value.get("fields", {}), "extensions": value.get("extensions", {}), "parents": value.get("parents", [])})
    require(isinstance(result["fields"], dict) and isinstance(result["extensions"], dict), "PAYLOAD_TYPE", "fields and extensions must be objects")
    require(len(result["fields"]) <= 64 and len(result["extensions"]) <= 64, "PAYLOAD_LIMIT", "At most 64 fields/extensions")
    require(isinstance(result["parents"], list) and len(result["parents"]) <= 32, "DELIVERY_REFERENCE", "At most 32 parent references")
    for item in result["parents"]:
        reference(item)
    return result


def binding_shape(operation, bindings):
    require(operation in TARGETS and isinstance(bindings, list) and 1 <= len(bindings) <= 32,
            "DELIVERY_TARGET", "Record bindings require a content-writing operation and 1..32 sources")
    seen = []
    for binding in bindings:
        fields(binding, {"target", "source"}, {"target", "source"})
        target = binding["target"]
        path(target)
        require(any(tuple(target[:len(prefix)]) == prefix for prefix in TARGETS[operation]),
                "DELIVERY_TARGET", "Only content fields can reuse prior records; controls/outcomes remain explicit")
        require(not any(target[:len(old)] == old or old[:len(target)] == target for old in seen),
                "DELIVERY_TARGET", "Input binding targets overlap")
        seen.append(target)
        reference(binding["source"])
    return copy.deepcopy(bindings)


def validate_bindings(api, connection, run, arguments, bindings):
    for binding in bindings:
        value = resolve(api, connection, run, binding["source"])
        require(canonical_hash(at(arguments, binding["target"])) == canonical_hash(value),
                "DELIVERY_VALUE_MISMATCH", "Argument differs from its referenced value; submit an explicit new claim instead")


def ref(run_id, kind, record_id, record, keys=()):
    return {"run_id": run_id, "kind": kind, "record_id": record_id, "record_digest": canonical_hash(record),
            "path": list(keys), "sha256": canonical_hash(at(record, list(keys)))}
