"""Independent input mistakes are returned together without publishing state."""
import json
import subprocess
import sys

import pytest

from harness_external.domain import DevelopModule
from harness_external.errors import HarnessError, fields
from harness_external.service import Harness


def broken_parameters():
    return {
        "inputs": ["app.py"], "artifacts": ["app.py"],
        "expectations": [
            {"path": "app.py", "operator": "unknown", "expected": "x"},
            {"path": "app.py", "operator": "contains", "expected": ""},
        ],
        "execution_checks": [
            {"kind": "command", "argv": []},
            {"kind": "command", "argv": ["python3"], "timeout_seconds": 0},
        ],
    }


def test_independent_check_errors_have_stable_paths_and_do_not_publish_run(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    api = Harness(tmp_path / "state")
    with pytest.raises(HarnessError) as caught:
        api.start(domain_id="develop", goal="Validate inputs", workspace=str(workspace),
                  parameters=broken_parameters(), request_id="bad")
    diagnostics = caught.value.diagnostics
    assert [item["path"] for item in diagnostics] == [
        "/expectations/0", "/expectations/1", "/execution_checks/0", "/execution_checks/1",
    ]
    assert all(item["code"] and item["message"] for item in diagnostics)
    assert api.list_runs()["items"] == []


def test_missing_and_unexpected_fields_are_reported_without_values():
    with pytest.raises(HarnessError) as caught:
        fields({"extra": "secret-not-to-echo"}, {"left", "right"}, {"left", "right"})
    value = caught.value.as_dict()
    assert {(item["path"], item["code"]) for item in value["diagnostics"]} == {
        ("/left", "FIELD_REQUIRED"), ("/right", "FIELD_REQUIRED"), ("/extra", "FIELD_UNEXPECTED"),
    }
    assert "secret-not-to-echo" not in json.dumps(value)
    assert value["code"] == "INVALID_PARAMETERS"


def test_diagnostic_count_is_bounded_and_truncation_explicit():
    with pytest.raises(HarnessError) as caught:
        fields({"extra" + str(i): i for i in range(300)}, set())
    value = caught.value.as_dict()
    assert 1 <= len(value["diagnostics"]) <= 64
    assert value["diagnostic_count"] == 300
    assert value["diagnostics_truncated"] is True


def test_multiple_parameter_types_are_diagnosed_before_dependent_compilation():
    with pytest.raises(HarnessError) as caught:
        DevelopModule().prepare("goal", {"inputs": 1, "artifacts": {}, "expectations": "invalid"}, {})
    assert {item["path"] for item in caught.value.diagnostics} == {"/inputs", "/artifacts", "/expectations"}


def test_empty_required_collections_are_reported_in_one_pass():
    with pytest.raises(HarnessError) as caught:
        DevelopModule().prepare("goal", {"inputs": [], "artifacts": [], "expectations": [], "execution_checks": []}, {})
    assert {item["path"] for item in caught.value.diagnostics} == {"/inputs", "/artifacts", "/expectations", "/execution_checks"}


def test_cli_preserves_structured_diagnostics(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    command = subprocess.run([
        sys.executable, "-m", "harness_external", "--state-dir", str(tmp_path / "state"),
        "start", "--workspace", str(workspace), "--goal", "Validate inputs", "--request-id", "invalid",
        "--parameters", "json:" + json.dumps(broken_parameters()),
    ], text=True, encoding="utf-8", capture_output=True, timeout=20)
    assert command.returncode == 2, command.stderr
    reply = json.loads(command.stdout)
    assert reply["ok"] is False
    assert len(reply["error"]["diagnostics"]) == 4
    assert reply["error"]["diagnostics_truncated"] is False
