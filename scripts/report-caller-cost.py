#!/usr/bin/env python3
"""Summarize local caller costs, with optional separately supplied Host usage."""
import argparse
from collections import Counter
import json
from pathlib import Path


def summarize(events, *, run_id=None, model_usage=None):
    rows = []
    for event in events:
        if not isinstance(event, dict) or event.get("schema_version") != "harness-client-cost-v1" or event.get("kind") not in {"transport", "helper"}:
            raise ValueError("Unsupported cost trace row")
        if run_id and not (event.get("run_id") == run_id or run_id in event.get("run_ids", [])):
            continue
        if type(event.get("elapsed_ms")) not in (int, float) or not 0 <= event["elapsed_ms"] < 10**9:
            raise ValueError("Invalid measured elapsed time")
        rows.append(event)
    transports = [item for item in rows if item["kind"] == "transport"]
    helpers = [item for item in rows if item["kind"] == "helper"]
    ids = Counter((item.get("run_id"), item["request_id"]) for item in transports if item.get("request_id"))
    response_complete = all(item.get("response_bytes") is not None for item in transports)
    if model_usage is not None:
        allowed = {"source", "run_id", "model", "input_tokens", "output_tokens", "cache_read_tokens", "elapsed_seconds"}
        if not isinstance(model_usage, dict) or not set(model_usage) <= allowed or not {"source", "run_id", "model"} <= model_usage.keys():
            raise ValueError("Host usage needs source, run_id and model; only normalized token/time counters are accepted")
        known_runs = {item.get("run_id") for item in transports if item.get("run_id")}
        if not all(isinstance(model_usage[key], str) and model_usage[key] for key in ("source", "run_id", "model")) or model_usage["run_id"] not in known_runs:
            raise ValueError("Host usage does not identify a Run in the selected trace")
        for key in ("input_tokens", "output_tokens", "cache_read_tokens"):
            if model_usage.get(key) is not None and (type(model_usage[key]) is not int or model_usage[key] < 0):
                raise ValueError("Host token counters must be nonnegative integers or null")
        duration = model_usage.get("elapsed_seconds")
        if duration is not None and (type(duration) not in (int, float) or not 0 <= duration < 10**9):
            raise ValueError("Invalid Host duration")
    return {"schema_version": "harness-caller-cost-summary-v1", "run_id": run_id,
            "helper_invocations": len(helpers), "transport_calls": len(transports),
            "calls_by_operation": dict(Counter(item["operation"] for item in transports)),
            "error_codes": dict(Counter(item["error_code"] for item in transports if item.get("error_code"))),
            "helper_error_codes": dict(Counter(item["error_code"] for item in helpers if item.get("error_code"))),
            "repeated_request_id_transmissions": sum(count - 1 for count in ids.values()),
            "unknown_outcomes": sum(item.get("ok") is None for item in transports),
            "helper_elapsed_ms": round(sum(item["elapsed_ms"] for item in helpers), 3),
            "transport_elapsed_ms": round(sum(item["elapsed_ms"] for item in transports), 3),
            "observed_response_bytes": sum(item.get("response_bytes") or 0 for item in transports),
            "response_bytes_complete": response_complete,
            "host_reported_model_usage": model_usage, "billed_tokens": None,
            "limitations": ["Transport time is included in helper time; concurrent durations are not wall-clock totals.",
                            "Unknown or lost responses are not zero-cost or proof of failed mutations.",
                            "Bytes are canonical JSON size, not token usage. Host counters are supplied claims, not billing verification."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--model-usage", type=Path, help="optional normalized Host usage JSON with source/run_id/model and token counters")
    args = parser.parse_args()
    try:
        with args.trace.open(encoding="utf-8") as stream:
            events = [json.loads(line) for line in stream if line.strip()]
        usage = json.loads(args.model_usage.read_text(encoding="utf-8")) if args.model_usage else None
        print(json.dumps(summarize(events, run_id=args.run_id, model_usage=usage), ensure_ascii=False, allow_nan=False))
        return 0
    except (OSError, ValueError, TypeError, KeyError) as error:
        print(json.dumps({"ok": False, "error": str(error)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
