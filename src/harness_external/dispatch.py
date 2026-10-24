"""One isolated CLI request, with Core-owned context guards and mechanical batching."""
import inspect
import os
from pathlib import Path
import sqlite3
import time

from harness.common import canonical_hash
from .errors import HarnessError, fields, require

METHODS = {name: name.replace("-", "_") for name in (
    "start", "observe", "revise", "assess", "submit", "verify", "compare", "cancel", "status", "resume", "records", "cleanup", "finish", "list-runs", "context", "describe")}
METHODS.update(check="register_check", **{"retire-check": "retire_check"})
READS = {"status", "resume", "records", "list-runs", "context", "doctor", "wait", "describe"}


def waiting(api, run_id, job_id, wait_seconds, *, view="summary", attention_limit=20):
    require(type(wait_seconds) in (int, float) and 0 <= wait_seconds <= 3600, "WAIT_INVALID", "wait_seconds must be 0..3600")
    require(view in {"summary", "attention", "brief"}, "INVALID_VIEW", "Wait view is summary, attention or brief")
    deadline = time.monotonic() + wait_seconds
    while True:
        result = api.status(run_id, job_id, view="summary" if view == "summary" else "compact")
        terminal = result["job"]["status"] not in {"queued", "running"}
        remaining = deadline - time.monotonic()
        if terminal or remaining <= 0:
            if view != "summary":
                result = api.status(run_id, job_id, view="attention", attention_limit=attention_limit)
                terminal = result["selected_job"]["status"] not in {"queued", "running"}
                if view == "brief":
                    from .checkpoint_view import brief
                    result = brief(result)
            return {**result, "wait_status": "terminal" if terminal else "pending",
                    **({"note": "Observation window ended; job was not cancelled or reissued."} if not terminal else {})}
        time.sleep(min(1, remaining))


def _checkpoint_intent(api, run_id, request_id, observation, compare_baseline):
    """Bind optional context to the existing durable request ledger, not a new DB."""
    scope = "checkpoint:" + run_id
    fingerprint = canonical_hash({"operation": "checkpoint", "observation": observation, "compare_baseline": compare_baseline,
                                  **api.store.delivery_metadata(run_id)})
    with api.store.transaction() as connection:
        prior = api.store.replay(connection, scope, request_id, fingerprint)
        if prior:
            return
        child = connection.execute("SELECT 1 FROM requests WHERE scope=? AND request_id IN (?,?) LIMIT 1",
                                   (run_id, request_id + "-submit", request_id + "-verify")).fetchone()
        require(not child or observation is None, "CHECKPOINT_REPLAY_SCOPE", "A legacy checkpoint cannot acquire new caller context on replay")
        run = api.store.current(connection, run_id)
        require(child is not None or run["status"] == "active", "RUN_CLOSED", "Cannot create a checkpoint intent for a terminal Run")
        api.store.remember(connection, scope, request_id, fingerprint,
                           {"schema_version": "checkpoint-intent-v1", "run_id": run_id, "has_caller_context": observation is not None})


def _checkpoint_steps(api, run_id, request_id, observation, compare_baseline, wait_seconds, view, attention_limit):
    caller_record, submitted, verified, stage = None, None, None, "observe" if observation is not None else "submit"
    try:
        if observation is not None:
            stored = api.observe(run_id, observation, request_id + "-note")
            caller_record = {"observation_id": stored["observation"]["observation_id"], "trust": "untrusted",
                             **({"record_ref": stored["record_ref"]} if "record_ref" in stored else {})}
        stage = "submit"
        submitted = api.submit(run_id, request_id + "-submit")
        stage = "verify"
        verified = api.verify(run_id, request_id + "-verify", compare_baseline=compare_baseline,
                              expected_candidate_hash=submitted["candidate"]["candidate_hash"])
        stage = "wait"
        return {**waiting(api, run_id, verified["job_id"], wait_seconds, view=view, attention_limit=attention_limit),
                "request_ids": {"submit": request_id + "-submit", "verify": request_id + "-verify"},
                "submitted_candidate_hash": submitted["candidate"]["candidate_hash"],
                **({"caller_record": caller_record} if caller_record is not None else {})}
    except (HarnessError, OSError, ValueError, sqlite3.Error) as error:
        error.checkpoint_progress = {"stage": stage, "run_id": run_id,
            "caller_record": caller_record,
            "submitted_candidate_hash": submitted["candidate"]["candidate_hash"] if submitted else None,
            "job_id": verified["job_id"] if verified else None,
            "recovery": "inspect_or_replay_the_same_saved_request_not_a_new_intent",
            "meaning": "acknowledged_stages_null_does_not_prove_absence_of_side_effects"}
        raise


