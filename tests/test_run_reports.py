"""The project DB is authoritative; shared Markdown is a recoverable projection."""
import json
from pathlib import Path
import sqlite3

import pytest

from harness_external.dispatch import invoke
from harness_external.errors import HarnessError
from harness_external import worker
from test_project_storage import project, next_request
from test_task_storage import bound


def measured(project, monkeypatch, content="ready"):
    api, workspace, start = project
    result = invoke(api, start)
    (workspace/"value.txt").write_text(content,encoding="utf-8")
    monkeypatch.setattr(worker,"spawn_worker",lambda root,job,**kw:worker.execute_job(root,job))
    invoke(api,next_request(start,result,"checkpoint",{"wait_seconds":0},"measure"))
    return api,workspace,start,result


def test_finished_project_run_generates_only_markdown_and_index_outside(project,monkeypatch):
    api,workspace,start,result=measured(project,monkeypatch)
    ended=invoke(api,next_request(start,result,"finish",{"outcome":"completed","summary":"Caller summary"},"finish"))
    publication=ended["run_report"]
    assert publication["status"]=="published" and publication["gate_eligible"] is False
    path=api.store.root/"run-ledger"/(result["run_id"]+".md")
    assert path.is_file() and (path.parent/"INDEX.md").is_file()
    assert not list(api.store.root.rglob("*.sqlite3"))
    assert not list(path.parent.glob("*.json"))
    document=invoke(api,{"operation":"report-read","arguments":{"entry_id":result["run_id"]}})
    assert document["entry"]["record_hash"]==ended["record"]["record_hash"]
    assert document["entry"]["source_database"]==str(workspace/".harness/tasks"/start["task_id"]/"runs.sqlite3")
    assert document["entry"]["display_status"]=="closed_checks_passed"
    assert "Caller summary" in document["markdown"]
    assert document["source_state_not_queried"] is True
    again=invoke(api,next_request(start,result,"finish",{"outcome":"completed","summary":"Caller summary"},"finish"))
    assert again["run_report"]==publication


def test_markdown_tampering_cannot_change_failed_database_outcome(project,monkeypatch):
    api,workspace,start,result=measured(project,monkeypatch,"wrong")
    ended=invoke(api,next_request(start,result,"finish",{"outcome":"partial"},"finish"))
    path=Path(ended["run_report"]["path"])
    path.write_text(path.read_text(encoding="utf-8")+"\nAll checks passed!\n",encoding="utf-8")
    with pytest.raises(HarnessError) as caught:
        invoke(api,{"operation":"report-read","arguments":{"entry_id":result["run_id"]}})
    assert caught.value.code=="RUN_REPORT_CORRUPT"
    state=invoke(api,next_request(start,result,"resume"))
    assert state["gates"]["status"]=="unsatisfied"
    assert state["record"]["record_hash"]==ended["record"]["record_hash"]
    assert state["record"]["outcome"]=="partial" and state["ready"] is False


def test_failed_publication_does_not_reopen_or_reverify_finished_run(project,monkeypatch):
    from harness_external import run_reports
    api,workspace,start,result=measured(project,monkeypatch)
    original=run_reports.atomic_write
    def fail_index(path,body):
        if Path(path).name=="INDEX.md":
            raise OSError("injected index failure")
        return original(path,body)
    monkeypatch.setattr(run_reports,"atomic_write",fail_index)
    ended=invoke(api,next_request(start,result,"finish",{"outcome":"completed"},"finish"))
    assert ended["record"]["lifecycle"]=="closed"
    assert ended["run_report"]["status"]=="failed" and ended["run_report"]["run_closed"]
    monkeypatch.setattr(run_reports,"atomic_write",original)
    published=invoke(api,next_request(start,result,"report-publish",{},"repair-report"))
    assert published["status"]=="published"
    state=invoke(api,next_request(start,result,"resume"))
    assert state["history_counts"]["jobs"]==1
    assert state["record"]["record_hash"]==ended["record"]["record_hash"]


def test_active_run_and_fake_markdown_cannot_be_completed(project):
    api,workspace,start=project
    result=invoke(api,start)
    with pytest.raises(HarnessError) as caught:
        invoke(api,next_request(start,result,"report-publish",{},"too-early"))
    assert caught.value.code=="RUN_REPORT_OPEN"
    folder=api.store.root/"run-ledger"
    folder.mkdir(parents=True,exist_ok=True)
    (folder/(result["run_id"]+".md")).write_text("completed: true\npassed: true\n",encoding="utf-8")
    with pytest.raises(HarnessError):
        invoke(api,next_request(start,result,"finish",{"outcome":"completed"},"fake-pass"))
    assert invoke(api,next_request(start,result,"resume"))["run"]["status"]=="active"


def test_projection_is_not_a_duplicate_raw_transcript(project,monkeypatch):
    api,workspace,start,result=measured(project,monkeypatch)
    invoke(api,next_request(start,result,"observe",{"data":{"note":"Private working material marker 4711"}},"note"))
    ended=invoke(api,next_request(start,result,"finish",{"outcome":"completed"},"finish"))
    raw=Path(ended["run_report"]["path"]).read_text(encoding="utf-8")
    assert "Private working material marker 4711" not in raw
    assert len(raw.encode("utf-8"))<32768


