"""One authoritative project-local task DB; shared storage receives selected advice only."""
import copy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor

import pytest

from harness_external.dispatch import invoke
from harness_external.errors import HarnessError
from harness_external.service import Harness
from harness_external import worker
from test_task_storage import scoped_start, bound


@pytest.fixture
def project(tmp_path):
    work = tmp_path / "project"
    work.mkdir()
    (work / "value.txt").write_text("ready", encoding="utf-8")
    api = Harness(tmp_path / "shared")
    request = {**scoped_start(work), "project_workspace": str(work)}
    request["arguments"]["required_check"] = ["domain.value"]
    return api, work, request


def next_request(start, result, operation, arguments=None, request_id=None):
    return {**bound(start, result, operation, arguments, request_id), "project_workspace": start["project_workspace"]}


def test_project_database_is_local_and_restart_uses_the_same_store(project):
    api, work, request = project
    result = invoke(api, request)
    directory = work / ".harness/tasks" / request["task_id"]
    assert (directory / "runs.sqlite3").is_file()
    assert not (api.store.root / "tasks").exists()
    assert not (api.store.root / "runs.sqlite3").exists()
    assert (work / ".harness/.gitignore").read_text(encoding="utf-8") == "*\n"
    assert result["project_workspace"] == str(work)
    fresh = Harness(api.store.root)
    assert invoke(fresh, request)["run_id"] == result["run_id"]
    state = invoke(fresh, next_request(request, result, "resume"))
    assert state["run"]["run_id"] == result["run_id"]
    discovered = invoke(fresh, {"operation":"list-tasks", "arguments":{"workspace":str(work)}})
    assert discovered["total"] == 1
    assert discovered["items"][0]["project_workspace"] == str(work)


def test_local_measurements_and_selected_shared_markdown_remain_separate(project, monkeypatch):
    api, work, request = project
    result = invoke(api, request)
    monkeypatch.setattr(worker, "spawn_worker", lambda state, job, **kw: worker.execute_job(state, job))
    measured = invoke(api, next_request(request, result, "checkpoint", {"wait_seconds":0,"view":"brief"}, "measure"))
    assert measured["gates"]["status"] == "passed"
    facts = invoke(api, next_request(request, result, "records", {"kind":"measurements"}))["items"]
    note = {"title":"Selected lesson", "when":"A later task", "lesson":"Preserve a fixed input before checking it.",
            "limitations":["One synthetic file."], "cited_observation_ids":[facts[0]["observation_id"]]}
    finish = next_request(request, result, "finish", {"outcome":"completed"}, "finish")
    finish["knowledge"] = note
    closed = invoke(api, finish)
    assert closed["knowledge_publication"]["status"] == "published"
    assert (api.store.root / "knowledge/INDEX.md").is_file()
    assert len(list((api.store.root / "knowledge/entries").glob("*.md"))) == 1
    assert not (api.store.root / "tasks").exists()
    assert not list(api.store.root.rglob("*.sqlite3"))
    task = work / ".harness/tasks" / request["task_id"]
    manifests = list((task / "runs").glob("*/candidate_*/candidate.json"))
    assert manifests and all([f["path"] for f in json.loads(p.read_text(encoding="utf-8"))["manifest"]["files"]] == ["value.txt"] for p in manifests)


@pytest.mark.parametrize("field", ["inputs", "artifacts"])
def test_control_files_cannot_enter_domain_submission_scope(project, field):
    api, work, request = project
    bad = copy.deepcopy(request)
    bad["arguments"]["parameters"][field].append(".harness/private.json")
    if field == "artifacts":
        bad["arguments"]["parameters"]["inputs"].append(".harness/private.json")
        bad["arguments"]["parameters"]["expectations"].append({"id":"private","path":".harness/private.json","operator":"contains","expected":"private"})
    with pytest.raises(HarnessError) as caught:
        invoke(api, bad)
    assert caught.value.code == "WORKSPACE_CONTROL_PATH"


def test_old_external_task_is_not_moved_or_implicitly_selected(project):
    api, work, project_request = project
    old_request = {k:v for k,v in project_request.items() if k != "project_workspace"}
    old = invoke(api, old_request)
    path = api.store.root / "tasks" / old_request["task_id"] / "runs.sqlite3"
    assert path.is_file()
    with pytest.raises(HarnessError) as caught:
        invoke(api, next_request(project_request, old, "resume"))
    assert caught.value.code == "TASK_STORE_MISSING"
    assert invoke(api, bound(old_request, old, "resume"))["run"]["run_id"] == old["run_id"]
    assert not (work / ".harness").exists()


def test_project_route_cannot_be_changed_to_another_workspace(project, tmp_path):
    api, work, request = project
    other = tmp_path / "other"
    other.mkdir()
    bad = {**request,"project_workspace":str(other)}
    with pytest.raises(HarnessError) as caught:
        invoke(api, bad)
    assert caught.value.code == "RUN_CONTEXT_MISMATCH"
    assert not (other / ".harness").exists()