def invoke(api, request, *, allowed_workspace=None):
    """Application envelope; routing metadata is never a Domain parameter."""
    from .task_storage import TaskStores
    from .service import response
    from .application_schema import OPTIONS as APP_OPTIONS
    fields(request, {"operation", "arguments", "request_id", "context", "task_id", "project_workspace", "knowledge", "input_bindings"}, {"operation", "arguments"})
    require("project_workspace" not in request or "task_id" in request,
            "PROJECT_WORKSPACE", "A project route requires an explicit task ID")
    if "project_workspace" in request:
        require(isinstance(request["project_workspace"], str) and Path(request["project_workspace"]).is_absolute(),
                "PROJECT_WORKSPACE", "Project workspace must be absolute")
        require(allowed_workspace is None or allowed_workspace(Path(request["project_workspace"])),
                "WORKSPACE_NOT_ALLOWED", "Project is outside the configured roots")
    operation = request["operation"]
    require(isinstance(operation, str) and operation in {*METHODS, *APP_OPTIONS, "doctor", "checkpoint", "wait"},
            "OPERATION_UNSUPPORTED", "Unknown operation")
    require(isinstance(request["arguments"], dict), "INVALID_PARAMETERS", "Arguments must be an object")
    require("knowledge" not in request or operation == "finish", "REQUEST_INVALID", "Automatic publication belongs only to finish")
    if "input_bindings" in request:
        from .delivery import binding_shape
        binding_shape(operation, request["input_bindings"])
    if request["operation"] == "list-tasks":
        require(set(request) <= {"operation", "arguments"} and isinstance(request["arguments"], dict),
                "REQUEST_INVALID", "Task discovery is a global read")
        fields(request["arguments"], {"workspace", "domain", "offset", "limit"})
        return response(**TaskStores(api).list(**request["arguments"], allowed_workspace=allowed_workspace))
    if operation == "list-runs" and allowed_workspace is not None and "task_id" not in request:
        from . import queries
        fields(request, {"operation", "arguments"}, {"operation", "arguments"})
        fields(request["arguments"], {"offset", "limit"})
        return response(**queries.list_runs(api, allowed_workspace=allowed_workspace, **request["arguments"]))
    if operation.startswith("knowledge-"):
        return _knowledge(api, request, allowed_workspace)
    if operation.startswith("report-"):
        return _report(api, request, allowed_workspace)
    inner = {k: v for k, v in request.items() if k != "knowledge"}
    if "task_id" in request:
        result = TaskStores(api, request.get("project_workspace")).invoke(inner, _invoke_core, allowed_workspace=allowed_workspace)
    else:
        result = _invoke_core(api, inner)
    if operation == "finish" and "project_workspace" in request:
        publication={**{k:v for k,v in inner.items() if k!="input_bindings"},"operation":"report-publish",
                     "arguments":{"run_id":request["arguments"]["run_id"]},
                     "request_id":"finish-report-"+canonical_hash({"request_id":request["request_id"]})[:48]}
        try:
            result["run_report"]=_report(api,publication,allowed_workspace)
        except (HarnessError,OSError,ValueError,sqlite3.Error) as error:
            result["run_report"]={"status":"failed","run_closed":True,"retry_operation":"report-publish",
                "error":error.as_dict() if isinstance(error,HarnessError) else {"code":"RUN_REPORT_IO","message":str(error)[:1000]}}
    if operation == "finish" and "knowledge" in request:
        publication = {**{k: v for k, v in inner.items() if k != "input_bindings"}, "operation": "knowledge-publish",
                       "arguments": {"run_id": request["arguments"]["run_id"], "data": request["knowledge"]},
                       "request_id": "finish-note-" + canonical_hash({"request_id": request["request_id"]})[:48]}
        try:
            result["knowledge_publication"] = _knowledge(api, publication, allowed_workspace)
        except (HarnessError, OSError, ValueError, sqlite3.Error) as error:
            # Core's committed closeout is authoritative even if this projection
            # failed. Re-publish the note, never re-run verification to repair MD.
            result["knowledge_publication"] = {"status": "failed", "run_closed": True,
                "error": error.as_dict() if isinstance(error, HarnessError) else {"code": "KNOWLEDGE_IO", "message": str(error)[:1000]},
                "retry_operation": "knowledge-publish"}
    return result


