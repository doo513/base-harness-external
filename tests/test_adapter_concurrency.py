"""Concurrent publication, import isolation and one materialized command."""
from concurrent.futures import ThreadPoolExecutor
import importlib
import os
from pathlib import Path
import sys
import threading
import time
from types import ModuleType

import pytest

from harness_external.adapter_registry import AdapterRegistration, AdapterRegistry
from harness_external.domain import DevelopModule
from harness_external.errors import HarnessError
from harness_external.registry import DomainRegistry
from harness_external.service import Harness
from harness_external import worker
from test_adapter_registry import ProbeDomain, probe_registry


@pytest.fixture
def runs(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "probe_data.json").write_text('{"ok":true}')
    (workspace / "result.txt").write_text("result")
    api = Harness(tmp_path / "state", domains=DomainRegistry([ProbeDomain(), DevelopModule()]), adapters=probe_registry())
    target = api.start(domain_id="adapter-probe", goal="Observe input", workspace=str(workspace),
                       parameters={}, mode="exploratory", request_id="target")["run_id"]
    api.submit(target, "submit")
    other = api.start(domain_id="develop", goal="Observe a file", workspace=str(workspace), request_id="other",
                      parameters={"profile": "structural", "inputs": ["result.txt"], "artifacts": ["result.txt"],
                                  "expectations": [{"path": "result.txt", "operator": "equals", "expected": "result"}]})["run_id"]
    monkeypatch.setattr(worker, "spawn_worker", lambda *args: None)
    api.submit(other, "submit")
    job_id = api.verify(other, "verify")["job_id"]
    return api, target, other, job_id, workspace


def mutate(api, run_id, operation, request_id="change"):
    if operation == "revise":
        return api.revise(run_id, {"expected_revision": 1, "goal_summary": "More precise interpretation"}, request_id)
    if operation == "check":
        return api.register_check(run_id, {"check_id": "extra", "interpretation_revision": 1,
            "parameters": {"kind": "file", "path": "probe_data.json", "operator": "contains", "expected": "ok"}}, request_id)
    if operation == "verify":
        return api.verify(run_id, request_id)
    return api.finish(run_id, request_id, outcome="completed")


def pause_identity(monkeypatch, api, parties=1):
    entered, release = threading.Event(), threading.Event()
    original = api.adapters.identity
    threads, lock = set(), threading.Lock()
    def slow(adapter_id):
        with lock:
            first = threading.get_ident() not in threads
            threads.add(threading.get_ident())
            if len(threads) >= parties:
                entered.set()
        if first:
            assert release.wait(12), "Test did not release adapter preparation"
        return original(adapter_id)
    monkeypatch.setattr(api.adapters, "identity", slow)
    return entered, release


@pytest.mark.parametrize("operation", ["revise", "check", "verify", "finish"])
def test_slow_adapter_does_not_block_other_run_heartbeat_or_measurement(runs, monkeypatch, operation):
    api, target, other, job_id, _ = runs
    entered, release = pause_identity(monkeypatch, api)
    measuring, complete = threading.Event(), threading.Event()
    original = worker.MeasurementEngine.handle
    def held_measurement(engine, envelope):
        if envelope["type"] == "measure" and envelope["runId"] == other:
            measuring.set()
            assert complete.wait(8)
        return original(engine, envelope)
    monkeypatch.setattr(worker.MeasurementEngine, "handle", held_measurement)
    with ThreadPoolExecutor(max_workers=2) as pool:
        target_future = pool.submit(mutate, api, target, operation)
        try:
            assert entered.wait(4)
            verification = pool.submit(worker.execute_job, api.store.root, job_id)
            assert measuring.wait(4), "Another Run could not start its worker"
            before = api.status(other, job_id)["job"]["heartbeat_at"]
            deadline = time.monotonic() + 4
            while api.status(other, job_id)["job"]["heartbeat_at"] <= before:
                assert time.monotonic() < deadline, "Adapter preparation blocked the heartbeat"
                time.sleep(.05)
            complete.set()
            verification.result(timeout=4)
            assert api.status(other, job_id)["job"]["result"]["status"] == "passed"
            assert len(api.records(other, "measurements", job_id=job_id)["items"]) == 1
            assert not target_future.done()
        finally:
            complete.set()
            release.set()
        target_future.result(timeout=4)


