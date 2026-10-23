"""Core-owned structural inputs. Domain parameters and adapter selectors stay opaque.

The offline client catalog is generated from this file, not independently maintained.
State/authority checks still run in their existing service owners.
"""
import copy

from harness.common import canonical_hash
from .errors import fields, require, OBSERVATION_REFERENCE_LIMIT
from . import application_schema


OPTIONS = {
    "describe": {"domain_id": "str", "adapter_id": "str", "operation": "str"},
    "start": {"domain": "str", "goal": "str", "workspace": "str", "mode": "str", "parameters": "json",
              "budget": "json", "constraints": "json", "workspace_constraints": "json", "provenance": "json",
              "analysis_selection": "json", "sources": "json", "required_check": "repeat", "deferred_check": "repeat",
              "policy_id": "str", "predecessor_run_id": "str", "policy_change_reason": "str",
              "continuation_reason": "str", "capture_baseline": "bool"},
    "invoke": {"request": "json"},
    "context": {"run_id": "str", "after": "str", "page": "str", "limit": "int"},
    "checkpoint": {"run_id": "str", "compare_baseline": "bool", "wait_seconds": "number", "view": "str", "attention_limit": "int", "payload": "json", "note": "str"},
    "wait": {"run_id": "str", "job_id": "str", "wait_seconds": "number", "view": "str", "attention_limit": "int"},
    "doctor": {"sandbox": "bool"}, "list-runs": {"offset": "int", "limit": "int"},
    **{name: {"run_id": "str", "data": "json"} for name in ("observe", "revise", "check", "retire-check", "assess")},
    "submit": {"run_id": "str"},
    "resume": {"run_id": "str", "view": "str", "analysis_selection": "json", "attention_limit": "int"},
    "verify": {"run_id": "str", "compare_baseline": "bool", "expected_candidate_hash": "str"},
    "compare": {"run_id": "str", "reference": "str", "expected_candidate_hash": "str", "parameters": "json"},
    "cancel": {"run_id": "str", "job_id": "str"},
    "status": {"run_id": "str", "job_id": "str", "check_workspace": "bool", "view": "str", "attention_limit": "int"},
    "records": {"run_id": "str", "kind": "str", "job_id": "str", "offset": "int", "limit": "int", "view": "str",
                "detail": "json", "outcomes": "json", "subject_role": "str"},
    "cleanup": {"run_id": "str", "apply": "bool", "min_age_seconds": "int"},
    "finish": {"run_id": "str", "outcome": "str", "summary": "str", "assessment": "json"},
}
# Caller syntax intentionally asks for an explicit policy/provenance on start;
# the Core API retains its historical defaults. Expose both, do not conflate them.
REQUIRED = {
    "invoke": {"request"}, "wait": {"run_id", "job_id"},
    "start": {"domain", "goal", "workspace", "mode", "provenance"},
    **{name: {"run_id", "data"} for name in ("observe", "revise", "check", "retire-check", "assess")},
    "compare": {"run_id", "expected_candidate_hash", "parameters"},
    "cancel": {"run_id", "job_id"}, "records": {"run_id", "kind"}, "cleanup": {"run_id"}, "finish": {"run_id", "outcome"},
}
OPTIONS.update(application_schema.OPTIONS)
REQUIRED.update(application_schema.REQUIRED)


def obj(properties, required=()):
    return {"type": "object", "properties": properties, "required": sorted(required), "additionalProperties": False}


def strings(maximum=100):
    return {"type": "array", "items": {"type": "string"}, "maxItems": maximum}


SOURCE_REF = obj({"source_id": {"type": "string"}, "start_line": {"type": "integer", "minimum": 1},
                  "end_line": {"type": "integer", "minimum": 1}}, ("source_id", "start_line", "end_line"))
PROVENANCE = obj({"declared_author": {"type": "string", "enum": ["user", "model", "application", "unknown"]},
                  "approval_reference": {"type": "string", "minLength": 1, "maxLength": 2000}})
ASSESSMENT = obj({
    "status": {"type": "string", "enum": ["satisfied", "partial", "unsolved", "not_assessed"]},
    "summary": {"type": "string", "maxLength": 16000},
    "uncertainties": {"type": "array", "maxItems": 100, "items": {"type": "string", "maxLength": 2000}},
    "cited_observation_ids": strings(OBSERVATION_REFERENCE_LIMIT), "interpretation_revision": {"type": "integer"},
}, ("status", "summary", "uncertainties", "cited_observation_ids", "interpretation_revision"))
REVISE = obj({"expected_revision": {"type": "integer"}, "goal_summary": {"type": "string"},
              "parameters": {"type": "object", "description": "Domain-owned schema"},
              "assumptions": {"type": "array", "items": obj({"id": {"type": "string"}, "statement": {"type": "string"},
                              "observation_ids": strings(OBSERVATION_REFERENCE_LIMIT)}, ("id", "statement"))},
              "open_questions": strings(), "observation_ids": strings(OBSERVATION_REFERENCE_LIMIT), "activity_ids": strings()}, ("expected_revision",))
