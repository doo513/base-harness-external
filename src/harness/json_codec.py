"""Shared message representation, separate from schemas and stored identities.

Only JSON is currently supported. Message framing and size limits remain with
each transport; canonical storage/hashing does not select a presentation format.
"""
import json

MAX_BYTES = 10 * 1024 * 1024
MAX_JSON_DEPTH = 64


class MeasurementProtocolError(ValueError):
    pass


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result or key in {"__proto__", "constructor", "prototype"}:
            raise MeasurementProtocolError("duplicate or reserved key")
        result[key] = value
    return result


def decode(value):
    require_text = value.decode("utf-8") if isinstance(value, bytes) else value
    if not isinstance(require_text, str):
        raise MeasurementProtocolError("JSON text required")
    depth = 0
    quoted = escaped = False
    for character in require_text:
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
            continue
        if character == '"':
            quoted = True
        elif character in "[{":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise MeasurementProtocolError("JSON nesting exceeds 64")
        elif character in "]}":
            depth -= 1
    def reject_constant(value):
        raise MeasurementProtocolError("nonfinite JSON number")
    try:
        return json.loads(require_text, object_pairs_hook=_pairs, parse_constant=reject_constant)
    except RecursionError as error:
        raise MeasurementProtocolError("JSON nesting exceeds decoder capacity") from error


def parse_message(raw, *, format="application/json"):
    """Decode a wire representation; the receiving boundary validates its fields."""
    if format != "application/json":
        raise MeasurementProtocolError("Unsupported message format")
    return decode(raw)


def render_message(value, *, format="application/json"):
    """Encode an internal value without granting it authority or adding fields.

    Keep the current JSON bytes for compatibility. Future presentation formats
    belong here, not in canonical_hash or persisted record serialization.
    """
    if format != "application/json":
        raise MeasurementProtocolError("Unsupported message format")
    from .common import canonical_bytes
    return canonical_bytes(value)
