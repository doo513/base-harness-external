"""Configured runtimes must not silently follow PATH or another Python release."""
import copy
from pathlib import Path
import sys
import json
import subprocess

import pytest

from test_skill_workflow import helper, ROOT


def configured(tmp_path):
    return helper.configure(tmp_path / "client.json", str(ROOT), str(tmp_path / "state"),
                            ephemeral=True, python_executable=sys.executable)["config"]


def test_configure_pins_selected_interpreter_and_identity(tmp_path):
    config = configured(tmp_path)
    runtime = config["python_runtime"]
    assert runtime["schema_version"] == "caller-python-runtime-v1"
    assert Path(runtime["executable"]).is_absolute()
    assert runtime["identity"]["version"] == list(sys.version_info[:3])
    assert runtime["identity"]["implementation"] == sys.implementation.name
    assert len(runtime["identity"]["executable_sha256"]) == 64
    assert helper.validate_config(config) == config


def test_missing_pinned_executable_fails_without_core_dispatch(tmp_path, monkeypatch):
    config = configured(tmp_path)
    config["python_runtime"]["executable"] = str(tmp_path / "missing-python")
    client = helper.Client(config)
    with pytest.raises(helper.ClientError) as error:
        client.execute("invoke", {"request": {"operation": "describe", "arguments": {"operation": "start"}}})
    assert error.value.code == "PYTHON_RUNTIME_UNAVAILABLE"
    assert not (tmp_path / "state").exists()


def test_mismatched_identity_is_not_a_domain_change_or_silent_fallback(tmp_path):
    config = configured(tmp_path)
    config["python_runtime"]["identity"]["version"] = [0, 0, 0]
    client = helper.Client(config)
    with pytest.raises(helper.ClientError) as error:
        client.execute("invoke", {"request": {"operation": "describe", "arguments": {"operation": "start"}}})
    assert error.value.code == "PYTHON_RUNTIME_CHANGED"
    assert not (tmp_path / "state").exists()


def test_legacy_local_config_does_not_fall_back_to_path_python(tmp_path):
    repo = tmp_path / "checkout"
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts/harness-tool").write_text("exit 99\n", encoding="utf-8")
    config = {"schema_version": 1, "harness_repo": str(repo), "state_dir": str(tmp_path / "state"), "state_lifetime": "ephemeral"}
    client = helper.Client(config)
    with pytest.raises(helper.ClientError) as error:
        client.execute("invoke", {"request": {"operation": "describe", "arguments": {"operation": "start"}}})
    assert error.value.code == "PYTHON_RUNTIME_NOT_CONFIGURED"


def test_remote_configuration_does_not_require_host_python_or_checkout(tmp_path):
    config = {"schema_version": 1, "harness_repo": "/not-on-host", "state_dir": str(tmp_path / "state"),
              "state_lifetime": "ephemeral", "server_socket": str(tmp_path / "socket")}
    assert helper.validate_config(config) == config


def test_core_runtime_error_is_not_misreported_as_no_core_invocation(tmp_path, monkeypatch):
    client = helper.Client(configured(tmp_path))
    response = {"api_version": "external-harness-v2", "ok": False,
                "error": {"code": "PYTHON_RUNTIME_CHANGED", "message": "Different Run runtime"},
                "checkpoint_progress": {"stage": "verify", "caller_record": {"observation_id": "stored"}}}
    def core_error(argv, **options):
        options["stdout"].write(json.dumps(response).encode("utf-8"))
        return subprocess.CompletedProcess(argv, 2)
    monkeypatch.setattr(helper.subprocess, "run", core_error)
    result = client.execute("invoke", {"request": {"operation": "describe", "arguments": {"operation": "start"}}})
    assert result == response


def test_domain_source_identity_survives_an_interpreter_identity_change(monkeypatch):
    from harness_external.domain import DevelopModule
    from harness_external.registry import DomainRegistry
    from harness_external import runtime_identity
    registry = DomainRegistry([DevelopModule()])
    before = registry.identity("develop")
    changed = {**before["runtime"]["python"], "version": [99, 0, 0]}
    monkeypatch.setattr(runtime_identity, "python_identity", lambda: changed)
    after = registry.identity("develop")
    assert before["schema_version"] == "domain-identity-v3"
    assert before["implementation_hash"] == after["implementation_hash"]
    assert before["configuration_hash"] == after["configuration_hash"]
    assert before["runtime"] != after["runtime"]


class ExampleDomain:
    domain_id = "example"
    revision = "1"

    def prepare(self, *args, **kwargs):
        return 10

    def normalize_check(self, *args, **kwargs):
        return {}


def test_changed_loaded_code_still_changes_identity_without_source_edit(monkeypatch):
    from harness_external.registry import DomainRegistry
    registry = DomainRegistry([ExampleDomain()])
    before = registry.identity("example")
    old = ExampleDomain.prepare.__code__
    monkeypatch.setattr(ExampleDomain.prepare, "__code__", old.replace(co_consts=tuple(11 if v == 10 else v for v in old.co_consts)))
    after = registry.identity("example")
    assert before["implementation_hash"] == after["implementation_hash"]
    assert before["configuration_hash"] == after["configuration_hash"]
    assert before["runtime"]["loaded_code_hash"] != after["runtime"]["loaded_code_hash"]


def test_runtime_mismatch_has_its_own_error_before_domain_comparison(tmp_path, monkeypatch):
    from harness_external.errors import HarnessError
    from harness_external.service import Harness
    from harness_external import runtime_identity
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "value.txt").write_text("ready", encoding="utf-8")
    api = Harness(tmp_path / "state")
    run_id = api.start(domain_id="develop", goal="Preserve ready", workspace=str(workspace), parameters={
        "profile": "structural", "inputs": ["value.txt"], "artifacts": ["value.txt"],
        "expectations": [{"path": "value.txt", "operator": "equals", "expected": "ready"}]}, request_id="begin")["run_id"]
    api.submit(run_id, "submit")
    current = runtime_identity.python_identity()
    monkeypatch.setattr(runtime_identity, "python_identity", lambda: {**current, "version": [99, 0, 0]})
    assert api.resume(run_id)["run"]["run_id"] == run_id
    with pytest.raises(HarnessError) as error:
        api.verify(run_id, "verify")
    assert error.value.code == "PYTHON_RUNTIME_CHANGED"
