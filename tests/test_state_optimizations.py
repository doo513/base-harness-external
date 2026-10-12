import copy
import json
import time

import pytest

from harness.common import canonical_bytes, canonical_hash
from harness_external import context, journal
from harness_external.errors import HarnessError
from harness_external.service import Harness
from harness_external.store import Store
from harness_external.worker import execute_job


@pytest.fixture
def task(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "result").write_text("done", encoding="utf-8")
    api = Harness(tmp_path / "state")
    monkeypatch.setattr("harness_external.worker.spawn_worker", lambda *args: None)
    run = api.start(domain_id="develop", workspace=str(workspace), goal="Keep the result intact", request_id="start",
                    mode="exploratory", required_checks=["domain.result"],
                    parameters={"profile": "structural", "inputs": ["result"], "artifacts": ["result"],
                                "expectations": [{"id": "result", "path": "result", "operator": "equals", "expected": "done"}]})["run_id"]
    return api, run


def test_append_persists_only_changed_fields_and_new_records(task, monkeypatch):
    api, run = task
    for index in range(35):
        api.observe(run, {"kind": "decision", "note": f"Decision {index}"}, f"note-{index}")
    puts = []
    original = journal.put
    def counted(connection, run_id, seq, kind, position, value):
        puts.append((kind, position))
        return original(connection, run_id, seq, kind, position, value)
    monkeypatch.setattr(journal, "put", counted)
    api.observe(run, {"kind": "decision", "note": "One new decision"}, "new")
    assert ("observations", 35) in puts and ("activity", 35) in puts
    assert not any(kind in journal.COLLECTIONS and position < 35 for kind, position in puts)
    assert len(puts) <= 5


def test_nested_mutations_persist_but_detached_copies_do_not_dirty_original(task, monkeypatch):
    api, run = task
    with api.store.transaction() as connection:
        value = api.store.run(connection, run)
        detached = copy.deepcopy(value)
        detached["contract"]["limitations"].append("Only a detached copy")
        assert value._dirty_fields == set()
        value["domain_preparation"]["extra"] = {"nested": ["before"]}
        api.store.save_run(connection, value)
        value["domain_preparation"]["extra"]["nested"].append("after")
        api.store.save_run(connection, value)
    assert api.resume(run)["domain_preparation"]["extra"]["nested"] == ["before", "after"]


def test_tracked_nested_containers_keep_normal_self_extend_and_dict_mutators(task):
    api, run = task
    with api.store.transaction() as connection:
        value = api.store.run(connection, run)
        value["domain_preparation"]["extra"] = {"items": [1, 2]}
        items = value["domain_preparation"]["extra"]["items"]
        items.extend(items)
        assert items == [1, 2, 1, 2]
        value["domain_preparation"]["extra"] |= {"changed": True}
        assert value["domain_preparation"]["extra"].pop("changed") is True
        api.store.save_run(connection, value)
    assert api.resume(run)["domain_preparation"]["extra"]["items"] == [1, 2, 1, 2]


def test_context_page_does_not_expand_complete_reference_inventory(task, monkeypatch):
    api, run = task
    for index in range(35):
        api.observe(run, {"note": f"Note {index}"}, f"note-{index}")
    with api.store.transaction(write=False) as connection:
        snapshot = journal.head(connection, run)
        expected = context.inventory(connection, snapshot, None)
    monkeypatch.setattr(context, "inventory", lambda *args: pytest.fail("full inventory expanded"))
    monkeypatch.setattr(journal, "collection_refs", lambda *args, **kwargs: pytest.fail("full collection refs expanded"))
    start = len(snapshot["fields"]) + 1 + 1 + 30  # interpretations, checks, then late note positions
    token = context.encode({"target": context.cursor(snapshot), "base": None, "offset": start})
    page = api.context(run, page=token, limit=3)
    assert [(item["key"], item["ref"]) for item in page["items"]] == expected[start:start + 3]


def test_gate_lookup_uses_check_subject_index(task):
    api, run = task
    with api.store.transaction(write=False) as connection:
        plan = list(connection.execute("""EXPLAIN QUERY PLAN SELECT data FROM measurements WHERE run_id=?
            AND json_extract(data,'$.check_ref')=? AND json_extract(data,'$.subject')=? ORDER BY rowid DESC LIMIT 1""",
            (run, "{}", "{}")))
        assert any("measurements_check_subject" in row[-1] for row in plan)


