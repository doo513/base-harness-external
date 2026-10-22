"""CIA boundary regressions for state, transport inputs and worker capacity."""
import os
from pathlib import Path
import sqlite3
import stat
import time

import pytest

from harness.json_codec import MeasurementProtocolError, decode
from harness_external.__main__ import load_json
from harness_external.artifact_io import read_bytes
from harness_external.dispatch import invoke
from harness_external.errors import HarnessError
from harness_external.service import Harness
from harness_external.store import Store
from harness_external.task_storage import task_id_for
from harness_external import worker

from test_task_storage import scoped_start, bound
from test_skill_workflow import helper, ROOT


@pytest.mark.skipif(os.name != "posix", reason="POSIX owner/mode enforcement")
def test_existing_state_permissions_are_hardened_before_reuse(tmp_path):
    root = tmp_path / "state"
    store = Store(root)
    with store.transaction():
        pass
    root.chmod(0o755)
    store.database.chmod(0o644)
    with Store(root).transaction(write=False):
        pass
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.database.stat().st_mode) == 0o600


def test_incomplete_existing_schema_fails_closed(tmp_path):
    store = Store(tmp_path / "state")
    with store.transaction():
        pass
    with sqlite3.connect(store.database) as connection:
        connection.execute("DROP TABLE job_heartbeats")
    with pytest.raises(HarnessError, match="incomplete") as error:
        with Store(store.root).transaction(write=False):
            pass
    assert error.value.code == "STATE_SCHEMA_CORRUPT"


@pytest.mark.skipif(os.name != "posix", reason="POSIX nonblocking special-file reads")
def test_state_and_cli_artifact_reads_reject_fifos(tmp_path):
    artifact = tmp_path / "artifact.fifo"
    database = tmp_path / "state" / "runs.sqlite3"
    database.parent.mkdir()
    os.mkfifo(artifact)
    os.mkfifo(database)
    with pytest.raises(HarnessError) as error:
        read_bytes(artifact, 1024)
    assert error.value.code == "ARTIFACT_TYPE"
    with pytest.raises(HarnessError) as error:
        load_json(str(artifact))
    assert error.value.code == "JSON_FILE_ERROR"
    with pytest.raises(HarnessError) as error:
        with Store(database.parent).transaction(write=False):
            pass
    assert error.value.code == "STATE_PERMISSIONS"


def test_task_database_cannot_be_swapped_between_manifests(tmp_path):
    state = tmp_path / "state"
    api = Harness(state)
    work_a, work_b = tmp_path / "a", tmp_path / "b"
    for work in (work_a, work_b):
        work.mkdir()
        (work / "value.txt").write_text("ready", encoding="utf-8")
    request_a, request_b = scoped_start(work_a, "a"), scoped_start(work_b, "b")
    result_a, result_b = invoke(api, request_a), invoke(api, request_b)
    paths = [state / "tasks" / request["task_id"] / "runs.sqlite3" for request in (request_a, request_b)]
    for path in paths:
        with sqlite3.connect(path) as connection:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    temporary = tmp_path / "swapped.sqlite3"
    paths[0].rename(temporary)
    paths[1].rename(paths[0])
    temporary.rename(paths[1])
    with pytest.raises(HarnessError) as error:
        invoke(Harness(state), bound(request_a, result_a, "resume"))
    assert error.value.code == "TASK_STORE_IDENTITY"
    with pytest.raises(HarnessError):
        invoke(Harness(state), bound(request_b, result_b, "resume"))