CHECK = obj({"check_id": {"type": "string"}, "parameters": {"type": "object", "description": "Domain-owned schema"},
             "expected_revision": {"type": "integer"}, "interpretation_revision": {"type": "integer"},
             "provenance": PROVENANCE}, ("check_id", "parameters", "interpretation_revision"))
OBSERVE = obj({**{key: {"type": "string"} for key in ("note", "kind", "record_id", "task_id", "parent_task_id", "state", "related_activity_id", "relation")},
               **{key: strings() for key in ("references", "depends_on")},
               "observation_ids": strings(OBSERVATION_REFERENCE_LIMIT),
               "interpretation_revision": {"type": "integer"}, "task_revision": {"type": "integer"},
               "need": {"type": "object", "description": "Optional Domain-owned legacy data"},
               "payload": obj({"fields": {"type": "object", "default": {}, "description": "Caller-authored JSON fields; null is unresolved, not passed"},
                               "extensions": {"type": "object", "default": {}, "description": "Opaque caller additions, never authority or measurements"},
                               "parents": {"type": "array", "default": [], "maxItems": 32,
                                           "description": "Exact same-Run record references, not filesystem paths"}})}, ("note",))
RETIRE = obj({"check_id": {"type": "string"}, "expected_revision": {"type": "integer"},
              "interpretation_revision": {"type": "integer"}, "reason": {"type": "string"}},
             ("check_id", "expected_revision", "interpretation_revision", "reason"))
WORKSPACE_CONSTRAINTS = obj({"unchanged_files": {"type": "array", "maxItems": 128, "items": obj({
    "path": {"type": "string"}, "source_refs": {"type": "array", "minItems": 1, "maxItems": 16, "items": SOURCE_REF}},
    ("path", "source_refs"))}}, ("unchanged_files",))


def validate_shape(value, schema, path="", *, types=True):
    """Bounded structural validation; no dispatch, filesystem or Domain semantics."""
    from .errors import ValidationIssues
    issues = ValidationIssues()
    def walk(item, node, pointer):
        expected = node.get("type")
        match = {"object": isinstance(item, dict), "array": isinstance(item, list),
                 "string": isinstance(item, str), "integer": type(item) is int,
                 "boolean": type(item) is bool, "number": type(item) in (int, float)}
        if not match.get(expected, True):
            issues.add(pointer, "TYPE_INVALID", "Expected " + str(expected))
            return
        if expected == "object" and "properties" in node:
            if not all(isinstance(key, str) for key in item):
                issues.add(pointer, "TYPE_INVALID", "Object keys must be strings")
                return
            allowed, required = node["properties"], node.get("required", [])
            for key in sorted(set(required) - item.keys()):
                issues.add(pointer + "/" + key, "FIELD_REQUIRED", "Required field is missing")
            for key in sorted(item):
                escaped = str(key).replace("~", "~0").replace("/", "~1")
                if key not in allowed:
                    issues.add(pointer + "/" + escaped, "FIELD_UNEXPECTED", "Field is not supported by this operation")
                elif types:
                    walk(item[key], allowed[key], pointer + "/" + escaped)
        if not types:
            return
        if "enum" in node and item not in node["enum"]:
            issues.add(pointer, "VALUE_INVALID", "Expected one of " + ", ".join(map(str, node["enum"])))
        for key, valid in (("maxLength", lambda: len(item) <= node[key]), ("maxItems", lambda: len(item) <= node[key]),
                           ("minItems", lambda: len(item) >= node[key]), ("minLength", lambda: len(item) >= node[key]),
                           ("minimum", lambda: item >= node[key])):
            if key in node and not valid():
                issues.add(pointer, "VALUE_INVALID", "Value is outside " + key)
        if expected == "array" and "items" in node:
            for index, child in enumerate(item[:node.get("maxItems", 256)]):
                walk(child, node["items"], pointer + "/" + str(index))
    walk(value, schema, path)
    issues.raise_if_any(code="INVALID_PARAMETERS", message="Unexpected or invalid structured input")
    return value


