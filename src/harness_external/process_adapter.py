"""Observe a Candidate process from the trusted side of the Sandbox boundary.

The subject receives argv, never predicates, observer tokens or a reporting API.
Each case is an explicit assertion on captured output, not a claimed unit test.
"""
import copy
from datetime import datetime

from harness.common import canonical_bytes
from harness.json_codec import decode
from .errors import fields, integer, relative_path, require


class ProcessObservationAdapter:
    adapter_id = "process-observations-v1"
    revision = "1"
    # Required by the compatibility port; the external route never mounts or
    # reads an observation artifact from the Candidate.
    report_path = ".harness-external-unused"

    def normalize_selection(self, selector, inputs, required_cases):
        fields(selector, {"source_paths", "argv", "cwd", "cases"}, {"source_paths", "argv", "cases"})
        paths, argv, cases = selector["source_paths"], selector["argv"], selector["cases"]
        require(isinstance(paths, list) and 1 <= len(paths) <= 128
                and all(isinstance(path, str) for path in paths) and len(set(paths)) == len(paths),
                "PROCESS_SCOPE", "Declare the input files whose process behavior is observed")
        for path in paths:
            relative_path(path)
            require(path in inputs, "PROCESS_SCOPE", "Observed source is outside the submitted input scope")
        require(isinstance(argv, list) and 1 <= len(argv) <= 64
                and all(isinstance(arg, str) and len(arg) <= 8192 and "\x00" not in arg for arg in argv)
                and bool(argv[0].strip()) and len(canonical_bytes(argv)) <= 64000,
                "PROCESS_COMMAND", "Use a bounded argv array, not a shell string")
        cwd = relative_path(selector.get("cwd", "."), directory=True)
        require(isinstance(cases, list) and 1 <= len(cases) <= 128, "PROCESS_CASES", "Declare 1..128 output observations")
        identifiers = set()
        for case in cases:
            fields(case, {"id", "stream", "operator", "expected", "json_path"}, {"id", "stream", "operator", "expected"})
            key = case["id"]
            require(isinstance(key, str) and 0 < len(key.encode("utf-8")) <= 4096
                    and "\x00" not in key and "\n" not in key and key not in identifiers,
                    "PROCESS_CASE_ID", "Observation IDs must be bounded and unique")
            identifiers.add(key)
            require(isinstance(case["stream"], str) and case["stream"] in {"stdout", "stderr"}
                    and isinstance(case["operator"], str) and case["operator"] in {"equals", "contains", "json_equals"},
                    "PROCESS_PREDICATE", "Use stdout/stderr and equals, contains or json_equals")
            require(len(canonical_bytes(case["expected"])) <= 64000, "PROCESS_PREDICATE", "Expected output exceeds 64 KiB")
            require(case["operator"] == "json_equals" or isinstance(case["expected"], str),
                    "PROCESS_PREDICATE", "Text predicates require a string expectation")
            require(case["operator"] != "contains" or bool(case["expected"]), "PROCESS_PREDICATE", "Empty contains predicates are vacuous")
            projection = case.get("json_path", [])
            require(isinstance(projection, list) and len(projection) <= 16
                    and all(isinstance(part, str) and len(part) <= 512 or type(part) is int and 0 <= part <= 100000 for part in projection)
                    and ("json_path" not in case or case["operator"] == "json_equals"),
                    "PROCESS_PREDICATE", "json_path is a bounded sequence of object keys or nonnegative array indexes for json_equals")
        require(set(required_cases) <= identifiers, "PROCESS_CASE_ID", "Required observation is not declared by this selector")
        normalized = {"source_paths": list(paths), "argv": list(argv), "cwd": cwd, "cases": copy.deepcopy(cases)}
        return {"selector": normalized, "source_paths": list(paths), "criteria_source": "selector"}

    def validate_reference(self, selector, reference):
        require(reference["path"] in selector["source_paths"]
                and reference["case_id"] in {case["id"] for case in selector["cases"]}
                and reference.get("framework", "process-observations") == "process-observations",
                "PROCESS_REFERENCE", "Reference must name an observed source and declared output observation")

    def runtime(self, state_root, pinned=None):
        value = {"schema_version": "process-observer-runtime-v1", "observation_boundary": "external_capture"}
        require(pinned is None or pinned == value, "ADAPTER_RUNTIME_CHANGED", "External observer runtime differs")
        return value

    def command(self, check):
        selector = check["adapter"]["selector"]
        return {"kind": "command", "argv": list(selector["argv"]), "cwd": selector["cwd"],
                "timeout_seconds": integer(check.get("timeout_seconds", 30), 1, 120, "timeout_seconds"), "expectedExitCode": 0}

    def stage(self, *args):
        raise RuntimeError("External observers cannot stage reporting resources into a Candidate")

    def observer(self, *args):
        raise RuntimeError("External observers cannot consume Candidate-supplied case events")

    def observe_capture(self, capture, check):
        # This is called by the common transport only AFTER receipt validation.
        receipt = capture.get("capture") or {}
        executed = receipt.get("execution") in {"completed", "error"}
        completed = capture.get("status") == "completed" and receipt.get("execution") == "completed"
        duration = 0.0
        if executed:
            duration = max(0.0, (datetime.fromisoformat(receipt["finishedAt"].replace("Z", "+00:00"))
                                 - datetime.fromisoformat(receipt["startedAt"].replace("Z", "+00:00"))).total_seconds())
        observed = []
        for declaration in check["adapter"]["selector"]["cases"]:
            outcome, message = "not_run", capture.get("reason")
            if completed:
                actual, expected = receipt[declaration["stream"]], declaration["expected"]
                try:
                    if declaration["operator"] == "json_equals":
                        projected = decode(actual)
                        for part in declaration.get("json_path", []):
                            if type(part) is int and isinstance(projected, list) or isinstance(part, str) and isinstance(projected, dict):
                                projected = projected[part]
                            else:
                                raise KeyError("Projection does not address this JSON value")
                        passed = canonical_bytes(projected) == canonical_bytes(expected)
                    else:
                        passed = expected in actual if declaration["operator"] == "contains" else actual == expected
                    outcome = "passed" if passed else "failed"
                    message = None if passed else (declaration["stream"] + " " + declaration["operator"] +
                        " failed; expected " + repr(expected)[:240] + "; observed " + repr(actual)[:240])
                except (ValueError, UnicodeError, KeyError, IndexError):
                    outcome, message = "failed", "Captured output was not unambiguous JSON with the declared projection"
            elif executed:
                outcome, message = "error", (receipt.get("error") or {}).get("message", "Process did not finish normally")
            observed.append({"framework": "process-observations", "case_id": declaration["id"],
                             "discovered": True, "selected": True, "started": executed, "executed": executed,
                             "finished": completed, "outcome": outcome, "duration_seconds": duration,
                             "phases": [{"phase": "external_observation", "outcome": outcome,
                                         "message": message, "duration": duration}]})
        return {"framework": "process-observations", "collection_complete": True, "session_complete": completed,
                "discovery_source": "pinned_predicate_declarations", "cases": observed,
                "observation_boundary": "external_capture"}