@pytest.mark.parametrize("operation", ["revise", "check", "verify", "finish"])
def test_prepared_operation_cannot_overwrite_concurrent_run_change(runs, monkeypatch, operation):
    api, target, _, _, _ = runs
    entered, release = pause_identity(monkeypatch, api)
    before = api.status(target)["run"]
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(mutate, api, target, operation)
        try:
            assert entered.wait(4)
            note = api.observe(target, {"note": "Concurrent progress must survive"}, "concurrent-note")
        finally:
            release.set()
        with pytest.raises(HarnessError) as error:
            pending.result(timeout=4)
    assert error.value.code in {"RUN_CONFLICT", "VERIFICATION_CONFLICT"}
    current = api.status(target)["run"]
    assert current["actions"] == before["actions"] + 1
    assert current["status"] == "active" and current["active_job"] is None and current["record"] is None
    assert current["observations"][-1]["observation_id"] == note["observation"]["observation_id"]
    assert len(current["interpretations"]) == 1 and len(current["check_records"]) == 1


@pytest.mark.parametrize("operation", ["revise", "check", "verify", "finish"])
def test_same_request_concurrently_commits_once(runs, monkeypatch, operation):
    api, target, _, _, _ = runs
    entered, release = pause_identity(monkeypatch, api, parties=2)
    before = api.status(target)["run"]["actions"]
    spawns = []
    monkeypatch.setattr(worker, "spawn_worker", lambda *args: spawns.append(args))
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(mutate, api, target, operation)
        second = pool.submit(mutate, api, target, operation)
        try:
            assert entered.wait(5)
        finally:
            release.set()
        assert first.result(timeout=5) == second.result(timeout=5)
    state = api.status(target)["run"]
    assert state["actions"] == before + (operation != "finish")
    assert len(spawns) == int(operation == "verify")
    if operation == "revise":
        assert len(state["interpretations"]) == 2
    if operation == "check":
        assert len(state["check_records"]) == 2


def test_deadline_rechecked_after_preparation_and_error_replayed(runs, monkeypatch):
    api, target, _, _, _ = runs
    entered, release = pause_identity(monkeypatch, api)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(mutate, api, target, "revise")
        try:
            assert entered.wait(4)
            with api.store.transaction() as connection:
                run = api.store.run(connection, target)
                run["deadline_at"] = time.time() - 1
                api.store.save_run(connection, run)
        finally:
            release.set()
        with pytest.raises(HarnessError) as error:
            pending.result(timeout=4)
    assert error.value.code == "DEADLINE_EXCEEDED"
    def forbidden(*args):
        pytest.fail("A replay must not repeat adapter preparation")
    monkeypatch.setattr(api.adapters, "identity", forbidden)
    with pytest.raises(HarnessError) as replay:
        mutate(api, target, "revise")
    assert replay.value.code == error.value.code
    assert api.status(target)["run"]["status"] == "blocked"


def test_finish_rejects_a_new_candidate_during_preparation(runs, monkeypatch):
    api, target, _, _, workspace = runs
    entered, release = pause_identity(monkeypatch, api)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(mutate, api, target, "finish")
        try:
            assert entered.wait(4)
            (workspace / "probe_data.json").write_text('{"ok":false}')
            candidate = api.submit(target, "new-candidate")["candidate"]
        finally:
            release.set()
        with pytest.raises(HarnessError) as error:
            pending.result(timeout=4)
    assert error.value.code == "RUN_CONFLICT"
    current = api.status(target)["run"]
    assert current["candidate"] == candidate and current["record"] is None


