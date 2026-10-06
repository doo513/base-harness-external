"""Framework-neutral preparation, schema ownership and portable UTF-8 I/O."""
import ast
import copy
import io
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from harness_external.adapter_registry import AdapterRegistration, AdapterRegistry, builtin_adapter_registry
from harness_external.check_preparation import AdapterCheckPreparation
from harness_external.domain import DevelopModule
from harness_external.errors import HarnessError
from harness_external.registry import DomainRegistry
from harness_external.service import Harness
from test_adapter_registry import FIXTURES, PROBE_ID, probe_registry, wait_job


def parameters():
    return {"inputs": ["app.py", "test_cases.py"], "artifacts": ["app.py"],
            "expectations": [{"id": "entry", "path": "app.py", "operator": "contains", "expected": "def value"}]}


def case_check():
    return {"kind": "cases", "id": "behavior", "adapter_id": "pytest-cases-v1",
            "selector": {"paths": ["test_cases.py"]}, "required_cases": ["test_cases.py::test_ok"]}


@pytest.mark.parametrize("check", [{"kind": "command", "argv": ["python3", "-c", "pass"]}, case_check()])
def test_clarification_names_an_answerable_framework_neutral_parameter(tmp_path, check):
    api = Harness(tmp_path / "state")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run = api.start(domain_id="develop", goal="개발 검증", workspace=str(workspace), parameters=parameters(),
                    mode="exploratory", request_id="start")["run_id"]
    before = api.resume(run)
    assert before["domain_preparation"]["status"] == "needs_input"
    questions = before["domain_questions"]
    assert [item["parameter"] for item in questions] == ["execution_checks"]
    assert questions[0]["id"] == "develop:execution_checks"
    api.revise(run, {"expected_revision": 1, "parameters": {"execution_checks": [check]}}, "answer")
    assert api.resume(run)["domain_preparation"]["status"] == "proceed"


def test_old_and_new_inputs_compile_identical_checks_without_changing_caller_data():
    bridge = AdapterCheckPreparation(builtin_adapter_registry())
    module = DevelopModule(check_preparation=bridge)
    old = {**parameters(), "test_commands": [{"id": "cli", "argv": ["python3", "app.py"]}],
           "pytest_checks": [{"id": "behavior", "paths": ["test_cases.py"], "required_cases": ["test_cases.py::test_ok"]}]}
    unchanged = copy.deepcopy(old)
    new = {**parameters(), "execution_checks": [{"kind": "command", "id": "cli", "argv": ["python3", "app.py"]}, case_check()]}
    assert module.prepare("goal", old, {}) == module.prepare("goal", new, {})
    assert old == unchanged
    with pytest.raises(HarnessError, match="not both"):
        bridge.parameters({**old, "execution_checks": [case_check()]})


@pytest.mark.parametrize(("patch", "code"), [
    ({"selector": {"paths": ["test_cases.py"], "args": ["--override-ini=bad"]}}, "PYTEST_ARGS"),
    ({"required_cases": ["opaque-not-a-node-id"]}, "PYTEST_CASE_ID"),
    ({"required_cases": ["outside.py::test_x"]}, "PYTEST_SCOPE"),
])
def test_selected_adapter_rejects_its_own_invalid_syntax(patch, code):
    module = DevelopModule(check_preparation=AdapterCheckPreparation(builtin_adapter_registry()))
    with pytest.raises(HarnessError) as error:
        module.prepare("goal", {**parameters(), "execution_checks": [{**case_check(), **patch}]}, {})
    assert error.value.code == code


def test_case_schema_is_explicit_and_cannot_expand_input_scope(monkeypatch):
    with pytest.raises(HarnessError) as error:
        DevelopModule().prepare("goal", {**parameters(), "execution_checks": [case_check()]}, {})
    assert error.value.code == "ADAPTER_SCHEMA_UNAVAILABLE"
    adapters = probe_registry()
    monkeypatch.setattr(adapters.resolve(PROBE_ID), "normalize_selection", lambda *args: {"selector": {}, "source_paths": ["outside.py"]})
    with pytest.raises(HarnessError) as error:
        AdapterCheckPreparation(adapters).selection(PROBE_ID, {}, ["probe_data.json"], [])
    assert error.value.code == "CHECK_SCOPE_CONFLICT"


