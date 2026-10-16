"""Framework-neutral identity for an individual Sandbox invocation.

Build expectations from pinned Job metadata, never by blessing a fresh read of
the mutable source path. Receipts describe local measurements, not attestation.
"""
import copy
from datetime import datetime
import re

from harness.common import canonical_bytes, canonical_hash
from harness.json_codec import MAX_BYTES
from .errors import fields, relative_path, require


def make_binding(files, scope):
    ordered = sorted(copy.deepcopy(files), key=lambda item: item["path"].encode("utf-8"))
    result = {"schema_version": "execution-input-v1", "scope_hash": canonical_hash(scope),
              "input_hash": canonical_hash(ordered), "files": ordered}
    return validate_binding(result)


def validate_binding(value):
    require(isinstance(value, dict), "EXECUTION_INPUT_REQUIRED", "Pinned execution input binding is required")
    fields(value, {"schema_version", "scope_hash", "input_hash", "files"},
           {"schema_version", "scope_hash", "input_hash", "files"})
    require(value["schema_version"] == "execution-input-v1"
            and all(isinstance(value[key], str) and re.fullmatch(r"[0-9a-f]{64}", value[key]) for key in ("scope_hash", "input_hash")),
            "EXECUTION_INPUT_INVALID", "Invalid execution input identity")
    entries = value["files"]
    require(isinstance(entries, list) and len(entries) <= 128, "EXECUTION_INPUT_INVALID", "Input inventory exceeds bounds")
    total, paths = 0, []
    for entry in entries:
        fields(entry, {"path", "sha256", "size", "executable"}, {"path", "sha256", "size", "executable"})
        paths.append(relative_path(entry["path"]))
        require(type(entry["size"]) is int and 0 <= entry["size"] <= MAX_BYTES and type(entry["executable"]) is bool
                and isinstance(entry["sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]),
                "EXECUTION_INPUT_INVALID", "Invalid input file metadata")
        total += entry["size"]
    require(total <= MAX_BYTES and len(set(paths)) == len(paths) and paths == sorted(paths, key=lambda p: p.encode("utf-8"))
            and value["input_hash"] == canonical_hash(entries) and len(canonical_bytes(value)) <= 512 * 1024,
            "EXECUTION_INPUT_INVALID", "Input inventory identity or bounds mismatch")
    return copy.deepcopy(value)


def job_binding(candidate, run_id, job_id, owner, check_ref, execution):
    return make_binding([{**entry, "executable": candidate["executable"][entry["path"]]}
                         for entry in candidate["manifest"]["files"]],
                        {"run_id": run_id, "job_id": job_id, "owner": owner, "check_ref": check_ref,
                         "subject": candidate["subject"], "candidate_hash": candidate["candidate_hash"],
                         "command": {key: execution[key] for key in ("argv", "cwd", "timeout_seconds")},
                         **({"scenario": {key: execution[key] for key in ("fixtures", "observed_files", "expectedExitCode") if key in execution}}
                            if "fixtures" in execution or "observed_files" in execution or execution.get("expectedExitCode", 0) else {})})


def validate_receipt(capture, binding):
    """No success from a missing/foreign receipt, even when process exit is zero."""
    validate_binding(binding)
    if not capture.get("capture"):
        require(capture.get("status") in {"not_run", "error"}, "RECEIPT_BINDING", "Execution receipt is missing")
        return
    receipt = capture.get("input_receipt")
    require(isinstance(receipt, dict) and set(receipt) == {
        "schema_version", "scope_hash", "input_hash", "checked_at", "post_checked_at", "unchanged", "changed_inputs"},
        "RECEIPT_BINDING", "Pinned execution input receipt is missing or malformed")
    require(receipt["schema_version"] == "execution-input-receipt-v1"
            and all(receipt[key] == binding[key] for key in ("scope_hash", "input_hash"))
            and all(isinstance(receipt[key], str) and receipt[key] for key in ("checked_at", "post_checked_at"))
            and type(receipt["unchanged"]) is bool and isinstance(receipt["changed_inputs"], list)
            and all(isinstance(item, str) for item in receipt["changed_inputs"])
            and len(set(receipt["changed_inputs"])) == len(receipt["changed_inputs"])
            and set(receipt["changed_inputs"]) <= {item["path"] for item in binding["files"]}
            and receipt["unchanged"] == (not receipt["changed_inputs"]),
            "RECEIPT_BINDING", "Execution receipt does not belong to the pinned input/invocation")
    try:
        before = datetime.fromisoformat(receipt["checked_at"].replace("Z", "+00:00"))
        after = datetime.fromisoformat(receipt["post_checked_at"].replace("Z", "+00:00"))
        require(before.tzinfo is not None and after.tzinfo is not None and before <= after,
                "RECEIPT_BINDING", "Invalid execution input observation interval")
    except ValueError:
        require(False, "RECEIPT_BINDING", "Invalid execution input observation timestamps")
    require(receipt["unchanged"] or (capture["status"] == "error" and capture["capture"].get("execution") == "error"),
            "RECEIPT_BINDING", "Changed submitted inputs cannot yield a successful execution")
