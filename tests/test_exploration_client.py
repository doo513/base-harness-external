"""Optional explorer works via the packaged caller without touching a Run DB."""
import json
import builtins
from pathlib import Path
import subprocess
import sys

from test_skill_workflow import helper, ROOT, HELPER


def test_portable_client_explores_without_core_or_state(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "a.py").write_text("value = 1\n", encoding="utf-8")
    (workspace / "b.py").write_text("import a\n", encoding="utf-8")
    config_path = tmp_path / "client.json"
    config = helper.configure(config_path, str(ROOT), str(tmp_path / "state"), ephemeral=True)["config"]
    client = helper.Client(config)
    monkeypatch.setattr(client, "execute", lambda *args: (_ for _ in ()).throw(AssertionError("No Core transport")))
    result = client.explore(["impact", str(workspace), "--changed", "a.py", "--format", "json"])
    assert result["ok"] and result["gate_eligible"] is False
    assert [x["path"] for x in result["data"]["affected"]] == ["a.py", "b.py"]
    assert not (tmp_path / "state").exists()
    call = subprocess.run([sys.executable, str(HELPER), "--config", str(config_path), "explore", "context", str(workspace),
                           "--changed", "a.py", "--cache-dir", str(tmp_path / "cache"), "--limit", "1", "--format", "json"],
                          capture_output=True, text=True, encoding="utf-8", timeout=20)
    value = json.loads(call.stdout)
    assert call.returncode == 0, value
    assert value["data"]["omitted_files"] == 1 and value["data"]["cache"]["verification_eligible"] is False
    assert not (tmp_path / "state").exists()


def test_describe_advertises_operation_specific_formats():
    assert "brief" in helper.describe("checkpoint")["response_formats"]
    assert helper.describe("revise")["response_formats"] == ["raw"]


def test_core_domain_and_worker_still_verify_with_explorer_unavailable(tmp_path, monkeypatch):
    original = builtins.__import__
    def isolated(name, *args, **kwargs):
        if name == "change_lens" or name.startswith("change_lens."):
            raise ImportError("Optional Host explorer deliberately unavailable")
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", isolated)
    from harness_external.service import Harness
    from harness_external import worker
    workspace = tmp_path / "work"
    workspace.mkdir()
    (workspace / "value.txt").write_text("ready", encoding="utf-8")
    api = Harness(tmp_path / "state")
    run = api.start(domain_id="develop", goal="Measure ready bytes", workspace=str(workspace), request_id="begin",
        parameters={"profile":"structural", "inputs":["value.txt"], "artifacts":["value.txt"],
                    "expectations":[{"path":"value.txt", "operator":"equals", "expected":"ready"}]})["run_id"]
    api.submit(run, "submit")
    monkeypatch.setattr(worker, "spawn_worker", lambda *args: None)
    job = api.verify(run, "verify")["job_id"]
    worker.execute_job(api.store.root, job)
    assert api.status(run, job)["job"]["result"]["status"] == "passed"
    assert api.finish(run, "finish", outcome="completed")["record"]["ready"] is False
