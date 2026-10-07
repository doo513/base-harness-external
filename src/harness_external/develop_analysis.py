"""Domain reasoning support: discover useful unknowns, then find and apply knowledge.

This module returns advice only. It does not generate task answers, persist Needs,
route by goal keywords, fetch sources or make Core lifecycle/gate decisions.
"""
import copy

from harness.common import canonical_hash


KINDS = ("knowledge", "observation", "decision", "verification")
NEED_QUALITY = {
    "unknown": "Find what the request and inspected evidence do not settle; do not turn an already stated requirement into a new question.",
    "decision_relevant": "Name the design choice, code change or verification decision that would differ depending on the answer.",
    "investigable": "Make the question specific to this project's behavior, data or dependencies and identify a source or observation that could answer it.",
    "sufficient": "Identify what finding would be enough for the next action; distinguish an observed fact, a tentative assumption and a decision still needing the user.",
}
PERSPECTIVES = (
    {"id": "outcome", "ask": "What observable change is requested, what must remain true, and which input or conflict rules are still unspecified?",
     "knowledge_refs": ["compatibility", "verification"]},
    {"id": "existing_behavior", "ask": "Which entrypoints, callers and data transformations produce the behavior being changed? Where could code and documentation disagree?",
     "knowledge_refs": ["structure", "compatibility"]},
    {"id": "design", "ask": "What is the smallest coherent change boundary? Which facts are local, which results depend on other inputs, and what observation distinguishes plausible designs?",
     "knowledge_refs": ["structure", "runtime"]},
    {"id": "change", "ask": "Which dependents, side effects and failure paths would this change affect? What must be ordered, preserved or recovered?",
     "knowledge_refs": ["structure", "recovery"]},
    {"id": "verification", "ask": "What result would expose a plausible but wrong implementation? Which assumptions can be checked now and which remain unobserved?",
     "knowledge_refs": ["verification", "runtime"]},
)
KNOWLEDGE_MAP = (
    {"id": "compatibility", "helps_answer": "What behavior or data must survive a change, including conflicting inputs?",
     "when": "Existing behavior, schemas or consumers constrain a change",
     "source_hints": ["Request constraints, public API/schema and their consumers", "Existing implementation plus callers of the affected behavior", "Representative inputs, conflict cases and compatibility tests"],
     "enables": "Choose preservation and conflict behavior from relevant evidence; ask for an externally owned decision rather than inventing it.",
     "limitation": "Current behavior may be a bug; examples do not establish an approved policy."},
    {"id": "structure", "helps_answer": "What produces the result, who consumes it, and which dependencies make a seemingly local change nonlocal?",
     "when": "Locating a change or choosing component responsibilities",
     "source_hints": ["The affected entrypoint and immediate producers/consumers", "Data structures, ownership boundaries and derived-state users", "Project/package configuration where resolution depends on layout"],
     "enables": "Separate input-local facts from derived results; choose reuse, invalidation and change boundaries from actual dependencies.",
     "limitation": "Directory names alone do not establish runtime or import behavior."},
    {"id": "runtime", "helps_answer": "Which runtime, dependency and platform assumptions affect the result?",
     "when": "Execution, packaging or platform behavior matters",
     "source_hints": ["Dependency and build configuration", "Supported environment documentation", "A bounded runtime capability probe"],
     "enables": "Select a compatible implementation and test environment; keep unsupported environments explicit instead of assuming equivalence.",
     "limitation": "A result in one environment does not establish behavior in another."},
    {"id": "recovery", "helps_answer": "Who owns partial changes, retries, concurrent updates and cleanup after failure?",
     "when": "The task changes persistent state or interacts with fallible operations",
     "source_hints": ["Writes and other effects, including helpers that hide them", "State/resource owners, error paths and transaction boundaries", "A targeted failure, replay or concurrency observation"],
     "enables": "Decide effect ordering, atomicity, retry behavior and cleanup ownership before relying on a successful happy path.",
     "limitation": "A happy-path result does not establish recovery or concurrency safety."},
    {"id": "verification", "helps_answer": "Which observable outcomes distinguish the required behavior from a plausible but wrong implementation?",
     "when": "Choosing evidence for a requirement or investigating a failed observation",
     "source_hints": ["Requirement conditions and known counterexamples", "Existing tests and their actual assertions", "Baseline and current Candidate observations"],
     "enables": "Turn a decision or assumption into a discriminating observation; use its outcome to keep, revise or reject the approach.",
     "limitation": "Test presence and passing linked checks do not prove complete goal coverage."},
)


def guidance():
    body = {"schema_version": "develop-analysis-v2", "revision": "2", "need_kinds": list(KINDS),
            "meaning": "advisory_reasoning_support_not_workflow", "persistence_required": False, "minimum_need_count": 0,
            "need_quality": copy.deepcopy(NEED_QUALITY), "analysis_perspectives": copy.deepcopy(list(PERSPECTIVES)),
            "knowledge_map": copy.deepcopy(list(KNOWLEDGE_MAP)),
            "discovery": {
                "select": "Form a task-specific question first, then choose a useful map entry or another source. Skip irrelevant perspectives; no category quota or goal-keyword routing.",
                "inspect": "Locate the relevant symbol, caller, configuration or assertion and inspect only enough context to answer the question. Use a focused observation if reading cannot distinguish alternatives.",
                "apply": "Connect the finding to the next design choice, code change or check. This is ordinary Host reasoning: no Need IDs, registration or state updates are needed. Preserve a consequential decision/source only when useful for handoff.",
                "revisit": "When implementation or measurement contradicts an assumption, refine the question or approach. Stop exploration when the next action is supported; do not read the entire map or treat an answer as goal proof.",
            },
            "boundary": "The Host reasons and uses its existing tools. Domain advice neither executes tools nor changes permissions, budgets, Run state, GoalContract, gates or Verifier observations."}
    return {**body, "map_hash": canonical_hash(body)}