def test_reference_validation_is_batched_and_preserves_every_digest(tmp_path):
    store = Store(tmp_path / "state")
    run, job = "run_" + "a" * 32, "job_" + "b" * 32
    refs = []
    with store.transaction() as connection:
        for index in range(270):
            observation_id = f"observation:{index}"
            item = {"run_id": run, "job_id": job, "observation_id": observation_id,
                    "subject": {"id": "subject"}, "check_ref": {"id": "check"},
                    "report": {"observation": {"runId": run, "taskId": job, "observationId": observation_id,
                                                 "subject": {"id": "subject"}, "checkRef": {"id": "check"}}}}
            item["record_hash"] = canonical_hash(item)
            connection.execute("INSERT INTO measurements VALUES(?,?,?,?)", (observation_id, run, job, canonical_bytes(item).decode()))
            refs.append({"observation_id": observation_id, "record_hash": item["record_hash"]})
        queries = []
        connection.set_trace_callback(queries.append)
        assert len(list(store._referenced(connection, "measurements", refs, run, job))) == 270
        assert len([q for q in queries if q.startswith("SELECT data FROM measurements")]) == 3
        altered = copy.deepcopy(refs)
        altered[-1]["record_hash"] = "0" * 64
        with pytest.raises(HarnessError, match="missing or changed"):
            list(store._referenced(connection, "measurements", altered, run, job))


def test_validated_result_reuse_is_transaction_local_and_write_invalidated(task):
    api, run = task
    api.submit(run, "submit")
    job_id = api.verify(run, "verify")["job_id"]
    execute_job(api.store.root, job_id)
    with api.store.transaction() as connection:
        job = api.store.job(connection, job_id)
        queries = []
        connection.set_trace_callback(queries.append)
        api.store.validate_result(connection, job)
        count = sum(q.startswith("SELECT data FROM measurements") for q in queries)
        assert count == 1
        api.store.validate_result(connection, job)
        assert sum(q.startswith("SELECT data FROM measurements") for q in queries) == count
        connection.execute("UPDATE jobs SET data=data WHERE job_id=?", (job_id,))
        api.store.validate_result(connection, job)
        assert sum(q.startswith("SELECT data FROM measurements") for q in queries) == count + 1
    with api.store.transaction(write=False) as connection:
        queries = []
        connection.set_trace_callback(queries.append)
        api.store.validate_result(connection, job)
        assert sum(q.startswith("SELECT data FROM measurements") for q in queries) == 1


def test_lease_renewal_is_guarded_and_does_not_rewrite_job_or_context(task, monkeypatch):
    api, run = task
    api.submit(run, "submit")
    job_id = api.verify(run, "verify")["job_id"]
    with api.store.transaction() as connection:
        job = api.store.job(connection, job_id)
        job.update(status="running", owner="owner", heartbeat_at=time.time() - 2)
        api.store.save_job(connection, job)
        before = connection.execute("SELECT data FROM jobs WHERE job_id=?", (job_id,)).fetchone()[0]
        seq = journal.head(connection, run)["seq"]
        with monkeypatch.context() as patch:
            patch.setattr(api.store, "job", lambda *args: pytest.fail("lease hydrated whole Job"))
            patch.setattr(api.store, "save_job", lambda *args: pytest.fail("lease rewrote whole Job"))
            assert api.store.renew_lease(connection, job_id, "owner", run, job["generation"])
            assert not api.store.renew_lease(connection, job_id, "other", run, job["generation"])
            assert not api.store.renew_lease(connection, job_id, "owner", run, job["generation"] + 1)
        assert connection.execute("SELECT data FROM jobs WHERE job_id=?", (job_id,)).fetchone()[0] == before
        assert journal.head(connection, run)["seq"] == seq
        renewed = api.store.job(connection, job_id)
        assert renewed["heartbeat_at"] > json.loads(before)["heartbeat_at"]
        api.store.save_job(connection, renewed)
        assert not connection.execute("SELECT 1 FROM job_heartbeats WHERE job_id=?", (job_id,)).fetchone()
    api.cancel(run, job_id, "cancel")
    with api.store.transaction() as connection:
        assert not api.store.renew_lease(connection, job_id, "owner", run, job["generation"])
