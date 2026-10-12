import base64
import copy
import json

import pytest

from harness.common import canonical_bytes, canonical_hash
from harness_external.context import apply_page
from harness_external.detail_projection import MAX_INLINE_BYTES, project, restore
from harness_external.dispatch import invoke
from harness_external.errors import HarnessError
from harness_external.service import Harness
from harness_external.worker import execute_job


@pytest.fixture
def task(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "result").write_text("needle " + "한글" * 9000, encoding="utf-8")
    api = Harness(tmp_path / "state")
    monkeypatch.setattr("harness_external.worker.spawn_worker", lambda *args: None)
    parameters = {"profile": "structural", "inputs": ["result"], "artifacts": ["result"],
                  "expectations": [{"id": "result", "path": "result", "operator": "contains", "expected": "needle"}]}
    run = api.start(domain_id="develop", workspace=str(workspace), goal="Preserve all original context. " * 400,
                    request_id="start", mode="exploratory", required_checks=["domain.result"],
                    constraints=["Never discard uncertainty"], parameters=parameters)["run_id"]
    return api, run, workspace, parameters


def decoded_details(api, run, kind, projection):
    result = {}
    for ref in projection["detail_refs"]:
        request = {key: projection["source"][key] for key in ("record_id", "record_digest")}
        request.update(path=ref["path"], sha256=ref["sha256"], offset=0, limit=4093)
        chunks = []
        while True:
            chunk = api.records(run, kind, detail=request)
            data = base64.b64decode(chunk["data"])
            assert len(data) <= 4093 and chunk["encoding"] == "base64-canonical-json"
            chunks.append(data)
            if chunk["next_offset"] is None:
                break
            request["offset"] = chunk["next_offset"]
        encoded = b"".join(chunks)
        assert len(encoded) == ref["byte_count"]
        value = json.loads(encoded)
        assert canonical_hash(value) == ref["sha256"]
        result[ref["sha256"]] = value
    return result


def test_large_note_projection_and_unicode_detail_chunks_restore_exact_body(task):
    api, run, _, _ = task
    api.observe(run, {"note": "중요한 맥락 " * 1200}, "note")
    full = api.records(run, "notes")["items"][0]
    projection = api.records(run, "notes", view="references")["items"][0]
    assert projection["data"]["note"] is None and projection["detail_refs"]
    assert len(canonical_bytes(projection)) <= MAX_INLINE_BYTES
    assert restore(projection, decoded_details(api, run, "notes", projection)) == full
    assert api.resume(run)["measurement"]["status"] == "not_run"


def test_measured_observation_projection_restores_actual_record_without_second_body(task, monkeypatch):
    from harness_external import observation_links
    api, run, _, _ = task
    api.submit(run, "submit")
    job = api.verify(run, "verify")["job_id"]
    collected = []
    original = observation_links.report
    def inspected(links, checks, cases, pinned):
        collected.extend(checks)
        assert all("report" not in item for item in checks)
        assert all("phases" not in item["case"] for item in cases)
        return original(links, checks, cases, pinned)
    monkeypatch.setattr(observation_links, "report", inspected)
    execute_job(api.store.root, job)
    assert collected
    full = api.records(run, "measurements")["items"][0]
    projection = api.records(run, "measurements", view="references")["items"][0]
    assert len(canonical_bytes(projection)) < len(canonical_bytes(full)) / 4
    assert restore(projection, decoded_details(api, run, "measurements", projection)) == full
    with api.store.transaction(write=False) as connection:
        assert connection.execute("SELECT count(*) FROM measurements").fetchone()[0] == 1
        saved = api.store.job(connection, job)["result"]
        assert "observations" not in saved and len(saved["observation_refs"]) == 1


