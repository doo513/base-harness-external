"""Evaluation preparation must not silently reinterpret unavailable controls."""
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("lean_study", ROOT / "scripts/benchmark-lean-workflow.py")
study = importlib.util.module_from_spec(spec)
spec.loader.exec_module(study)


def driver():
    source = (ROOT / "scripts/study_subject_driver.py").read_text(encoding="utf-8")
    transformed = study.bounded_driver(source)
    namespace = {"__name__": "evaluation_fixture"}
    exec(compile(transformed, "bounded-evaluator", "exec"), namespace)
    return namespace


def test_driver_keeps_six_writers_and_bounds_live_concurrency(monkeypatch):
    namespace = driver()
    keys, live = [], []
    maxima = []
    class Process:
        def __init__(self, argv, **options):
            self.key, self.done = argv[4], False
            live.append(self)
            maxima.append(len(live))
        def poll(self):
            return 0 if self.done else None
        def wait(self, timeout):
            if not self.done:
                self.done = True
                live.remove(self)
                keys.append(self.key)
            return 0
        def kill(self):
            raise AssertionError("Successful children must not be killed")
    class Journal:
        def __init__(self, path):
            pass
        def read_all(self):
            return [{"key": key, "value": 1} for key in sorted(set(keys))]
    monkeypatch.setitem(sys.modules, "journal", SimpleNamespace(Journal=Journal))
    monkeypatch.setattr(namespace["subprocess"], "Popen", Process)
    result = namespace["observe"]({"operation": "journal_concurrent", "duplicate": False})
    assert result == {"exits": [0] * 6, "count": 6, "keys": ["k" + str(i) for i in range(6)]}
    assert max(maxima) == 2 and not live


def test_partial_spawn_failure_reaps_started_children(monkeypatch):
    namespace = driver()
    events = []
    class Process:
        def poll(self):
            return None
        def kill(self):
            events.append("kill")
        def wait(self, timeout):
            events.append("wait")
            return -9
    def launch(*args, **kwargs):
        if events:
            raise BlockingIOError("controlled process limit")
        events.append("started")
        return Process()
    monkeypatch.setitem(sys.modules, "journal", SimpleNamespace(Journal=object))
    monkeypatch.setattr(namespace["subprocess"], "Popen", launch)
    with pytest.raises(BlockingIOError):
        namespace["observe"]({"operation": "journal_concurrent", "duplicate": False})
    assert events == ["started", "kill", "wait"]


def test_unknown_driver_is_not_rewritten_by_heuristic():
    with pytest.raises(ValueError, match="inspect"):
        study.bounded_driver("unrelated")
