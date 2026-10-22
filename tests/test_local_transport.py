"""A confined Host calls the existing Core outside its inherited restrictions."""
import os
from pathlib import Path
import threading
import json
import platform
import subprocess
import sys

import pytest

from harness_external.local_transport import HarnessServer, request
from harness_external.service import Harness


@pytest.fixture
def server(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "result.txt").write_text("ready", encoding="utf-8")
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    api = Harness(tmp_path / "state")
    with HarnessServer(api, runtime / "harness.sock", [workspace]) as service:
        thread = threading.Thread(target=service.serve_forever, daemon=True)
        thread.start()
        try:
            yield service, api, workspace
        finally:
            service.shutdown()
            thread.join(timeout=5)
            assert not thread.is_alive()


def call(service, operation, arguments, request_id=None, context=None, **options):
    envelope = {"operation": operation, "arguments": arguments}
    if request_id is not None:
        envelope["request_id"] = request_id
    if context is not None:
        envelope["context"] = context
    return request(service.socket_path, service.configuration, envelope, timeout=10, **options)


def start(service, workspace, request_id="start"):
    return call(service, "start", {"domain_id": "develop", "goal": "Read a file", "workspace": str(workspace),
                "parameters": {"profile": "structural", "inputs": ["result.txt"], "artifacts": ["result.txt"],
                               "expectations": [{"path": "result.txt", "operator": "equals", "expected": "ready"}]}}, request_id)


def test_transport_reuses_core_request_replay_and_bindings(server):
    service, api, workspace = server
    first = start(service, workspace)
    assert first["ok"], first
    assert start(service, workspace)["run_id"] == first["run_id"]
    assert api.list_runs()["total"] == 1
    context = {"workspace": str(workspace), "domain": "develop"}
    result = call(service, "checkpoint", {"run_id": first["run_id"], "wait_seconds": 10}, "checkpoint", context)
    assert result["ok"] and result["job"]["result"]["status"] == "passed", result
    assert result["transport"]["configuration"] == service.configuration
    wrong = call(service, "observe", {"run_id": first["run_id"], "data": {"note": "wrong context"}}, "note",
                 {"workspace": str(workspace), "domain": "other"})
    assert wrong["error"]["code"] == "RUN_CONTEXT_MISMATCH"


def test_transport_rejects_foreign_workspace_and_store_configuration(server, tmp_path):
    service, api, _ = server
    foreign = tmp_path / "outside"
    foreign.mkdir()
    denied = start(service, foreign)
    assert denied["error"]["code"] == "WORKSPACE_NOT_ALLOWED"
    reply = request(service.socket_path, {**service.configuration, "state_dir": str(tmp_path / "wrong")},
                    {"operation": "doctor", "arguments": {}}, timeout=5)
    assert reply["error"]["code"] == "TRANSPORT_CONFIGURATION_MISMATCH"
    assert api.list_runs()["total"] == 0


def test_transport_has_no_arbitrary_shell_or_model_operation(server):
    service, _, _ = server
    result = call(service, "shell", {"command": "print a fake pass"})
    assert result["error"]["code"] == "OPERATION_UNSUPPORTED"


def test_transport_filters_run_totals_before_pagination(server, tmp_path):
    service, api, workspace = server
    visible = start(service, workspace)
    assert visible["ok"]
    foreign = tmp_path / "outside"
    foreign.mkdir()
    (foreign / "result.txt").write_text("ready", encoding="utf-8")
    api.start(domain_id="develop", goal="foreign", workspace=str(foreign), mode="exploratory", request_id="foreign",
              parameters={"profile": "structural", "inputs": ["result.txt"], "artifacts": ["result.txt"],
                          "expectations": [{"path": "result.txt", "operator": "equals", "expected": "ready"}]})
    listed = call(service, "list-runs", {"offset": 0, "limit": 20})
    assert listed["ok"] and listed["total"] == 1
    assert [item["run_id"] for item in listed["items"]] == [visible["run_id"]]


def test_transport_bounds_long_poll_time_and_capacity(server):
    service, _, workspace = server
    run = start(service, workspace)["run_id"]
    arguments = {"run_id": run, "job_id": "job_" + "a" * 32, "wait_seconds": 31}
    too_long = call(service, "wait", arguments)
    assert too_long["error"]["code"] == "WAIT_INVALID"
    service._long_slots = threading.BoundedSemaphore(0)
    busy = call(service, "wait", {**arguments, "wait_seconds": 1})
    assert busy["error"]["code"] == "SERVER_BUSY"


def test_server_requires_restart_after_runtime_code_changes(server, monkeypatch):
    from harness_external import local_transport
    from harness_external.identity import deployment_identity
    service, api, workspace = server
    monkeypatch.setattr(local_transport, "deployment_identity", lambda: {**deployment_identity(), "source_hash": "changed"}, raising=False)
    result = start(service, workspace)
    assert result["error"]["code"] == "SERVER_RUNTIME_CHANGED"
    assert api.list_runs()["total"] == 0


