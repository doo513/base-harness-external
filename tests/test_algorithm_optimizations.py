"""Behavior and operation-count regressions for role-preserving optimizations."""
from collections import Counter
from pathlib import Path
import copy
import queue
import threading

import pytest

from harness_external.domain import DevelopModule, input_scope
from harness_external.errors import HarnessError
from harness_external.observation_links import report
from harness_external.pytest_adapter import Collector
from harness_external.registry import DomainRegistry
from harness_external import develop_analysis, domain
from harness_external.adapter_execution import MAX_PENDING_EVENTS, _offer
from harness_external.check_preparation import AdapterCheckPreparation


def discovered(count):
    observer = Collector("token")
    observer.feed({"version": 1, "token": "token", "seq": 1, "event": "session_start",
                   "framework": "pytest", "framework_version": "test"})
    for index in range(count):
        observer.feed({"version": 1, "token": "token", "seq": index + 2, "event": "discovered",
                       "case_id": f"tests/test_cost.py::test_{index}", "path": "tests/test_cost.py"})
    return observer


def test_case_selection_does_not_search_the_input_list_per_case():
    class Selection(list):
        def __contains__(self, value):
            raise AssertionError("Selection must be indexed once, not searched for every case")
    observer = discovered(3000)
    selected = Selection(list(observer.cases)[::2])
    observer.feed({"version": 1, "token": "token", "seq": 3002, "event": "collection_finish",
                   "selected": selected, "errors": 0})
    assert observer.collection_complete
    assert sum(case["selected"] for case in observer.cases.values()) == 1500
    assert all(case["outcome"] == "not_selected" for case in list(observer.cases.values())[1::2])


@pytest.mark.parametrize("selection", [[{}], [None], ["unknown"], ["tests/test_cost.py::test_0"] * 2, "not a list"])
def test_indexed_selection_preserves_protocol_rejections(selection):
    observer = discovered(1)
    with pytest.raises(HarnessError) as error:
        observer.feed({"version": 1, "token": "token", "seq": 3, "event": "collection_finish",
                       "selected": selection, "errors": 0})
    assert error.value.code == "CASE_REPORT_PROTOCOL"
    assert observer.collection_finished is False


@pytest.mark.parametrize("paths,valid", [
    (["a", "a-b", "a/file"], False),
    (["a-b", "a/file", "a/other"], True),
    (["a/b", "a/b/c"], False),
    (["A.txt", "a.txt"], False),
    (["한글", "한글/file.txt"], False),
    (["한글-a", "한글/file.txt"], True),
    ([], True),
])
def test_component_scope_preserves_prefix_and_case_collision_semantics(paths, valid):
    if valid:
        assert input_scope(paths, partial=True) == frozenset(paths)
    else:
        with pytest.raises(HarnessError) as error:
            input_scope(paths, partial=True)
        assert error.value.code == "INVALID_PATH"


def test_domain_identity_reads_each_source_once_without_cross_call_cache(monkeypatch):
    reads = Counter()
    original = Path.read_bytes
    def counted(path):
        reads[path.resolve()] += 1
        return original(path)
    monkeypatch.setattr(Path, "read_bytes", counted)
    registry = DomainRegistry([DevelopModule()])
    first = registry.identity("develop")
    assert reads and max(reads.values()) == 1
    reads.clear()
    assert registry.identity("develop") == first
    assert reads and max(reads.values()) == 1


