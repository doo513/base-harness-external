"""Small, model-free analysis index. Hosts choose questions and fetch sources."""
import copy

from harness.common import canonical_hash
from .errors import fields, require


KINDS = ("knowledge", "observation", "decision", "verification")
KNOWLEDGE_MAP = (
    {"id": "compatibility", "helps_answer": "What behavior or data must survive a change, including conflicting inputs?",
     "when": "Existing behavior, schemas or consumers constrain a change",
     "source_hints": ["User constraints and schema definitions", "Existing implementation and callers", "Representative inputs and migration history"],
     "limitation": "Current behavior may be a bug; examples do not establish an approved policy."},
    {"id": "structure", "helps_answer": "Where are entrypoints, module boundaries, dependencies and state ownership?",
     "when": "Locating a change or choosing component responsibilities",
     "source_hints": ["Project and package configuration", "Entrypoints and their call sites", "Relevant module interfaces and state transitions"],
     "limitation": "Directory names alone do not establish runtime or import behavior."},
    {"id": "runtime", "helps_answer": "Which runtime, dependency and platform assumptions affect the result?",
     "when": "Execution, packaging or platform behavior matters",
     "source_hints": ["Dependency and build configuration", "Supported environment documentation", "A bounded runtime capability probe"],
     "limitation": "A result in one environment does not establish behavior in another."},
    {"id": "recovery", "helps_answer": "Who owns partial changes, retries, concurrent updates and cleanup after failure?",
     "when": "The task changes persistent state or interacts with fallible operations",
     "source_hints": ["State ownership and resource lifecycle code", "Error paths and transaction boundaries", "A targeted failure or replay experiment"],
     "limitation": "A happy-path result does not establish recovery or concurrency safety."},
    {"id": "verification", "helps_answer": "Which observable outcomes distinguish the required behavior from a plausible but wrong implementation?",
     "when": "Choosing evidence for a requirement or investigating a failed observation",
     "source_hints": ["Requirement conditions and known counterexamples", "Existing tests and their actual assertions", "Baseline and current Candidate observations"],
     "limitation": "Test presence and passing linked checks do not prove complete goal coverage."},
)


def guidance():
    body = {"schema_version": "develop-analysis-v1", "revision": "1", "need_kinds": list(KINDS),
            "knowledge_map": copy.deepcopy(list(KNOWLEDGE_MAP)), "meaning": "advisory_index_not_required_steps"}
    return {**body, "map_hash": canonical_hash(body)}


def normalize(details, contract):
    fields(details, {"kind", "question", "reason", "resolution_criterion", "requirement_ids", "knowledge_refs"},
           {"kind", "question", "reason", "resolution_criterion"})
    require(isinstance(details["kind"], str) and details["kind"] in KINDS, "NEED_KIND", "Unknown Develop Need kind")
    for key in ("question", "reason", "resolution_criterion"):
        require(isinstance(details[key], str) and 0 < len(details[key].strip()) <= 2000, "NEED_DETAILS", key + " must be bounded nonblank text")
    result = copy.deepcopy(details)
    for key, known in (("requirement_ids", {r["id"] for r in contract.get("requirements", [])}),
                       ("knowledge_refs", {item["id"] for item in KNOWLEDGE_MAP})):
        refs = details.get(key, [])
        require(isinstance(refs, list) and len(refs) <= 32 and all(isinstance(ref, str) for ref in refs)
                and len(set(refs)) == len(refs) and set(refs) <= known, "NEED_REFERENCE", "Unknown or duplicate " + key)
        result[key] = list(refs)
    return result
