"""Bounded caller-supplied source text, pinned by Core without interpreting it."""
import hashlib
import re

from .errors import fields, require, integer


def validate_references(refs, goal, sources):
    """Identity/range checks only; a line reference is not approval or interpretation."""
    require(isinstance(refs, list) and len(refs) <= 16, "REQUIREMENT_SOURCE", "At most sixteen source references")
    texts = {"goal": goal, **{item["id"]: item["text"] for item in sources}}
    for ref in refs:
        fields(ref, {"source_id", "start_line", "end_line"}, {"source_id", "start_line", "end_line"})
        key = ref["source_id"]
        require(isinstance(key, str) and key in texts, "REQUIREMENT_SOURCE", "Unknown original source")
        count = len(texts[key].splitlines())
        begin = integer(ref["start_line"], 1, max(1, count), "start_line")
        integer(ref["end_line"], begin, count, "end_line")
    return refs


def normalize(values):
    require(isinstance(values, list) and len(values) <= 16, "INTENT_SOURCES", "Provide at most sixteen original source documents")
    result, seen, total = [], set(), 0
    for value in values:
        fields(value, {"id", "label", "text"}, {"id", "text"})
        key, text = value["id"], value["text"]
        require(isinstance(key, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", key)
                and key != "goal" and key not in seen, "INTENT_SOURCE_ID", "Source IDs must be unique; goal is reserved")
        label = value.get("label", key)
        require(isinstance(label, str) and 0 < len(label) <= 512 and isinstance(text, str) and bool(text.strip()),
                "INTENT_SOURCES", "Source label and original text are required")
        try:
            encoded = text.encode("utf-8")
        except UnicodeError:
            require(False, "INTENT_SOURCES", "Original source text must be valid UTF-8")
        total += len(encoded)
        require(len(encoded) <= 64000 and total <= 128000, "INTENT_SOURCE_LIMIT", "Sources exceed the 64 KB document / 128 KB total bound")
        seen.add(key)
        result.append({"id": key, "label": label, "text": text, "sha256": hashlib.sha256(encoded).hexdigest()})
    return result
