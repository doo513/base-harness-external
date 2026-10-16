"""Observe a Candidate process from the trusted side of the Sandbox boundary.

The subject receives argv, never predicates, observer tokens or a reporting API.
Each case is an explicit assertion on captured output, not a claimed unit test.
"""
import copy
import base64
import hashlib
import re
from datetime import datetime

from harness.common import canonical_bytes
from harness.json_codec import decode
from .errors import fields, integer, relative_path, require


class ProcessObservationAdapter:
    adapter_id = "process-observations-v1"
    revision = "2"
    # Required by the compatibility port; the external route never mounts or
    # reads an observation artifact from the Candidate.
    report_path = ".harness-external-unused"

    def normalize_selection(self, selector, inputs, required_cases):
        fields(selector, {"source_paths", "argv", "cwd", "cases", "fixtures", "expected_exit_code"}, {"source_paths", "argv", "cases"})
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
        integer(selector.get("expected_exit_code", 0), 0, 255, "expected_exit_code")
        fixtures = selector.get("fixtures", [])
        require(isinstance(fixtures, list) and len(fixtures) <= 32, "PROCESS_FIXTURES", "Declare at most 32 disposable fixture files")
        reserved = list(inputs)
        total = 0
        for fixture in fixtures:
            fields(fixture, {"path", "text"}, {"path", "text"})
            name = relative_path(fixture["path"])
            require(isinstance(fixture["text"], str), "PROCESS_FIXTURES", "Fixture content must be UTF-8 text")
            try:
                size = len(fixture["text"].encode("utf-8"))
            except UnicodeError:
                require(False, "PROCESS_FIXTURES", "Fixture content must be valid UTF-8")
            total += size
            require(size <= 64000 and total <= 128000, "PROCESS_FIXTURES", "Fixture content exceeds bounds")
            folded = name.casefold()
            require(not any(folded == item.casefold() or folded.startswith(item.casefold() + "/")
                            or item.casefold().startswith(folded + "/") for item in reserved),
                    "PROCESS_FIXTURES", "Fixture path overlaps another fixture or submitted input")
            reserved.append(name)
        require(isinstance(cases, list) and 1 <= len(cases) <= 128, "PROCESS_CASES", "Declare 1..128 output observations")
        identifiers = set()
        observed_paths = set()
        for case in cases:
            is_file = case.get("kind") == "file" if isinstance(case, dict) else False
            fields(case, {"kind", "id", "path", "operator", "expected"} if is_file else {"id", "stream", "operator", "expected", "json_path"},
                   {"kind", "id", "path", "operator"} if is_file else {"id", "stream", "operator", "expected"})
            key = case["id"]
            require(isinstance(key, str) and 0 < len(key.encode("utf-8")) <= 4096
                    and "\x00" not in key and "\n" not in key and key not in identifiers,
                    "PROCESS_CASE_ID", "Observation IDs must be bounded and unique")
            identifiers.add(key)
            if is_file:
                name = relative_path(case["path"])
                observed_paths.add(name)
                operator = case["operator"]
                require(isinstance(operator, str) and operator in {"equals", "json_equals", "sha256", "unchanged", "absent"},
                        "PROCESS_PREDICATE", "File observation uses equals, json_equals, sha256, unchanged or absent")
                require(("expected" in case) == (operator not in {"unchanged", "absent"}),
                        "PROCESS_PREDICATE", "This file predicate needs exactly its applicable expectation")
                if "expected" in case:
                    require(len(canonical_bytes(case["expected"])) <= 64000 and (operator == "json_equals" or isinstance(case["expected"], str)),
                            "PROCESS_PREDICATE", "Invalid file expectation")
                if operator == "sha256":
                    require(bool(re.fullmatch(r"[a-f0-9]{64}", case["expected"])), "PROCESS_PREDICATE", "Invalid expected file digest")
                continue
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
        require(len(observed_paths) <= 32, "PROCESS_PREDICATE", "At most 32 file paths may be observed per invocation")
        normalized = {"source_paths": list(paths), "argv": list(argv), "cwd": cwd, "cases": copy.deepcopy(cases)}
        for key in ("fixtures", "expected_exit_code"):
            if key in selector:
                normalized[key] = copy.deepcopy(selector[key])
        return {"selector": normalized, "source_paths": list(paths), "criteria_source": "selector",
                **({"expected_exit_code": selector["expected_exit_code"]} if "expected_exit_code" in selector else {})}

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
        command = {"kind": "command", "argv": list(selector["argv"]), "cwd": selector["cwd"],
                   "timeout_seconds": integer(check.get("timeout_seconds", 30), 1, 120, "timeout_seconds"),
                   "expectedExitCode": selector.get("expected_exit_code", 0)}
        if "fixtures" in selector:
            command["fixtures"] = copy.deepcopy(selector["fixtures"])
        paths = sorted({case["path"] for case in selector["cases"] if case.get("kind") == "file"})
        if paths:
            command["observed_files"] = paths
        return command

    def describe_observations(self, selector):
        return [{"case_id": case["id"], "kind": "file_state" if case.get("kind") == "file" else "process_output",
                 "operator": case["operator"], "target": case.get("path", case.get("stream"))} for case in selector["cases"]]

    def describe_selection(self):
        from .process_schema import schema
        return schema()

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
        files = capture.get("file_observations", [])
        require(isinstance(files, list) and len(files) <= 32, "PROCESS_FILE_REPORT", "Invalid trusted file observation list")
        for fact in files:
            fields(fact, {"path", "before", "after", "afterBase64", "contentStatus"}, {"path", "before", "after"})
            relative_path(fact["path"])
            for state in (fact["before"], fact["after"]):
                require(isinstance(state, dict) and state.get("kind") in {"file", "directory", "absent"},
                        "PROCESS_FILE_REPORT", "Invalid trusted file state")
                if state["kind"] == "file":
                    require(set(state) == {"kind", "sha256", "size", "executable", "mtimeNs"}
                            and isinstance(state["sha256"], str) and bool(re.fullmatch(r"[a-f0-9]{64}", state["sha256"]))
                            and type(state["size"]) is int and 0 <= state["size"] <= 10 * 1024 * 1024
                            and type(state["executable"]) is bool and isinstance(state["mtimeNs"], str),
                            "PROCESS_FILE_REPORT", "File observation metadata is incomplete")
                else:
                    require(set(state) == {"kind"}, "PROCESS_FILE_REPORT", "Invalid non-file metadata")
        file_index = {item["path"]: item for item in files}
        require(len(file_index) == len(files), "PROCESS_FILE_REPORT", "Duplicate file observations")
        expected_files = {case["path"] for case in check["adapter"]["selector"]["cases"] if case.get("kind") == "file"}
        require(not completed or set(file_index) == expected_files, "PROCESS_FILE_REPORT", "File observation scope differs from the declared invocation")
        for declaration in check["adapter"]["selector"]["cases"]:
            outcome, message = "not_run", capture.get("reason")
            if completed:
                try:
                    if declaration.get("kind") == "file":
                        fact = file_index[declaration["path"]]
                        if fact.get("contentStatus") == "limit_exceeded" and declaration["operator"] in {"equals", "json_equals"}:
                            outcome, message = "error", "Observed file body exceeds the retention limit; this content predicate was not evaluated"
                        else:
                            passed = self._file_matches(fact, declaration)
                            outcome = "passed" if passed else "failed"
                            message = None if passed else "File " + declaration["path"] + " " + declaration["operator"] + " failed; before " + repr(fact["before"])[:240] + "; after " + repr(fact["after"])[:240]
                    elif declaration["operator"] == "json_equals":
                        actual, expected = receipt[declaration["stream"]], declaration["expected"]
                        projected = decode(actual)
                        for part in declaration.get("json_path", []):
                            if type(part) is int and isinstance(projected, list) or isinstance(part, str) and isinstance(projected, dict):
                                projected = projected[part]
                            else:
                                raise KeyError("Projection does not address this JSON value")
                        passed = canonical_bytes(projected) == canonical_bytes(expected)
                    else:
                        actual, expected = receipt[declaration["stream"]], declaration["expected"]
                        passed = expected in actual if declaration["operator"] == "contains" else actual == expected
                    if declaration.get("kind") != "file":
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
                                         "message": message, "duration": duration,
                                         **({"file_state": {key: value for key, value in file_index[declaration["path"]].items() if key != "afterBase64"}}
                                            if declaration.get("kind") == "file" and declaration["path"] in file_index else {})}]})
        return {"framework": "process-observations", "collection_complete": True, "session_complete": completed,
                "discovery_source": "pinned_predicate_declarations", "cases": observed,
                "observation_boundary": "external_capture"}

    @staticmethod
    def _file_matches(fact, declaration):
        before, after, operator = fact["before"], fact["after"], declaration["operator"]
        require(before.get("kind") in {"file", "directory", "absent"} and after.get("kind") in {"file", "directory", "absent"},
                "PROCESS_FILE_REPORT", "Invalid trusted file state")
        if operator == "absent":
            return after["kind"] == "absent"
        if operator == "unchanged":
            return before["kind"] == after["kind"] == "file" and before == after
        if after["kind"] != "file":
            return False
        expected = declaration["expected"]
        if operator == "sha256":
            return after["sha256"] == expected
        raw = base64.b64decode(fact["afterBase64"], validate=True)
        require(len(raw) == after["size"] and hashlib.sha256(raw).hexdigest() == after["sha256"],
                "PROCESS_FILE_REPORT", "File bytes differ from their trusted measurement")
        if operator == "equals":
            return raw == expected.encode("utf-8")
        return canonical_bytes(decode(raw.decode("utf-8"))) == canonical_bytes(expected)