def test_schema_callback_cannot_mutate_domain_owned_criteria(monkeypatch):
    adapters = probe_registry(case_id="opaque")
    adapter = adapters.resolve(PROBE_ID)
    original = adapter.normalize_selection
    def mutating(selector, inputs, required):
        result = original(selector, inputs, required)
        inputs.clear()
        required.clear()
        selector["injected"] = True
        return result
    monkeypatch.setattr(adapter, "normalize_selection", mutating)
    module = DevelopModule(check_preparation=AdapterCheckPreparation(adapters))
    supplied = {**parameters(), "inputs": ["app.py", "probe_data.json"],
                "execution_checks": [{"kind": "cases", "id": "behavior", "adapter_id": PROBE_ID,
                                      "selector": {}, "required_cases": ["opaque"]}]}
    before = copy.deepcopy(supplied)
    check = module.prepare("goal", supplied, {})["contract"]["checks"][-1]
    assert check["adapter"]["rules"]["required_case_ids"] == ["opaque"]
    assert check["adapter"]["rules"]["allowed_outcomes"] == ["passed"]
    assert supplied == before


def test_adapter_schema_and_alias_are_identity_pinned(monkeypatch):
    adapters = probe_registry()
    before = adapters.identity(PROBE_ID)
    adapter = adapters.resolve(PROBE_ID)
    monkeypatch.setattr(adapter, "validate_reference", lambda selector, reference: None)
    assert adapters.identity(PROBE_ID) != before
    entry = AdapterRegistration(PROBE_ID, "probe_adapter_fixture:ProbeAdapter", import_root=str(FIXTURES), parameter_alias="probe_checks")
    assert AdapterRegistry([entry]).identity(PROBE_ID) != before


def test_injected_schema_does_not_hide_inherited_domain_configuration(monkeypatch):
    class Specialized(DevelopModule):
        threshold = 1
    module = Specialized(check_preparation=AdapterCheckPreparation(probe_registry()))
    registry = DomainRegistry([module])
    before = registry.identity("develop")
    monkeypatch.setattr(Specialized, "threshold", 2)
    assert registry.identity("develop") != before
    module.threshold = 3
    before = registry.identity("develop")
    monkeypatch.setattr(Specialized, "threshold", 4)
    assert registry.identity("develop") == before


def test_preparation_port_code_and_configuration_are_not_hidden(monkeypatch):
    class ConfiguredBridge(AdapterCheckPreparation):
        variant = 1
    bridge = ConfiguredBridge(probe_registry())
    registry = DomainRegistry([DevelopModule(check_preparation=bridge)])
    before = registry.identity("develop")
    bridge.variant = 2
    assert registry.identity("develop") != before
    before = registry.identity("develop")
    monkeypatch.setattr(bridge, "reference", lambda adapter, reference: None)
    assert registry.identity("develop") != before


def probe_policy(tmp_path, *, ok=True):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "app.py").write_text("# 결과\n", encoding="utf-8")
    policy_root = tmp_path / "policies"
    bundle = policy_root / "probe/bundle"
    bundle.mkdir(parents=True)
    (bundle / "probe_data.json").write_text(json.dumps({"ok": ok, "label": "관측"}, ensure_ascii=False), encoding="utf-8")
    definition = {"schema_version": "acceptance-policy-v1", "policy_id": "probe", "revision": "1", "domain_id": "develop",
                  "parameters": {"inputs": ["app.py", "probe_data.json"], "artifacts": ["app.py"],
                      "expectations": [{"id": "entry", "path": "app.py", "operator": "contains", "expected": "결과"}],
                      "execution_checks": [{"kind": "cases", "id": "behavior", "adapter_id": PROBE_ID, "selector": {}}]},
                  "required_check_ids": ["domain.entry", "domain.behavior"],
                  "requirements": [{"id": "R1", "statement": "선언한 사례를 관측", "check_ids": ["domain.entry", "domain.behavior"], "minimum_evidence": "testcase"}],
                  "coverage": {"schema_version": "develop-coverage-v2", "profile_id": "probe", "profile_revision": "1", "known_gaps": [],
                      "scenarios": [{"id": "S1", "requirement_id": "R1", "check_id": "domain.behavior", "kind": "positive", "required": True,
                          "test_ref": {"path": "probe_data.json", "case_id": "사례 / 1"}}]},
                  "bundle": {"version": "1", "files": ["probe_data.json"]},
                  "approval": {"declared_by": "test operator", "reference": "controlled fixture, not authentication"}}
    (policy_root / "probe/policy.json").write_text(json.dumps(definition, ensure_ascii=False), encoding="utf-8")
    return workspace, policy_root