def _report(api, request, allowed_workspace):
    from .run_reports import RunReports
    from .task_storage import TaskStores
    from .service import response
    library=RunReports(api.store.root)
    operation,arguments=request["operation"],request["arguments"]
    if operation in {"report-list","report-read"}:
        require(set(request)<={"operation","arguments"},"REQUEST_INVALID","Run document lookup is a global read")
        if operation=="report-list":
            fields(arguments,{"workspace","domain","offset","limit"})
            return response(**library.list(**arguments,allowed_workspace=allowed_workspace))
        fields(arguments,{"entry_id"},{"entry_id"})
        return response(**library.read(**arguments,allowed_workspace=allowed_workspace))
    fields(arguments,{"run_id"},{"run_id"})
    request_id=request.get("request_id")
    require(isinstance(request_id,str) and 0<len(request_id)<=128,"REQUEST_ID_REQUIRED","Publishing requires a stable request ID")
    selected=TaskStores(api,request.get("project_workspace")).selected(request["task_id"]) if "task_id" in request else api
    _invoke_core(selected,{"operation":"resume","arguments":{"run_id":arguments["run_id"],"view":"compact"},"context":request.get("context")})
    require(allowed_workspace is None or allowed_workspace(Path(request["context"]["workspace"])),"WORKSPACE_NOT_ALLOWED","Run document source is outside the configured roots")
    value=library.publish(selected,arguments["run_id"],request_id,task_id=request.get("task_id"))
    return response(**value,**({"task_id":request["task_id"]} if "task_id" in request else {}),
                    **({"project_workspace":request["project_workspace"]} if "project_workspace" in request else {}))


def _knowledge(api, request, allowed_workspace):
    from .knowledge import KnowledgeLibrary
    from .task_storage import TaskStores
    from .service import response
    library = KnowledgeLibrary(api.store.root)
    operation, arguments = request["operation"], request["arguments"]
    if operation in {"knowledge-find", "knowledge-read"}:
        require(set(request) <= {"operation", "arguments"}, "REQUEST_INVALID", "Knowledge lookup is a global read")
        if operation == "knowledge-find":
            fields(arguments, {"query", "domain", "offset", "limit"})
            return response(**library.find(**arguments, allowed_workspace=allowed_workspace))
        fields(arguments, {"entry_id"}, {"entry_id"})
        return response(**library.read(**arguments, allowed_workspace=allowed_workspace))
    fields(arguments, {"run_id", "data"}, {"run_id", "data"})
    request_id = request.get("request_id")
    require(isinstance(request_id, str) and 0 < len(request_id) <= 128, "REQUEST_ID_REQUIRED", "Publishing needs a stable request ID")
    selected = TaskStores(api, request.get("project_workspace")).selected(request["task_id"]) if "task_id" in request else api
    _invoke_core(selected, {"operation": "resume", "arguments": {"run_id": arguments["run_id"], "view": "compact"},
                           "context": request.get("context")})
    # Validate the same Core context guard even for terminal Runs, then enforce
    # server source visibility before any shared document is read or written.
    context = request["context"]
    require(allowed_workspace is None or allowed_workspace(Path(context["workspace"])),
            "WORKSPACE_NOT_ALLOWED", "Source workspace is outside the configured roots")
    result = library.publish(selected, arguments["run_id"], arguments["data"], request_id, task_id=request.get("task_id"))
    return response(**result, **({"task_id": request["task_id"]} if "task_id" in request else {}),
                    **({"project_workspace": request["project_workspace"]} if "project_workspace" in request else {}))