def test_plugin_loading_cannot_redirect_an_unrelated_import(tmp_path, monkeypatch):
    trusted, plugin = tmp_path / "trusted", tmp_path / "plugin"
    trusted.mkdir()
    plugin.mkdir()
    (trusted / "unrelated_import_probe.py").write_text("VALUE = 'host'\n")
    (plugin / "unrelated_import_probe.py").write_text("VALUE = 'plugin'\n")
    support = ModuleType("adapter_import_barrier")
    support.entered, support.release = threading.Event(), threading.Event()
    monkeypatch.setitem(sys.modules, "adapter_import_barrier", support)
    monkeypatch.syspath_prepend(str(trusted))
    original_path = list(sys.path)
    fixture = (Path(__file__).parent / "fixtures/probe_adapter_fixture.py").read_text()
    (plugin / "isolated_probe.py").write_text(
        "from adapter_import_barrier import entered, release\nentered.set()\nassert release.wait(8)\n" + fixture)
    registry = AdapterRegistry([AdapterRegistration("fixture-probe-v1", "isolated_probe:ProbeAdapter", import_root=str(plugin))])
    with ThreadPoolExecutor(max_workers=1) as pool:
        loading = pool.submit(registry.resolve, "fixture-probe-v1")
        try:
            assert support.entered.wait(4)
            assert list(sys.path) == original_path
            module = importlib.import_module("unrelated_import_probe")
            assert module.VALUE == "host"
        finally:
            support.release.set()
            sys.modules.pop("unrelated_import_probe", None)
        loading.result(timeout=4)
    assert list(sys.path) == original_path and "isolated_probe" not in sys.modules


def test_plugin_roots_and_relative_imports_remain_isolated_after_load(tmp_path):
    registries = []
    fixture = (Path(__file__).parent / "fixtures/probe_adapter_fixture.py").read_text()
    for label in ("first", "second"):
        root = tmp_path / label
        package = root / "sample_plugin"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("")
        (package / "helper.py").write_text("LABEL = " + repr(label) + "\n")
        (package / "adapter.py").write_text(fixture + "\ndef lazy_label():\n    from .helper import LABEL\n    return LABEL\n")
        registry = AdapterRegistry([AdapterRegistration("fixture-probe-v1", "sample_plugin.adapter:ProbeAdapter", import_root=str(root))])
        registry.resolve("fixture-probe-v1")
        registries.append(registry)
    modules = [sys.modules[type(registry.resolve("fixture-probe-v1")).__module__] for registry in registries]
    assert [module.lazy_label() for module in modules] == ["first", "second"]
    assert modules[0] is not modules[1]
    assert "sample_plugin" not in sys.modules
    assert registries[0].identity("fixture-probe-v1") == registries[1].identity("fixture-probe-v1")


@pytest.mark.skipif(os.environ.get("BASE_HARNESS_EXTERNAL_LIVE") != "1", reason="single command materialization through actual Sandbox")
def test_live_worker_executes_the_single_materialized_command(runs, monkeypatch):
    from harness_external import adapter_execution
    api, target, _, _, _ = runs
    original = adapter_execution.command
    calls = []
    def changing_command(*args, **kwargs):
        execution = original(*args, **kwargs)
        calls.append(execution)
        execution["argv"].append("materialization-" + str(len(calls)))
        return execution
    monkeypatch.setattr(adapter_execution, "command", changing_command)
    job_id = api.verify(target, "verify")["job_id"]
    worker.execute_job(api.store.root, job_id)
    job = api.status(target, job_id)["job"]
    assert job["status"] == "completed" and job["result"]["status"] == "passed", job
    assert len(calls) == 1
    assert api.records(target, "cases", job_id=job_id)["items"][0]["case"]["outcome"] == "passed"


@pytest.mark.skipif(os.environ.get("BASE_HARNESS_EXTERNAL_LIVE") != "1", reason="actual command identity in paired Sandbox measurements")
def test_live_different_materialized_commands_are_not_comparable(runs, monkeypatch):
    from harness_external import adapter_execution
    api, _, _, _, workspace = runs
    target = api.start(domain_id="adapter-probe", goal="Compare the same invocation", workspace=str(workspace),
                       parameters={}, mode="exploratory", capture_baseline=True, request_id="paired")["run_id"]
    api.submit(target, "submit")
    original, calls = adapter_execution.command, []
    def changing_command(*args, **kwargs):
        execution = original(*args, **kwargs)
        calls.append(execution)
        execution["argv"].append("invocation-" + str(len(calls)))
        return execution
    monkeypatch.setattr(adapter_execution, "command", changing_command)
    job_id = api.verify(target, "compare", compare_baseline=True)["job_id"]
    worker.execute_job(api.store.root, job_id)
    result = api.status(target, job_id)["job"]["result"]
    assert result["status"] == "passed" and len(calls) == 2
    comparison = result["baseline_comparison"]["changes"][0]
    assert comparison["status"] == "inconclusive"
    assert "recorded_environment_changed" in comparison["incomparable_reasons"]