def test_same_develop_accepts_opaque_cases_without_loading_pytest(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "harness_external.pytest_adapter", None)
    workspace, root = probe_policy(tmp_path)
    api = Harness(tmp_path / "state", policy_root=root, adapters=probe_registry(case_id="사례 / 1"))
    run = api.start(domain_id="develop", goal="한글 관측", workspace=str(workspace), parameters={}, policy_id="probe", request_id="start")["run_id"]
    checks = api.records(run, "checks")["items"]
    adapter = next(c for c in checks if c["check_id"] == "domain.behavior")["spec"]["parameters"]["adapter"]
    assert adapter["id"] == PROBE_ID
    assert adapter["rules"]["required_case_ids"] == ["사례 / 1"]
    assert api.resume(run)["measurement"]["status"] == "not_run"


def test_selected_adapter_validates_coverage_reference_and_canonical_checks_stay_pinned(tmp_path):
    workspace, root = probe_policy(tmp_path)
    api = Harness(tmp_path / "state", policy_root=root, adapters=probe_registry(case_id="사례 / 1"))
    run = api.start(domain_id="develop", goal="goal", workspace=str(workspace), parameters={}, policy_id="probe", request_id="start")["run_id"]
    with pytest.raises(HarnessError) as error:
        api.revise(run, {"expected_revision": 1, "parameters": {"execution_checks": [
            {"kind": "cases", "id": "behavior", "adapter_id": PROBE_ID, "selector": {},
             "required_cases": ["사례 / 1"], "allowed_outcomes": ["passed", "skipped"]}]}}, "weaken")
    assert error.value.code == "ACCEPTANCE_CHECK_CHANGED"
    policy_path = root / "probe/policy.json"
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    policy["coverage"]["scenarios"][0]["test_ref"]["case_id"] = "wrong opaque id"
    policy_path.write_text(json.dumps(policy, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(HarnessError) as error:
        api.start(domain_id="develop", goal="goal", workspace=str(workspace), parameters={}, policy_id="probe", request_id="wrong-reference")
    assert error.value.code == "PROBE_REFERENCE"


@pytest.mark.skipif(os.environ.get("BASE_HARNESS_EXTERNAL_LIVE") != "1", reason="alternate adapter through real Sandbox and detached worker")
@pytest.mark.parametrize("ok", [True, False])
def test_live_same_develop_joins_other_adapter_observations_and_gates(tmp_path, monkeypatch, ok):
    monkeypatch.setitem(sys.modules, "harness_external.pytest_adapter", None)
    workspace, root = probe_policy(tmp_path, ok=ok)
    api = Harness(tmp_path / "state", policy_root=root, adapters=probe_registry(case_id="사례 / 1"))
    run = api.start(domain_id="develop", goal="한글 관측", workspace=str(workspace), parameters={}, policy_id="probe", request_id="start")["run_id"]
    api.submit(run, "submit")
    job_id = api.verify(run, "verify")["job_id"]
    job = wait_job(api, run, job_id)
    assert job["status"] == "completed", job
    assert job["result"]["status"] == ("passed" if ok else "failed"), job
    requirement = job["result"]["requirement_observations"]["requirements"][0]
    assert requirement["linked_cases_passed"] is ok
    observed = api.records(run, "cases", job_id=job_id)["items"][0]
    assert observed["case"]["case_id"] == "사례 / 1"
    if ok:
        assert api.finish(run, "finish", outcome="completed")["record"]["ready"] is False
    else:
        with pytest.raises(HarnessError) as error:
            api.finish(run, "finish", outcome="completed")
        assert error.value.code == "VERIFICATION_REQUIRED"


@pytest.mark.skipif(os.environ.get("BASE_HARNESS_EXTERNAL_LIVE") != "1", reason="Unicode pytest cases through real Sandbox and detached worker")
def test_live_unicode_pytest_case_preserves_exact_binding(tmp_path):
    from test_pytest_domain import policy_fixture
    case = "test_cases.py::test_한글"
    api, workspace, _ = policy_fixture(tmp_path, "from app import value\ndef test_한글(): assert value() == 1\n", required=case)
    run = api.start(domain_id="develop", goal="한글 테스트 연결", workspace=str(workspace), parameters={}, request_id="start")["run_id"]
    api.submit(run, "submit")
    job_id = api.verify(run, "verify")["job_id"]
    job = wait_job(api, run, job_id)
    assert job["result"]["status"] == "passed", job
    assert api.records(run, "cases", job_id=job_id)["items"][0]["case"]["case_id"] == case
    assert job["result"]["requirement_observations"]["requirements"][0]["linked_cases_passed"]
    assert api.finish(run, "finish", outcome="completed")["record"]["ready"] is False


def ascii_defaults(monkeypatch):
    original = io.open
    def open_ascii(file, mode="r", buffering=-1, encoding=None, *args, **kwargs):
        if "b" not in mode and encoding in (None, "locale"):
            encoding = "ascii"
        return original(file, mode, buffering, encoding, *args, **kwargs)
    monkeypatch.setattr(io, "open", open_ascii)


def test_manifest_both_prepare_paths_read_utf8_under_ascii_defaults(tmp_path, monkeypatch):
    from harness_external import domain
    manifest = json.loads(Path(domain.__file__).with_name("develop_manifest.json").read_text(encoding="utf-8"))
    manifest["description"] = "한글 개발 정책"
    (tmp_path / "develop_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(domain, "files", lambda package: tmp_path)
    ascii_defaults(monkeypatch)
    # Prove the simulated default actually rejects UTF-8 text; Path.read_text
    # passes the sentinel "locale", not None, on supported Python versions.
    with pytest.raises(UnicodeDecodeError):
        (tmp_path / "develop_manifest.json").read_text()
    module = DevelopModule()
    assert module.prepare("목표", {}, {}, exploratory=True)["status"] == "needs_input"
    assert module.prepare("목표", {**parameters(), "profile": "structural"}, {})["status"] == "proceed"


def test_runtime_selection_cache_reads_utf8_under_ascii_defaults(tmp_path, monkeypatch):
    from harness_external import pytest_adapter
    monkeypatch.setattr(pytest_adapter.metadata, "distribution", lambda name: SimpleNamespace(version="한글-1", requires=[], files=[], locate_file=lambda path: tmp_path))
    first = pytest_adapter.prepare_runtime(tmp_path)
    ascii_defaults(monkeypatch)
    assert pytest_adapter.prepare_runtime(tmp_path) == first


def test_runner_capture_reads_utf8_under_ascii_defaults(tmp_path, monkeypatch):
    from harness_external import pytest_runner
    capture = tmp_path / "capture.json"
    capture.write_text(json.dumps({"token": "한글 토큰"}, ensure_ascii=False), encoding="utf-8")
    report = tmp_path / "report.jsonl"
    class RunnerPath:
        def __new__(cls, path):
            return capture if path == "/opt/harness-runtime/capture.json" else Path(path)
        cwd = staticmethod(Path.cwd)
    monkeypatch.setattr(pytest_runner, "Path", RunnerPath)
    def fake_main(args, plugins):
        plugins[0].pytest_sessionstart(None)
        return 0
    monkeypatch.setitem(sys.modules, "pytest", SimpleNamespace(__version__="probe", main=fake_main, hookimpl=lambda **kw: lambda fn: fn))
    monkeypatch.setattr(sys, "argv", ["runner", str(tmp_path), str(report), '{"paths":[],"args":[]}'])
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    ascii_defaults(monkeypatch)
    with pytest.raises(SystemExit) as result:
        pytest_runner.main()
    assert result.value.code == 0
    assert json.loads(report.read_text(encoding="utf-8"))["token"] == "한글 토큰"


def test_managed_text_io_always_declares_utf8():
    root = Path(__file__).resolve().parents[1]
    for path in (root / "src").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in {"read_text", "write_text"}:
                assert any(k.arg == "encoding" and isinstance(k.value, ast.Constant) and k.value.value == "utf-8" for k in node.keywords), (path, node.lineno)