def test_compact_polling_preserves_status_and_context_restores_all_omitted_data(task):
    api, run, _, _ = task
    summary = api.resume(run)
    assert summary == api.resume(run, view="summary")
    compact = api.resume(run, view="compact")
    assert compact["gates"]["status"] == summary["gates"]["status"]
    assert compact["intent_ref"] == summary["intent"]["ref"] and compact["policy_ref"] == summary["policy"]["ref"]
    assert compact["measurement"]["status"] == summary["measurement"]["status"]
    assert len(canonical_bytes(compact)) < len(canonical_bytes(summary)) / 2
    assert compact["ready"] is False
    page, restored = api.context(run), {}
    while True:
        restored = apply_page(restored, page)
        if page["next_page"] is None:
            break
        page = api.context(run, page=page["next_page"])
    assert restored["field:intent"] == summary["intent"]
    assert restored["field:domain_preparation"] == summary["domain_preparation"]
    assert restored["interpretations:0"]["goal_summary"] == summary["intent"]["original_goal"]


def test_compact_reads_do_not_waive_observation_integrity(task):
    api, run, _, _ = task
    api.submit(run, "submit")
    job = api.verify(run, "verify")["job_id"]
    execute_job(api.store.root, job)
    assert api.status(run, job, view="compact")["job"]["result"]["status"] == "passed"
    with api.store.transaction() as connection:
        item = json.loads(connection.execute("SELECT data FROM measurements").fetchone()[0])
        item["report"]["observation"]["result"]["findings"][-1]["observed"] = "tampered"
        connection.execute("UPDATE measurements SET data=?", (canonical_bytes(item).decode(),))
    with pytest.raises(HarnessError, match="digest"):
        api.status(run, job, view="compact")


def test_detail_rejects_wrong_digest_and_superseded_record(task):
    api, run, _, _ = task
    api.observe(run, {"kind": "task", "task_id": "work", "note": "Work " * 1200}, "task")
    projection = api.records(run, "tasks", view="references")["items"][0]
    ref = projection["detail_refs"][0]
    request = {key: projection["source"][key] for key in ("record_id", "record_digest")}
    request.update(path=ref["path"], sha256=ref["sha256"])
    wrong = {**request, "sha256": "0" * 64}
    with pytest.raises(HarnessError) as error:
        api.records(run, "tasks", detail=wrong)
    assert error.value.code == "DETAIL_CHANGED"
    api.observe(run, {"kind": "task", "task_id": "work", "task_revision": 1, "state": "settled", "note": "Changed"}, "settled")
    with pytest.raises(HarnessError) as error:
        api.records(run, "tasks", detail=request)
    assert error.value.code == "DETAIL_CHANGED"


def test_observation_details_are_run_bound_and_dispatch_stays_read_only(task):
    api, run, workspace, parameters = task
    api.submit(run, "submit")
    job = api.verify(run, "verify")["job_id"]
    execute_job(api.store.root, job)
    projection = api.records(run, "measurements", view="references")["items"][0]
    ref = projection["detail_refs"][0]
    request = {key: projection["source"][key] for key in ("record_id", "record_digest")}
    request.update(path=ref["path"], sha256=ref["sha256"], limit=500)
    usage = api.resume(run)["usage"]
    result = invoke(api, {"operation": "records", "arguments": {"run_id": run, "kind": "measurements", "detail": request},
                         "context": {"workspace": str(workspace), "domain": "develop"}})
    assert len(base64.b64decode(result["data"])) == 500
    assert api.resume(run)["usage"] == usage
    foreign = api.start(domain_id="develop", workspace=str(workspace), goal="Another Run", request_id="other",
                        mode="exploratory", parameters=parameters)["run_id"]
    with pytest.raises(HarnessError) as error:
        api.records(foreign, "measurements", detail=request)
    assert error.value.code == "DETAIL_REFERENCE"


def test_projection_has_a_hard_inline_bound_and_roundtrip_checks_digest():
    value = {f"part{i}": {f"key{j}": "small text" * 20 for j in range(30)} for i in range(30)}
    projection = project(value, {"run_id": "run", "kind": "notes", "record_id": 0})
    assert len(canonical_bytes(projection)) <= MAX_INLINE_BYTES
    assert projection["data"] is None and projection["detail_refs"][0]["path"] == []
    assert restore(projection, {canonical_hash(value): value}) == value
    altered = copy.deepcopy(projection)
    altered["source"]["record_digest"] = "0" * 64
    with pytest.raises(HarnessError, match="changed"):
        restore(altered, {canonical_hash(value): value})
