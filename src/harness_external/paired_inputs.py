"""Generic immutable composition of two subjects with identical shared inputs.

No test/framework semantics. Staging remains private until the queue transaction
publishes it; failed publication restores only these newly created views.
"""
import copy
from contextlib import contextmanager
import os
from pathlib import Path
import tempfile

from harness.common import canonical_hash
from .errors import fields, require, relative_path
from .snapshots import capture, validate_candidate
from .store import identifier
from .maintenance import capture_lease


def selection(value, checks, candidate):
    fields(value, {"check_ids", "shared_inputs"}, {"check_ids", "shared_inputs"})
    for key in ("check_ids", "shared_inputs"):
        require(isinstance(value[key], list) and 1 <= len(value[key]) <= 128
                and all(isinstance(item, str) for item in value[key]) and len(set(value[key])) == len(value[key]),
                "COMPARISON_PREPARATION", "Domain must return bounded unique references")
    require(set(value["check_ids"]) <= {item["check_id"] for item in checks}, "COMPARISON_PREPARATION", "Domain selected an unregistered Check")
    paths = {item["path"] for item in candidate["manifest"]["files"]}
    for path in value["shared_inputs"]:
        relative_path(path)
        require(path in paths, "COMPARISON_SCOPE", "Shared input is absent from the submitted Candidate")
    return copy.deepcopy(value)


@contextmanager
def prepare(run, selected, directory):
    directory = Path(directory)
    original, current = run["baseline"], run["candidate"]
    original_payload = directory / identifier(original["candidate_id"], "candidate") / "payload"
    current_payload = directory / identifier(current["candidate_id"], "candidate") / "payload"
    validate_candidate(original, original_payload)
    validate_candidate(current, current_payload)
    shared = sorted(selected["shared_inputs"])
    entries = [{**entry, "executable": current["executable"][entry["path"]]}
               for entry in current["manifest"]["files"] if entry["path"] in shared]
    with tempfile.TemporaryDirectory(prefix=".compare_", dir=directory) as temporary, capture_lease(temporary):
        staging = Path(temporary)
        overlay = {path: current_payload for path in shared}
        before = capture(original_payload, sorted({item["path"] for item in original["manifest"]["files"]} | set(shared)), staging, overlays=overlay)
        after = capture(current_payload, [item["path"] for item in current["manifest"]["files"]], staging)
        validate_candidate(original, original_payload)
        validate_candidate(current, current_payload)
        binding = {"schema_version": "paired-inputs-v1", "reference": copy.deepcopy(original), "current": copy.deepcopy(current),
                   "shared_inputs": entries, "shared_input_hash": canonical_hash(entries),
                   "before": before, "after": after}
        binding["binding_hash"] = canonical_hash(binding)
        pair = {"binding": binding, "staging": staging, "published": [], "committed": False}
        try:
            yield pair
        finally:
            if not pair["committed"]:
                for target in pair["published"]:
                    os.rename(target, staging / target.name)


def publish(pair, directory):
    for key in ("before", "after"):
        name = pair["binding"][key]["candidate_id"]
        target = Path(directory) / identifier(name, "candidate")
        os.rename(pair["staging"] / name, target)
        pair["published"].append(target)


def validate(job):
    binding = job.get("comparison_binding")
    require(isinstance(binding, dict) and binding.get("binding_hash") == canonical_hash({k: v for k, v in binding.items() if k != "binding_hash"}),
            "COMPARISON_BINDING", "Paired input binding changed")
    require(job.get("purpose") == "diagnostic" and job["baseline"] == binding["before"] and job["candidate"] == binding["after"]
            and binding["shared_input_hash"] == canonical_hash(binding["shared_inputs"]), "COMPARISON_BINDING", "Diagnostic subjects do not match their composition")
    shared = {entry["path"]: entry for entry in binding["shared_inputs"]}
    for key, origin in (("before", "reference"), ("after", "current")):
        actual = {entry["path"]: {**entry, "executable": binding[key]["executable"][entry["path"]]} for entry in binding[key]["manifest"]["files"]}
        expected = {entry["path"]: {**entry, "executable": binding[origin]["executable"][entry["path"]]} for entry in binding[origin]["manifest"]["files"]}
        expected.update(shared)
        require(actual == expected, "COMPARISON_BINDING", "Composed bytes differ outside the pinned shared scope")
