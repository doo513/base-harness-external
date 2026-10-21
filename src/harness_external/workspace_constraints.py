"""Explicit workspace identity invariants, not product-behavior checks.

No natural-language interpretation, Domain imports, command execution or policy inference.
Reads use the existing bounded/race-detecting snapshot primitive.
"""
import copy
import time
from pathlib import Path

from harness.json_codec import MAX_BYTES
from .errors import HarnessError, require, relative_path
from . import semantics
from .intent_sources import validate_references
from .operation_schema import WORKSPACE_CONSTRAINTS, validate_shape
from .snapshots import stream_file


def normalize(value, goal, sources):
    value = {"unchanged_files": []} if value is None else copy.deepcopy(value)
    validate_shape(value, WORKSPACE_CONSTRAINTS)
    paths = []
    for item in value["unchanged_files"]:
        paths.append(relative_path(item["path"]))
        validate_references(item["source_refs"], goal, sources)
    require(len({p.casefold() for p in paths}) == len(paths)
            and not any(b.startswith(a + "/") for a in paths for b in paths if a != b),
            "WORKSPACE_CONSTRAINTS_INVALID", "Unchanged files must be distinct explicit file paths")
    value["unchanged_files"].sort(key=lambda item: item["path"].encode("utf-8"))
    return value


def pin(declaration, workspace, run_id):
    entries, total = [], 0
    for item in declaration["unchanged_files"]:
        digest, size, executable = stream_file(Path(workspace), item["path"])
        total += size
        require(total <= MAX_BYTES, "WORKSPACE_CONSTRAINTS_LIMIT", "Protected files exceed snapshot byte limits")
        entries.append({**copy.deepcopy(item), "sha256": digest, "size": size, "executable": executable})
    pinned = semantics.record("workspace-constraints:" + run_id, 1, {"files": entries,
                               "meaning": "explicit_caller_constraint_not_authenticated_authority"})
    result = inspect(pinned, workspace)
    enforce(result)
    return pinned, result


def inspect(pinned, workspace):
    if not pinned:
        return {"status": "not_configured", "checked_at": None, "files": [], "meaning": "not_product_verification"}
    semantics.validate(pinned)
    results = []
    for expected in pinned["files"]:
        try:
            digest, size, executable = stream_file(Path(workspace), expected["path"])
            same = (digest, size, executable) == (expected["sha256"], expected["size"], expected["executable"])
            results.append({"path": expected["path"], "status": "unchanged" if same else "changed"})
        except (OSError, HarnessError) as error:
            results.append({"path": expected["path"], "status": "unavailable", "code": getattr(error, "code", "FILE_UNAVAILABLE")})
    return {"status": "satisfied" if all(item["status"] == "unchanged" for item in results) else "violated",
            "binding_ref": copy.deepcopy(pinned["ref"]), "checked_at": time.time(), "files": results,
            "meaning": "observed_at_boundary_not_edit_prevention_or_product_verification"}


def enforce(result):
    if result["status"] == "violated":
        changed = [item for item in result["files"] if item["status"] != "unchanged"]
        raise HarnessError("WORKSPACE_CONSTRAINT_VIOLATION", "Pinned workspace files changed or could not be read",
                           diagnostics=[{"path": "/workspace_constraints/unchanged_files/" + item["path"],
                                         "code": item.get("code", "FILE_CHANGED"), "message": item["status"]} for item in changed[:64]],
                           diagnostic_count=len(changed))


def view(run):
    pinned = run.get("workspace_constraints")
    if not pinned:
        return {"status": "not_configured", "checked_at": None, "files": [], "meaning": "not_product_verification"}
    semantics.validate(pinned)
    return {**copy.deepcopy(run.get("workspace_constraints_check", {"status": "not_checked", "checked_at": None})),
            "configured_files": [item["path"] for item in pinned["files"]], "binding_ref": copy.deepcopy(pinned["ref"]),
            "freshness": "last_explicit_boundary_check_not_continuous_monitoring"}