def test_workspace_aliases_have_one_task_identity(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    alias = workspace / "child" / ".."
    assert task_id_for("same", str(workspace), "develop") == task_id_for("same", str(alias), "develop")


def test_deep_json_is_rejected_before_python_recursion_failure():
    with pytest.raises(MeasurementProtocolError, match="nesting"):
        decode("[" * 65 + "]" * 65)


def test_structured_note_keeps_redaction_and_rejects_secret_payload(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "value.txt").write_text("ready", encoding="utf-8")
    api = Harness(tmp_path / "state")
    run = api.start(domain_id="develop", goal="g", workspace=str(workspace), mode="exploratory", request_id="start",
        parameters={"profile": "structural", "inputs": ["value.txt"], "artifacts": ["value.txt"],
                    "expectations": [{"path": "value.txt", "operator": "equals", "expected": "ready"}]})["run_id"]
    token = "Bearer " + "a" * 32
    observed = api.observe(run, {"note": token, "payload": {}}, "note")["observation"]
    assert observed["note"] == "[REDACTED]"
    with pytest.raises(HarnessError) as error:
        api.observe(run, {"note": "", "payload": {"fields": {"password": "synthetic"}}}, "secret")
    assert error.value.code == "PAYLOAD_SENSITIVE"


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes and FIFO")
def test_client_artifacts_are_private_and_special_inputs_fail_without_blocking(tmp_path):
    config_path = tmp_path / "config" / "client.json"
    helper.configure(config_path, str(ROOT), str(tmp_path / "state"), ephemeral=True)
    assert stat.S_IMODE(config_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    receipt = tmp_path / "receipt.json"
    helper.write_new_json(receipt, {"goal": "private"})
    assert stat.S_IMODE(receipt.stat().st_mode) == 0o600
    fifo = tmp_path / "input.fifo"
    os.mkfifo(fifo)
    with pytest.raises(helper.ClientError) as error:
        helper.read_regular_file(fifo, 1024)
    assert error.value.code == "FILE_TYPE"


@pytest.mark.skipif(os.name != "posix", reason="POSIX flock slots")
def test_global_worker_slots_are_bounded_across_task_stores(tmp_path):
    task_a = tmp_path / "state" / "tasks" / ("task_" + "a" * 32)
    task_b = tmp_path / "state" / "tasks" / ("task_" + "b" * 32)
    task_a.mkdir(parents=True)
    task_b.mkdir(parents=True)
    descriptors = [worker._worker_slot(task_a), worker._worker_slot(task_b)]
    try:
        with pytest.raises(OSError, match="capacity"):
            worker._worker_slot(task_a)
    finally:
        for descriptor in descriptors:
            os.close(descriptor)


@pytest.mark.skipif(os.name == "nt", reason="POSIX sandbox runtime roots")
def test_runtime_identity_rejects_absolute_paths_outside_mounted_command_roots(tmp_path):
    target = tmp_path / "private-command"
    target.write_text("synthetic secret-like bytes", encoding="utf-8")
    target.chmod(0o700)
    with pytest.raises(HarnessError) as error:
        worker.runtime_identity({"argv": [str(target)], "cwd": "."}, tmp_path)
    assert error.value.code == "RUNTIME_UNTRUSTED"


@pytest.mark.skipif(os.name != "posix", reason="POSIX capture recovery locks")
def test_cleanup_apply_is_idempotent_after_quarantine(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "value.txt").write_text("ready", encoding="utf-8")
    api = Harness(tmp_path / "state")
    run = api.start(domain_id="develop", goal="g", workspace=str(workspace), mode="exploratory", request_id="start",
        parameters={"profile": "structural", "inputs": ["value.txt"], "artifacts": ["value.txt"],
                    "expectations": [{"path": "value.txt", "operator": "equals", "expected": "ready"}]})["run_id"]
    abandoned = api.store.directory(run) / ".submit_deadbeef"
    abandoned.mkdir()
    (abandoned / ".capture.lock").write_bytes(b"")
    old = time.time() - 120
    os.utime(abandoned, (old, old))
    first = api.cleanup(run, "cleanup", apply=True, min_age_seconds=60)
    second = api.cleanup(run, "cleanup", apply=True, min_age_seconds=60)
    assert first == second
    assert first["items"][0]["state"] == "quarantined"
    assert len(list(api.store.directory(run).glob(".quarantine_*"))) == 1