def test_existing_socket_or_file_is_not_replaced(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    endpoint = runtime / "endpoint"
    endpoint.write_text("owned by someone else", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(ValueError):
        HarnessServer(Harness(tmp_path / "state"), endpoint, [workspace])
    assert endpoint.read_text(encoding="utf-8") == "owned by someone else"


def test_shutdown_removes_only_its_socket(server):
    service, _, _ = server
    assert service.socket_path.exists()
    assert service.socket_path.stat().st_mode & 0o777 == 0o600


@pytest.mark.skipif(os.environ.get("BASE_HARNESS_EXTERNAL_LIVE") != "1", reason="strict Sandbox in a separately launched worker")
def test_live_probe_and_verification_use_server_execution_context(server):
    service, _, workspace = server
    preflight = call(service, "doctor", {"sandbox": True})
    assert preflight["healthy"] and preflight["command_execution_verified"], preflight
    (workspace / "app.py").write_text("print(42)\n", encoding="utf-8")
    started = call(service, "start", {"domain_id": "develop", "goal": "Print 42", "workspace": str(workspace),
                   "parameters": {"inputs": ["app.py"], "artifacts": ["app.py"],
                                  "expectations": [{"path": "app.py", "operator": "contains", "expected": "print"}],
                                  "execution_checks": [{"kind": "cases", "id": "output", "adapter_id": "process-observations-v1",
                                      "selector": {"source_paths": ["app.py"], "argv": ["python3", "app.py"],
                                                   "cases": [{"id": "answer", "stream": "stdout", "operator": "equals", "expected": "42\n"}]}}]}}, "live-start")
    assert started["ok"], started
    result = call(service, "checkpoint", {"run_id": started["run_id"], "wait_seconds": 20}, "live-check",
                  {"workspace": str(workspace), "domain": "develop"})
    assert result["ok"] and result["job"]["result"]["status"] == "passed", result


@pytest.mark.skipif(os.environ.get("BASE_HARNESS_EXTERNAL_LIVE") != "1" or platform.system() != "Linux" or platform.machine() != "x86_64",
                    reason="Linux x86_64 seccomp-controlled Host and actual strict Sandbox")
def test_live_confined_caller_cannot_unshare_but_can_use_external_verifier(server):
    service, _, workspace = server
    helper = Path(__file__).resolve().parents[1] / "skills/harness-workflow/scripts/harness_client.py"
    config = {"schema_version": 1, "harness_repo": "/checkout-not-visible-to-host", "state_dir": service.configuration["state_dir"],
              "state_lifetime": "ephemeral", "server_socket": str(service.socket_path)}
    code = '''import ctypes, importlib.util, json, subprocess, sys
class Filter(ctypes.Structure):
    _fields_ = [('code',ctypes.c_ushort),('jt',ctypes.c_ubyte),('jf',ctypes.c_ubyte),('k',ctypes.c_uint)]
class Program(ctypes.Structure):
    _fields_ = [('length',ctypes.c_ushort),('filter',ctypes.POINTER(Filter))]
rules = (Filter*4)(Filter(0x20,0,0,0),Filter(0x15,0,1,272),Filter(0x06,0,0,0x00050026),Filter(0x06,0,0,0x7fff0000))
program = Program(4,rules)
libc = ctypes.CDLL(None,use_errno=True)
assert libc.prctl(38,1,0,0,0) == 0
assert libc.prctl(22,2,ctypes.byref(program),0,0) == 0
direct = subprocess.run(['unshare','--user','--map-root-user','/bin/true'],capture_output=True,text=True,encoding='utf-8',timeout=10)
assert direct.returncode != 0 and 'Function not implemented' in direct.stderr, direct.stderr
spec = importlib.util.spec_from_file_location('portable_client',sys.argv[1])
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
client = helper.Client(json.loads(sys.argv[2]), timeout=30)
preflight = client.call({'operation':'doctor','arguments':{'sandbox':True}})
assert preflight['healthy'] and preflight['command_execution_verified'], preflight
workspace = sys.argv[3]
started = client.call({'operation':'start','request_id':'confined-start','arguments':{
 'domain':'develop','goal':'Read in the real Sandbox','workspace':workspace,'mode':'strict','provenance':{'declared_author':'application'},
 'parameters':{'inputs':['result.txt'],'artifacts':['result.txt'],
  'expectations':[{'path':'result.txt','operator':'equals','expected':'ready'}],
  'execution_checks':[{'kind':'command','argv':['python3','-c',"from pathlib import Path; assert Path('result.txt').read_text() == 'ready'"]}]}}})
assert started['ok'], started
verified = client.call({'operation':'checkpoint','request_id':'confined-check','context':{'workspace':workspace,'domain':'develop'},
                       'arguments':{'run_id':started['run_id'],'wait_seconds':20}})
assert verified['ok'] and verified['job']['result']['status'] == 'passed', verified
print(json.dumps({'host_unshare':'blocked','server_probe':'passed','verification':'passed'}))
'''
    completed = subprocess.run([sys.executable, "-I", "-c", code, str(helper), json.dumps(config), str(workspace)],
                               capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert json.loads(completed.stdout) == {"host_unshare": "blocked", "server_probe": "passed", "verification": "passed"}
