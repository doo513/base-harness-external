"""Core source normalization with an internal, format-specific parsing boundary.

Public sources remain original UTF-8 text with the existing line references.
Parsed content is an internal view, never a replacement for the pinned original.
"""
from dataclasses import dataclass
import hashlib
import re

from .errors import fields, require, integer


@dataclass(frozen=True)
class SourceDocument:
    """Internal value shared by parsing and source normalization; not a DB schema."""

    format: str
    original_text: str
    content: str
    sha256: str
    size_bytes: int


def parse_document(format: str, raw: str) -> str:
    """Extract a content view. Only literal text is supported at this stage.

    File extensions do not select a parser. Future formats belong here; this
    function must not infer requirements, execute markup, or alter the original.
    """
    require(format == "text/plain", "INTENT_SOURCE_FORMAT", "Only text/plain source parsing is supported")
    return raw


def normalize_document(text: str, *, format: str = "text/plain") -> SourceDocument:
    """Validate one original, bind its bytes, then obtain its content view."""
    require(isinstance(text, str) and bool(text.strip()), "INTENT_SOURCES", "Original source text is required")
    try:
        encoded = text.encode("utf-8")
    except UnicodeError:
        require(False, "INTENT_SOURCES", "Original source text must be valid UTF-8")
    require(len(encoded) <= 64000, "INTENT_SOURCE_LIMIT", "Sources exceed the 64 KB document / 128 KB total bound")
    return SourceDocument(format=format, original_text=text, content=parse_document(format, text),
                          sha256=hashlib.sha256(encoded).hexdigest(), size_bytes=len(encoded))


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
    """Validate the source collection and preserve its existing public records."""
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
        document = normalize_document(text)
        total += document.size_bytes
        require(total <= 128000, "INTENT_SOURCE_LIMIT", "Sources exceed the 64 KB document / 128 KB total bound")
        seen.add(key)
        result.append({"id": key, "label": label, "text": document.original_text, "sha256": document.sha256})
    return result
