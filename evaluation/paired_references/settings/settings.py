"""Evaluator positive control; not exposed to solving agents."""
import copy
import math


def json_value(value):
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for child in value:
            json_value(child)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for child in value.values():
            json_value(child)
        return
    raise ValueError("invalid JSON value")


def normalize(document):
    json_value(document)
    if type(document) is not dict or type(document.get("version")) is not int or document["version"] not in (1, 2):
        raise ValueError("version")
    labels = document.get("labels", {})
    if type(labels) is not dict or not all(type(k) is str and type(v) is str for k, v in labels.items()):
        raise ValueError("labels")
    if document["version"] == 1:
        milliseconds, retries = document.get("timeout_ms", 1000), document.get("retries", 0)
        if type(milliseconds) is not int or milliseconds < 0 or type(retries) is not int or retries < 0:
            raise ValueError("legacy limits")
        seconds, attempts = milliseconds / 1000, retries + 1
        extensions = {k: v for k, v in document.items() if k not in {"version", "timeout_ms", "retries", "labels"}}
    else:
        if set(document) - {"version", "timeout_seconds", "attempts", "labels", "extensions"}:
            raise ValueError("unknown keys")
        seconds, attempts, extensions = document.get("timeout_seconds", 1.0), document.get("attempts", 1), document.get("extensions", {})
        if type(seconds) not in (int, float) or seconds < 0 or type(attempts) is not int or attempts < 1 or type(extensions) is not dict:
            raise ValueError("limits")
    return copy.deepcopy({"version": 2, "timeout_seconds": seconds, "attempts": attempts, "labels": labels, "extensions": extensions})
