"""Syntax for application routing and derived documents, not Domain policy."""
from .errors import OBSERVATION_REFERENCE_LIMIT

OPTIONS = {
    "list-tasks": {"workspace": "str", "domain": "str", "offset": "int", "limit": "int"},
    "knowledge-publish": {"run_id": "str", "data": "json"},
    "knowledge-find": {"query": "str", "domain": "str", "offset": "int", "limit": "int"},
    "knowledge-read": {"entry_id": "str"},
    "report-publish": {"run_id": "str"},
    "report-list": {"workspace": "str", "domain": "str", "offset": "int", "limit": "int"},
    "report-read": {"entry_id": "str"},
}
REQUIRED = {"list-tasks": set(), "knowledge-find": set(),
            "knowledge-publish": {"run_id", "data"}, "knowledge-read": {"entry_id"},
            "report-publish": {"run_id"}, "report-list": set(), "report-read": {"entry_id"}}
READS = {"list-tasks", "knowledge-find", "knowledge-read", "report-list", "report-read"}


def knowledge_schema():
    from .operation_schema import obj, strings
    return obj({
        "title": {"type": "string", "minLength": 1, "maxLength": 160},
        "when": {"type": "string", "minLength": 1, "maxLength": 2000},
        "lesson": {"type": "string", "minLength": 1, "maxLength": 12000},
        "limitations": {"type": "array", "maxItems": 16, "items": {"type": "string", "maxLength": 1000}},
        "keywords": {"type": "array", "maxItems": 16, "items": {"type": "string", "minLength": 1, "maxLength": 80}},
        "cited_observation_ids": strings(OBSERVATION_REFERENCE_LIMIT),
    }, ("title", "when", "lesson", "limitations"))


def input_schema(operation):
    from .operation_schema import obj
    kinds = {"str": {"type": "string"}, "int": {"type": "integer"}, "json": {"type": "object"}}
    properties = {key: dict(kinds[kind]) for key, kind in OPTIONS[operation].items()}
    if operation == "knowledge-publish":
        properties["data"] = knowledge_schema()
    return obj(properties, REQUIRED[operation])
