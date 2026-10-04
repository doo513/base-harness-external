"""Shared strict JSON transport; importing it does not load a measurement engine."""
import json

MAX_BYTES = 10 * 1024 * 1024


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
    def reject_constant(value):
        raise MeasurementProtocolError("nonfinite JSON number")
    return json.loads(value, object_pairs_hook=_pairs, parse_constant=reject_constant)