def _invoke_core(api, request):
    fields(request, {"operation", "arguments", "request_id", "context", "input_bindings"}, {"operation", "arguments"})
    operation, arguments = request["operation"], request["arguments"]
    require(isinstance(operation, str) and operation in {*METHODS, "doctor", "checkpoint", "wait"}, "OPERATION_UNSUPPORTED", "Unknown operation")
    require(isinstance(arguments, dict), "INVALID_PARAMETERS", "Arguments must be an object")
    arguments = dict(arguments)
    request_id = request.get("request_id")
    require(request_id is None if operation in READS else isinstance(request_id, str) and 0 < len(request_id) <= 128,
            "REQUEST_ID_REQUIRED", "Mutations require a stable request ID; reads do not accept one")
    expected = None
    if operation not in {"start", "doctor", "list-runs", "describe"}:
        context = fields(request.get("context"), {"workspace", "domain", "intent_ref", "policy_ref"}, {"workspace", "domain"})
        require(isinstance(context["workspace"], str) and Path(context["workspace"]).is_absolute()
                and isinstance(context["domain"], str), "CONTEXT_REQUIRED", "An absolute workspace and Domain are required")
        require(("intent_ref" in context) == ("policy_ref" in context), "CONTEXT_REQUIRED", "Supply both pinned references")
        expected = {**context, "workspace": os.path.abspath(context["workspace"]), "run_id": arguments.get("run_id")}
    else:
        require("context" not in request, "REQUEST_INVALID", "Global operations do not accept a Run context")
    bindings = None
    if "input_bindings" in request:
        from .delivery import binding_shape
        bindings = binding_shape(operation, request["input_bindings"])
    with api.store.expecting(expected), api.store.delivering(api, arguments, bindings):
        if operation == "doctor":
            from .diagnostics import doctor
            from .service import response
            fields(arguments, {"sandbox"})
            return response(**doctor(api.store, **arguments))
        if operation in {"checkpoint", "wait"}:
            allowed = ({"run_id", "wait_seconds", "compare_baseline", "payload", "note"} if operation == "checkpoint"
                       else {"run_id", "job_id", "wait_seconds"}) | {"view", "attention_limit"}
            fields(arguments, allowed, {"run_id"} if operation == "checkpoint" else {"run_id", "job_id"})
            wait_seconds = arguments.get("wait_seconds", 0 if operation == "checkpoint" else 30)
            require(type(wait_seconds) in (int, float) and 0 <= wait_seconds <= 3600, "WAIT_INVALID", "wait_seconds must be 0..3600")
            from .errors import integer
            view = arguments.get("view", "summary")
            require(view in {"summary", "attention", "brief"}, "INVALID_VIEW", "Checkpoint view is summary, attention or brief")
            attention_limit = integer(arguments.get("attention_limit", 20), 1, 100, "attention_limit")
            run_id = arguments["run_id"]
            if operation == "wait":
                return waiting(api, run_id, arguments["job_id"], wait_seconds, view=view, attention_limit=attention_limit)
            require(len(request_id) <= 120 and type(arguments.get("compare_baseline", False)) is bool,
                    "INVALID_PARAMETERS", "Invalid checkpoint ID or compare_baseline")
            note = arguments.get("note", "")
            require(isinstance(note, str) and len(note) <= 16000, "INVALID_OBSERVATION", "Checkpoint note must be bounded text")
            observation = None
            if "payload" in arguments:
                from .delivery import payload
                observation = {"note": note, "payload": payload(arguments["payload"])}
            elif note:
                observation = {"note": note}
            _checkpoint_intent(api, run_id, request_id, observation, arguments.get("compare_baseline", False))
            # Independent durable replay keys retain the original snapshot if the
            # process dies after submit but before verify. No automatic retry.
            return _checkpoint_steps(api, run_id, request_id, observation, arguments.get("compare_baseline", False),
                                     wait_seconds, view, attention_limit)
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
