"""Develop-owned discovery schema. Normalization remains the admission authority."""
from .develop_requirements import KINDS
from .develop_cases import PERSPECTIVES


def obj(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


def array(items, maximum, minimum=0):
    return {"type": "array", "items": items, "minItems": minimum, "maxItems": maximum}


def schema():
    text = {"type": "string"}
    logical = {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$"}
    reference = obj({"source_id": text, "start_line": {"type": "integer", "minimum": 1},
                     "end_line": {"type": "integer", "minimum": 1}}, ("source_id", "start_line", "end_line"))
    requirement = obj({"id": logical, "statement": {"type": "string", "minLength": 1, "maxLength": 2000},
                       "kind": {"enum": sorted(KINDS)}, "when": text, "check_ids": array(text, 128),
                       "minimum_evidence": {"enum": ["file", "command", "testcase"]},
                       "source_refs": array(reference, 16)}, ("id", "statement", "check_ids"))
    expectation = obj({"id": logical, "path": text, "operator": {"enum": ["equals", "contains", "sha256"]},
                       "expected": text}, ("path", "operator", "expected"))
    command = obj({"kind": {"const": "command"}, "id": logical, "argv": array(text, 64, 1), "cwd": text,
                   "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 120}}, ("kind", "argv"))
    cases = obj({"kind": {"const": "cases"}, "id": logical, "adapter_id": text,
                 "selector": {"type": "object", "description": "Use the selected registered adapter's selector_schema"},
                 "required_cases": array(text, 128), "allowed_outcomes": array({"enum": ["passed", "skipped", "xfail", "xpass"]}, 4, 1),
                 "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 120}}, ("kind", "id", "adapter_id", "selector"))
    scenario = obj({"id": logical, "requirement_id": text, "check_id": text,
                    "kind": {"enum": ["positive", "negative", "boundary", "state_transition", "concurrency", "recovery", "fault_injection"]},
                    "required": {"type": "boolean"}, "test_ref": obj({"path": text, "case_id": text, "framework": text}, ("path", "case_id"))},
                   ("id", "requirement_id", "check_id", "kind", "test_ref"))
    coverage = obj({"schema_version": {"const": "develop-coverage-v2"}, "profile_id": logical, "profile_revision": text,
                    "scenarios": array(scenario, 128), "known_gaps": array(obj({"id": logical, "requirement_id": text, "reason": text},
                                                                               ("id", "requirement_id", "reason")), 128)},
                   ("schema_version", "profile_id", "profile_revision", "scenarios", "known_gaps"))
    return obj({"inputs": array(text, 128), "artifacts": array(text, 128), "expectations": array(expectation, 128),
                "execution_checks": array({"oneOf": [command, cases]}, 16), "profile": {"enum": ["structural", "execution"]},
                "validation_profile": {"enum": sorted(PERSPECTIVES)}, "requirements": array(requirement, 100), "coverage": coverage})