def input_schema(operation, *, caller=False):
    require(operation in OPTIONS, "OPERATION_UNSUPPORTED", "Unknown operation")
    if operation in application_schema.OPTIONS:
        return application_schema.input_schema(operation)
    kinds = {"str": {"type": "string"}, "int": {"type": "integer"}, "number": {"type": "number"},
             "bool": {"type": "boolean"}, "repeat": strings(256), "json": {"type": ["object", "array"]}}
    properties = {key: copy.deepcopy(kinds[value]) for key, value in OPTIONS[operation].items()}
    required = REQUIRED.get(operation, {"run_id"} if "run_id" in properties else set())
    if operation == "start":
        if not caller:
            required = {"domain", "goal", "workspace"}
        properties["workspace_constraints"] = copy.deepcopy(WORKSPACE_CONSTRAINTS)
        properties["provenance"] = copy.deepcopy(PROVENANCE)
        properties["constraints"] = strings()
        properties["parameters"] = {"type": "object", "description": "Domain-owned schema"}
        properties["budget"] = obj({"max_actions": {"type": "integer", "minimum": 1, "maximum": 1000},
                                    "max_verifications": {"type": "integer", "minimum": 1, "maximum": 20},
                                    "timeout_seconds": {"type": "integer", "minimum": 10, "maximum": 86400}})
        properties["sources"] = {"type": "array", "maxItems": 16, "items": obj({"id": {"type": "string"},
                                  "label": {"type": "string"}, "text": {"type": "string"}}, ("id", "text"))}
    if operation in {"assess", "revise", "check", "retire-check", "observe"}:
        properties["data"] = copy.deepcopy({"assess": ASSESSMENT, "revise": REVISE, "check": CHECK, "retire-check": RETIRE, "observe": OBSERVE}[operation])
    if operation == "finish":
        properties["assessment"] = copy.deepcopy(ASSESSMENT)
        properties["outcome"]["enum"] = ["completed", "partial", "abandoned"]
    if operation == "compare":
        properties["reference"]["enum"] = ["baseline"]
        properties["parameters"] = {"type": "object", "description": "Domain comparison preparation schema"}
    if operation in {"resume", "status"}:
        properties["view"]["enum"] = ["summary", "compact", "attention"] + (["full"] if operation == "status" else [])
        properties["attention_limit"] = {"type": "integer", "minimum": 1, "maximum": 100,
            "description": "Maximum displayed items for view=attention; default 20"}
    if operation in {"checkpoint", "wait"}:
        properties["view"]["enum"] = ["summary", "attention", "brief"]
        properties["attention_limit"] = {"type": "integer", "minimum": 1, "maximum": 100}
    if operation == "checkpoint":
        properties["payload"] = copy.deepcopy(OBSERVE["properties"]["payload"])
        properties["note"]["maxLength"] = 16000
    return obj(properties, required)


def catalog():
    from .delivery import TARGETS
    body = {"schema_version": "operation-catalog-v1", "revision": "3", "options": OPTIONS,
            "required": {key: sorted(value) for key, value in REQUIRED.items()},
            "operations": {name: input_schema(name, caller=True) for name in OPTIONS},
            "core_operations": {name: input_schema(name) for name in OPTIONS},
            "application_capabilities": {"task_stores": "task-store-v1", "knowledge": "knowledge-entry-v1"},
            "finish_knowledge_schema": application_schema.knowledge_schema(),
            "delivery": {"schema_version": "record-inputs-v1", "targets": {name: [list(path) for path in paths] for name, paths in TARGETS.items()},
                         "reference_fields": ["run_id", "kind", "record_id", "record_digest", "path", "sha256"],
                         "meaning": "exact_content_copy_not_authority_or_new_evidence"}}
    return {**body, "sha256": canonical_hash(body)}


def describe(operation):
    return {"schema_version": "operation-catalog-v1", "operation": operation,
            "input_schema": input_schema(operation), "schema_hash": canonical_hash(input_schema(operation)),
            "source": "runtime_application" if operation in application_schema.OPTIONS else "runtime_core",
            "domain_fields_are_opaque": True,
            "application_capabilities": {"task_stores": "task-store-v1", "knowledge": "knowledge-entry-v1"},
            "envelope_extensions": {"task_id": "optional opaque store selector; omission retains the legacy store",
                                    "knowledge": "optional caller-selected note on finish; not a Core criterion",
                                    "input_bindings": "same-Run source reference + exact argument target, checked inside each transaction"},
            "delivery": catalog()["delivery"]}