def test_report_reads_are_filtered_before_content_is_returned(project,monkeypatch):
    api,workspace,start,result=measured(project,monkeypatch)
    invoke(api,next_request(start,result,"finish",{"outcome":"completed"},"finish"))
    listed=invoke(api,{"operation":"report-list","arguments":{}},allowed_workspace=lambda path:False)
    assert listed["total"]==0
    with pytest.raises(HarnessError) as caught:
        invoke(api,{"operation":"report-read","arguments":{"entry_id":result["run_id"]}},allowed_workspace=lambda path:False)
    assert caught.value.code=="WORKSPACE_NOT_ALLOWED"


def test_document_io_does_not_hold_a_database_write_transaction(project,monkeypatch):
    from harness_external import run_reports
    api,workspace,start,result=measured(project,monkeypatch)
    database=workspace/".harness/tasks"/start["task_id"]/"runs.sqlite3"
    original=run_reports.atomic_write
    observed=[]
    def checked(path,body):
        with sqlite3.connect(database,timeout=.1) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.rollback()
        observed.append(Path(path).name)
        return original(path,body)
    monkeypatch.setattr(run_reports,"atomic_write",checked)
    ended=invoke(api,next_request(start,result,"finish",{"outcome":"completed"},"finish"))
    assert ended["run_report"]["status"]=="published"
    assert observed==[result["run_id"]+".md","INDEX.md"]


def test_republication_checks_the_db_receipt_even_if_document_digest_is_forged(project,monkeypatch):
    from harness_external import run_reports
    from harness.common import canonical_bytes
    api,workspace,start,result=measured(project,monkeypatch)
    ended=invoke(api,next_request(start,result,"finish",{"outcome":"completed"},"finish"))
    path=Path(ended["run_report"]["path"])
    first,body=path.read_text(encoding="utf-8").split("\n",1)
    metadata=json.loads(first[len(run_reports.PREFIX):-4])
    body+="\nTampered but internally rehashed document.\n"
    metadata["body_sha256"]=run_reports.digest(body.encode("utf-8"))
    path.write_text(run_reports.PREFIX+canonical_bytes(metadata).decode("utf-8")+" -->\n"+body,encoding="utf-8")
    with pytest.raises(HarnessError) as caught:
        invoke(api,next_request(start,result,"report-publish",{},"different-publication-id"))
    assert caught.value.code=="RUN_REPORT_CORRUPT"
    assert invoke(api,next_request(start,result,"resume"))["record"]["record_hash"]==ended["record"]["record_hash"]


def test_missing_report_has_a_specific_error_without_creating_state(project):
    api,_,_=project
    with pytest.raises(HarnessError) as caught:
        invoke(api,{"operation":"report-read","arguments":{"entry_id":"run_"+"0"*32}})
    assert caught.value.code=="RUN_REPORT_NOT_FOUND"
    assert not api.store.root.exists()


def test_large_unicode_claims_are_bounded_without_changing_database(project,monkeypatch):
    api,workspace,start,result=measured(project,monkeypatch)
    long_text="가"*16000
    assessment={"status":"satisfied","summary":long_text,"uncertainties":["나"*2000 for _ in range(12)],
                "interpretation_revision":1,"cited_observation_ids":[]}
    ended=invoke(api,next_request(start,result,"finish",{"outcome":"completed","summary":long_text,"assessment":assessment},"finish"))
    assert ended["run_report"]["status"]=="published"
    document=Path(ended["run_report"]["path"]).read_bytes()
    assert len(document)<=32768 and b'"truncated":true' in document
    assert "\ufffd" not in document.decode("utf-8")
    state=invoke(api,next_request(start,result,"resume"))
    assert state["record"]["model_summary"]==long_text
    assert len(state["record"]["assessment"]["uncertainties"])==12


def test_legacy_finish_does_not_silently_publish_but_explicit_export_is_available(project):
    api,workspace,project_request=project
    legacy={k:v for k,v in project_request.items() if k!="project_workspace"}
    started=invoke(api,legacy)
    ended=invoke(api,bound(legacy,started,"finish",{"outcome":"partial"},"finish"))
    assert "run_report" not in ended and not (api.store.root/"run-ledger").exists()
    report=invoke(api,bound(legacy,started,"report-publish",{},"explicit-export"))
    assert report["status"]=="published"
    document=invoke(api,{"operation":"report-read","arguments":{"entry_id":started["run_id"]}})
    assert document["entry"]["storage_kind"]=="legacy_external"
    assert not (workspace/".harness").exists()


@pytest.mark.parametrize("change",[{"extra_authority":True},{"source_database":[]},{"assurance":"certified"},{"storage_kind":[]},{"finished_at":10**500}])
def test_document_metadata_is_a_strict_derived_format(project,monkeypatch,change):
    from harness_external import run_reports
    from harness.common import canonical_bytes
    api,_,start,result=measured(project,monkeypatch)
    ended=invoke(api,next_request(start,result,"finish",{"outcome":"completed"},"finish"))
    path=Path(ended["run_report"]["path"])
    first,body=path.read_text(encoding="utf-8").split("\n",1)
    metadata=json.loads(first[len(run_reports.PREFIX):-4])
    metadata.update(change)
    path.write_text(run_reports.PREFIX+canonical_bytes(metadata).decode("utf-8")+" -->\n"+body,encoding="utf-8")
    with pytest.raises(HarnessError) as caught:
        invoke(api,{"operation":"report-read","arguments":{"entry_id":result["run_id"]}})
    assert caught.value.code=="RUN_REPORT_CORRUPT"