def test_requirement_join_indexes_scenarios_without_changing_order_or_unknowns():
    class Scenarios(list):
        iterations = 0
        def __iter__(self):
            self.iterations += 1
            return super().__iter__()
    scenarios = Scenarios({"scenario_id": f"s{i}", "requirement_id": f"r{i}", "check_id": "check",
                           "case_id": f"opaque {i}", "required": False} for i in range(100))
    links = {"schema_version": "requirement-case-links-v1", "scenarios": scenarios,
             "requirements": [{"requirement_id": f"r{i}", "check_ids": ["check"], "known_gap_ids": []} for i in range(100)]}
    job = {"checks": [{"check_id": "check", "ref": {"id": "check"}}], "candidate": {"subject": {"id": "subject"}}}
    result = report(links, [], [], job)
    assert scenarios.iterations <= 3
    assert [r["requirement_id"] for r in result["requirements"]] == [f"r{i}" for i in range(100)]
    assert all(r["scenarios"][0]["status"] == "unconfirmed" for r in result["requirements"])
    assert all(not r["linked_cases_passed"] for r in result["requirements"])


def test_acceptance_reuses_normalization_only_inside_one_compilation(monkeypatch):
    class Preparation(AdapterCheckPreparation):
        def __init__(self):
            super().__init__()
            self.calls = 0
        def parameters(self, value):
            self.calls += 1
            return super().parameters(value)
    preparation = Preparation()
    module = DevelopModule(check_preparation=preparation)
    policy = {"parameters": {"profile": "execution", "inputs": ["main.py", "tests/test_main.py"], "artifacts": ["main.py"],
                             "expectations": [{"id": "main", "path": "main.py", "operator": "contains", "expected": "main"}],
                             "test_commands": [{"id": "behavior", "argv": ["python3", "tests/test_main.py"]}]},
              "required_check_ids": ["domain.main", "domain.behavior"], "bundle": {"files": ["tests/test_main.py"]},
              "requirements": [{"id": "main", "statement": "Execute main behavior", "minimum_evidence": "command",
                                "check_ids": ["domain.main", "domain.behavior"]}]}
    calls, original = [], domain.normalize_command
    def counted(value):
        calls.append(copy.deepcopy(value))
        return original(value)
    monkeypatch.setattr(domain, "normalize_command", counted)
    result = module.prepare_acceptance("Run main", {}, {}, policy=policy)
    assert preparation.calls == 2  # Policy aliases, then the caller parameters.
    assert len(calls) == 1
    assert result["contract"]["requirements"][0]["minimum_evidence"] == "command"
    assert "test_commands" in policy["parameters"]  # No mutation of configured input.
    assert module.prepare_acceptance("Run main", {}, {}, policy=policy) == result
    assert len(calls) == 2 and preparation.calls == 4  # No cross-request cache.


def test_advisory_indexes_are_immutable_and_default_delivery_is_detached():
    original = develop_analysis.guidance()
    with pytest.raises(TypeError):
        develop_analysis.PERSPECTIVE_INDEX["design"]["ask"] = "changed"
    with pytest.raises(TypeError):
        develop_analysis.KNOWLEDGE_INDEX["runtime"] = {}
    original["knowledge_map"].clear()
    assert len(develop_analysis.guidance()["knowledge_map"]) == 5
    selected = develop_analysis.guidance(perspectives=["change"])
    assert [item["id"] for item in selected["analysis_perspectives"]] == ["change"]
    assert {item["id"] for item in selected["knowledge_map"]} == {"structure", "recovery"}
    with pytest.raises(ValueError):
        develop_analysis.guidance(knowledge=["not-defined"])


@pytest.mark.parametrize("stop", ["abort", "shutdown"])
def test_full_transport_queue_does_not_trap_producer_on_stop(stop):
    messages = queue.Queue(maxsize=MAX_PENDING_EVENTS)
    for index in range(MAX_PENDING_EVENTS):
        messages.put(index)
    abort, shutdown, completed = threading.Event(), threading.Event(), threading.Event()
    results = []
    def producer():
        results.append(_offer(messages, "must not enqueue", shutdown, abort))
        completed.set()
    thread = threading.Thread(target=producer, daemon=True)
    thread.start()
    try:
        (abort if stop == "abort" else shutdown).set()
        assert completed.wait(1)
        thread.join(timeout=1)
        assert results == [False] and messages.qsize() == MAX_PENDING_EVENTS
    finally:
        abort.set()
        thread.join(timeout=1)
