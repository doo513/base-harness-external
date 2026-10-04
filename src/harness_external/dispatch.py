"""One isolated CLI request, with Core-owned context guards and mechanical batching."""
import inspect
import os
from pathlib import Path
import time

from .errors import fields, require

METHODS = {name: name.replace("-", "_") for name in (
    "start", "observe", "revise", "assess", "submit", "verify", "cancel", "status", "resume", "records", "cleanup", "finish", "list-runs", "context")}
METHODS.update(check="register_check", **{"retire-check": "retire_check"})
READS = {"status", "resume", "records", "cleanup", "list-runs", "context", "doctor", "wait"}


def waiting(api, run_id, job_id, wait_seconds):
    require(type(wait_seconds) in (int, float) and 0 <= wait_seconds <= 3600, "WAIT_INVALID", "wait_seconds must be 0..3600")
    deadline = time.monotonic() + wait_seconds
    while True:
        result = api.status(run_id, job_id, view="summary")
        if result["job"]["status"] not in {"queued", "running"}:
            return {**result, "wait_status": "terminal"}
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {**result, "wait_status": "pending", "note": "Observation window ended; job was not cancelled or reissued."}
        time.sleep(min(1, remaining))


def invoke(api, request):
    fields(request, {"operation", "arguments", "request_id", "context"}, {"operation", "arguments"})
    operation, arguments = request["operation"], request["arguments"]
    require(isinstance(operation, str) and operation in {*METHODS, "doctor", "checkpoint", "wait"}, "OPERATION_UNSUPPORTED", "Unknown operation")
    require(isinstance(arguments, dict), "INVALID_PARAMETERS", "Arguments must be an object")
    arguments = dict(arguments)
    request_id = request.get("request_id")
    require(request_id is None if operation in READS else isinstance(request_id, str) and 0 < len(request_id) <= 128,
            "REQUEST_ID_REQUIRED", "Mutations require a stable request ID; reads do not accept one")
    expected = None
    if operation not in {"start", "doctor", "list-runs"}:
        context = fields(request.get("context"), {"workspace", "domain", "intent_ref", "policy_ref"}, {"workspace", "domain"})
        require(isinstance(context["workspace"], str) and Path(context["workspace"]).is_absolute()
                and isinstance(context["domain"], str), "CONTEXT_REQUIRED", "An absolute workspace and Domain are required")
        require(("intent_ref" in context) == ("policy_ref" in context), "CONTEXT_REQUIRED", "Supply both pinned references")
        expected = {**context, "workspace": os.path.abspath(context["workspace"]), "run_id": arguments.get("run_id")}
    else:
        require("context" not in request, "REQUEST_INVALID", "Global operations do not accept a Run context")
    with api.store.expecting(expected):
        if operation == "doctor":
            from .diagnostics import doctor
            from .service import response
            fields(arguments, {"sandbox"})
            return response(**doctor(api.store, **arguments))
        if operation in {"checkpoint", "wait"}:
            allowed = {"run_id", "wait_seconds", "compare_baseline"} if operation == "checkpoint" else {"run_id", "job_id", "wait_seconds"}
            fields(arguments, allowed, {"run_id"} if operation == "checkpoint" else {"run_id", "job_id"})
            wait_seconds = arguments.get("wait_seconds", 0 if operation == "checkpoint" else 30)
            require(type(wait_seconds) in (int, float) and 0 <= wait_seconds <= 3600, "WAIT_INVALID", "wait_seconds must be 0..3600")
            run_id = arguments["run_id"]
            if operation == "wait":
                return waiting(api, run_id, arguments["job_id"], wait_seconds)
            require(len(request_id) <= 120 and type(arguments.get("compare_baseline", False)) is bool,
                    "INVALID_PARAMETERS", "Invalid checkpoint ID or compare_baseline")
            # Independent durable replay keys retain the original snapshot if the
            # process dies after submit but before verify. No automatic retry.
            submitted = api.submit(run_id, request_id + "-submit")
            verified = api.verify(run_id, request_id + "-verify", compare_baseline=arguments.get("compare_baseline", False),
                                  expected_candidate_hash=submitted["candidate"]["candidate_hash"])
            return {**waiting(api, run_id, verified["job_id"], wait_seconds),
                    "request_ids": {"submit": request_id + "-submit", "verify": request_id + "-verify"},
                    "submitted_candidate_hash": submitted["candidate"]["candidate_hash"]}
        method = getattr(api, METHODS[operation])
        if operation == "start":
            for old, new in (("domain", "domain_id"), ("required_check", "required_checks"), ("deferred_check", "deferred_checks")):
                if old in arguments:
                    require(new not in arguments, "INVALID_PARAMETERS", "Duplicate argument alias")
                    arguments[new] = arguments.pop(old)
            arguments.setdefault("parameters", {})
        if "data" in arguments:
            name = "observation" if operation == "observe" else "assessment" if operation == "assess" else "proposal"
            arguments[name] = arguments.pop("data")
        if operation == "status":
            arguments.setdefault("view", "summary")
        if operation not in READS:
            arguments["request_id"] = request_id
        try:
            inspect.signature(method).bind(**arguments)
        except TypeError as error:
            require(False, "INVALID_PARAMETERS", str(error))
        return method(**arguments)