def test_project_control_symlinks_fail_closed(project, tmp_path):
    api, work, request = project
    target = tmp_path / "foreign"
    target.mkdir()
    (work / ".harness").symlink_to(target, target_is_directory=True)
    with pytest.raises(HarnessError):
        invoke(api, request)
    assert not list(target.iterdir())


def test_project_local_state_requires_the_explicit_routing_port(project):
    _, work, _ = project
    arbitrary = Harness(work / "state")
    with pytest.raises(HarnessError) as caught:
        arbitrary.start(domain_id="develop", goal="Do not bypass storage selection", workspace=str(work), request_id="bad",mode="exploratory",parameters={})
    assert caught.value.code == "STATE_WORKSPACE_OVERLAP"


def test_control_scope_cannot_be_added_during_revision(project):
    api, work, request = project
    started = invoke(api, request)
    revised = next_request(request, started, "revise", {"data":{"expected_revision":1,
        "parameters":{"inputs":["value.txt", ".HARNESS/private.json"]}}}, "revision")
    with pytest.raises(HarnessError) as caught:
        invoke(api, revised)
    assert caught.value.code == "WORKSPACE_CONTROL_PATH"
    assert invoke(api,next_request(request,started,"resume"))["interpretation"]["ref"]["revision"]==1


def test_existing_project_exclusions_are_not_overwritten(project):
    api, work, request = project
    control=work/".harness"
    control.mkdir()
    original=b"existing-user-rule\n"
    (control/".gitignore").write_bytes(original)
    with pytest.raises(HarnessError) as caught:
        invoke(api,request)
    assert caught.value.code=="PROJECT_IGNORE_CONFLICT"
    assert (control/".gitignore").read_bytes()==original
    assert not (control/"tasks").exists()


def test_project_task_database_swap_is_rejected(project):
    api, work, request = project
    first=invoke(api,request)
    second_request={**scoped_start(work,"second"),"project_workspace":str(work)}
    invoke(api,second_request)
    paths=[work/".harness/tasks"/r["task_id"]/"runs.sqlite3" for r in (request,second_request)]
    for path in paths:
        with sqlite3.connect(path) as connection:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    swap=work/".harness/swap.sqlite3"
    paths[0].rename(swap)
    paths[1].rename(paths[0])
    swap.rename(paths[1])
    with pytest.raises(HarnessError) as caught:
        invoke(Harness(api.store.root),next_request(request,first,"resume"))
    assert caught.value.code=="TASK_STORE_IDENTITY"


@pytest.mark.skipif(os.name!="posix",reason="POSIX worker capacity locks")
def test_project_workers_share_one_operator_capacity_pool(project, tmp_path):
    api, work, request = project
    a=work/".harness/tasks"/request["task_id"]
    b=tmp_path/"other/.harness/tasks"/("task_"+"b"*32)
    descriptors=[worker._worker_slot(a,api.store.root),worker._worker_slot(b,api.store.root)]
    try:
        with pytest.raises(OSError,match="capacity"):
            worker._worker_slot(b,api.store.root)
    finally:
        for descriptor in descriptors:
            os.close(descriptor)


def test_control_data_is_ignored_by_git_and_discovery(project):
    from change_lens import analyze
    api, work, request = project
    started=invoke(api,request)
    (work/"source.py").write_text("value=1\n",encoding="utf-8")
    (work/".harness/private.py").write_text("value=2\n",encoding="utf-8")
    assert list(analyze(work).by_path)==["source.py"]
    git=subprocess.run(["git","init","-q",str(work)],capture_output=True,timeout=10)
    assert git.returncode==0
    ignored=subprocess.run(["git","-C",str(work),"check-ignore",".harness/tasks/"+request["task_id"]+"/runs.sqlite3"],capture_output=True,timeout=10)
    assert ignored.returncode==0
    assert invoke(api,next_request(request,started,"resume"))["run"]["run_id"]==started["run_id"]


def test_foreign_project_is_rejected_before_local_store_creation(project):
    api, work, request=project
    with pytest.raises(HarnessError) as caught:
        invoke(api,request,allowed_workspace=lambda path:False)
    assert caught.value.code=="WORKSPACE_NOT_ALLOWED"
    assert not (work/".harness").exists()


def test_concurrent_project_starts_share_control_setup_without_duplicate_stores(project):
    api, work, request=project
    requests=[{**scoped_start(work,"parallel-"+str(i)),"project_workspace":str(work)} for i in range(3)]
    with ThreadPoolExecutor(max_workers=3) as executor:
        results=list(executor.map(lambda value:invoke(api,value),requests))
    assert len({item["run_id"] for item in results})==3
    assert (work/".harness/.gitignore").read_bytes()==b"*\n"
    assert (work/".harness/.ignore").read_bytes()==b"*\n"
    assert len(list((work/".harness/tasks").glob("*/runs.sqlite3")))==3
    assert not (api.store.root/"tasks").exists()
