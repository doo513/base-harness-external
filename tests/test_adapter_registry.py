import ast
import copy
import json
import os
from pathlib import Path
import shutil
import threading
import time

import pytest

from harness.common import canonical_hash
from harness_external.adapter_execution import bind, command, execute
from harness_external.adapter_registry import AdapterRegistration, AdapterRegistry, builtin_adapter_registry, validate_binding
from harness_external.errors import HarnessError
from harness_external.identity import verifier_identity
from harness_external.registry import DomainRegistry
from harness_external.service import Harness

FIXTURES = Path(__file__).parent / 'fixtures'
PROBE_ID = 'fixture-probe-v1'


def probe_registry(**config):
    return AdapterRegistry([AdapterRegistration(PROBE_ID, 'probe_adapter_fixture:ProbeAdapter', config, str(FIXTURES))])


def probe_check(delay=0):
    return {'kind': 'command', 'expectedExitCode': 0, 'timeout_seconds': 10,
            'adapter': {'id': PROBE_ID, 'delay': delay,
                        'rules': {'required_case_ids': [], 'allowed_outcomes': ['passed'], 'minimum_selected': 1, 'require_complete_session': True}}}


def bind_probe(registry, root, previous=None):
    return bind([{'check_id': 'domain.probe', 'spec': {'parameters': probe_check()}}], root, previous, registry)


def test_registration_roundtrip_and_binding_identity(tmp_path):
    registry = probe_registry()
    bindings = bind_probe(registry, tmp_path)
    manifest = registry.export(bindings)
    restored = AdapterRegistry.restore(json.loads(json.dumps(manifest)))
    restored.validate_bindings(bindings)
    assert command(probe_check(), registry) == command(probe_check(), restored)
    assert bind_probe(restored, tmp_path, bindings) == bindings
    assert manifest[PROBE_ID]['registration']['factory'] == 'probe_adapter_fixture:ProbeAdapter'


def test_duplicate_and_unknown_registrations_fail_without_loading_check_factories(tmp_path):
    entry = AdapterRegistration(PROBE_ID, 'probe_adapter_fixture:ProbeAdapter', import_root=str(FIXTURES))
    with pytest.raises(HarnessError, match='Duplicate'):
        AdapterRegistry([entry, entry])
    check = probe_check()
    check['adapter'].update(id='caller-factory', factory='probe_adapter_fixture:ProbeAdapter', import_root=str(FIXTURES))
    with pytest.raises(HarnessError) as error:
        command(check, builtin_adapter_registry())
    assert error.value.code == 'ADAPTER_UNREGISTERED'


def test_mutated_registration_input_does_not_change_registry():
    config = {'case_id': 'one'}
    registry = AdapterRegistry([AdapterRegistration(PROBE_ID, 'probe_adapter_fixture:ProbeAdapter', config, str(FIXTURES))])
    initial = registry.identity(PROBE_ID)
    config['case_id'] = 'changed'
    assert registry.identity(PROBE_ID) == initial


def test_omitted_and_explicit_constructor_defaults_have_same_identity():
    assert probe_registry().identity(PROBE_ID) == probe_registry(case_id='opaque case / 1', timeout_boost=0).identity(PROBE_ID)


def test_same_id_isolated_configuration_and_changed_binding_rejected(tmp_path):
    one, two = probe_registry(case_id='one'), probe_registry(case_id='two')
    first = bind_probe(one, tmp_path)
    second = bind_probe(two, tmp_path)
    assert first['domain.probe']['runtime']['case_id'] == 'one'
    assert second['domain.probe']['runtime']['case_id'] == 'two'
    with pytest.raises(HarnessError) as error:
        bind_probe(two, tmp_path, first)
    assert error.value.code == 'ADAPTER_IMPLEMENTATION_CHANGED'


def test_runtime_change_and_binding_corruption_fail_closed(tmp_path):
    registry = probe_registry()
    binding = bind_probe(registry, tmp_path)['domain.probe']
    changed = copy.deepcopy(binding)
    changed['runtime']['runner_hash'] = 'changed'
    with pytest.raises(HarnessError):
        validate_binding(changed)
    changed['binding_hash'] = canonical_hash({k: v for k, v in changed.items() if k != 'binding_hash'})
    with pytest.raises(HarnessError) as error:
        bind_probe(registry, tmp_path, {'domain.probe': changed})
    assert error.value.code == 'ADAPTER_RUNTIME_CHANGED'


def test_source_change_rejected_before_worker_factory_import(tmp_path):
    source = tmp_path / 'changed_probe_module.py'
    shutil.copyfile(FIXTURES / 'probe_adapter_fixture.py', source)
    registry = AdapterRegistry([AdapterRegistration(PROBE_ID, 'changed_probe_module:ProbeAdapter', import_root=str(tmp_path))])
    binding = bind_probe(registry, tmp_path)
    manifest = registry.export(binding)
    source.write_text(source.read_text() + '\n# changed implementation\n')
    with pytest.raises(HarnessError) as error:
        AdapterRegistry.restore(manifest).validate_bindings(binding)
    assert error.value.code == 'ADAPTER_IMPLEMENTATION_CHANGED'


def test_unused_adapter_is_not_part_of_core_execution_identity(monkeypatch):
    original = Path.read_bytes
    core_before = verifier_identity()
    registry = builtin_adapter_registry()
    adapter_before = registry.identity('pytest-cases-v1')
    def changed(path):
        value = original(path)
        return value + b'\n# plugin change\n' if path.name == 'pytest_adapter.py' else value
    monkeypatch.setattr(Path, 'read_bytes', changed)
    assert verifier_identity() == core_before
    assert registry.identity('pytest-cases-v1') != adapter_before


def test_adapter_cannot_raise_timeout_or_supply_a_different_binding(tmp_path):
    with pytest.raises(HarnessError) as error:
        command(probe_check(), probe_registry(timeout_boost=1))
    assert error.value.code == 'ADAPTER_COMMAND'
    registry = probe_registry()
    binding = bind_probe(registry, tmp_path)['domain.probe']
    check = probe_check()
    check['adapter']['id'] = 'another'
    with pytest.raises(HarnessError) as error:
        execute(check, tmp_path, tmp_path, threading.Event(), 10, binding, lambda *args: None, registry)
    assert error.value.code == 'ADAPTER_SELECTION_CHANGED'


def test_setup_deadline_prevents_command_launch(tmp_path, monkeypatch):
    import harness_external.adapter_execution as execution
    registry = probe_registry()
    binding = bind_probe(registry, tmp_path)['domain.probe']
    times = iter([0, 10])
    def forbidden(*args, **kwargs):
        pytest.fail('No child may start after the setup deadline')
    monkeypatch.setattr(execution.time, 'monotonic', lambda: next(times))
    monkeypatch.setattr(execution.shutil, 'which', lambda *args: '/unused/bun')
    monkeypatch.setattr(execution.subprocess, 'Popen', forbidden)
    with pytest.raises(HarnessError) as error:
        execute(probe_check(), tmp_path, tmp_path, threading.Event(), 1, binding, lambda *args: None, registry)
    assert error.value.code == 'VERIFICATION_CANCELLED'


def test_normalized_case_cannot_claim_success_without_execution():
    from harness_external.adapter_execution import _case
    body = {'case_id': 'opaque', 'outcome': 'passed', 'discovered': True, 'selected': True,
            'started': True, 'executed': False, 'finished': True, 'phases': []}
    with pytest.raises(HarnessError) as error:
        _case(body)
    assert error.value.code == 'CASE_REPORT_PROTOCOL'


def test_transport_and_worker_have_no_concrete_adapter_imports():
    root = Path(__file__).resolve().parents[1] / 'src/harness_external'
    for name in ('adapter_execution.py', 'worker.py', 'adapter_ports.py'):
        source = (root / name).read_text()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom):
                assert (node.module or '').split('.')[-1] != 'pytest_adapter'
        assert 'pytest-cases-v1' not in source and 'versions["pytest"]' not in source


class ProbeDomain:
    domain_id = 'adapter-probe'
    revision = '1'

    def prepare(self, goal, parameters, verifier, **kwargs):
        check = dict(probe_check(parameters.get('delay', 0)), check_id='domain.probe', check_key='probe')
        contract = {'original_goal': goal, 'domain_id': self.domain_id, 'domain_revision': self.revision,
                    'inputs': ['probe_data.json'], 'artifacts': ['probe_data.json'], 'profile': 'execution', 'checks': [check],
                    'verifier': verifier, 'rules': {'all_checks_required': True, 'snapshot_required': True}, 'limitations': []}
        contract['contract_hash'] = canonical_hash(contract)
        return {'contract': contract, 'questions': [], 'status': 'proceed', 'available_operations': ['submit', 'verify', 'finish_completed']}

    def normalize_check(self, parameters, contract):
        return copy.deepcopy(parameters)


def test_adapter_implementation_is_pinned_before_first_verification(tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    (workspace / 'probe_data.json').write_text('{"ok":true}')
    adapters = probe_registry(case_id='original')
    api = Harness(tmp_path / 'state', domains=DomainRegistry([ProbeDomain()]), adapters=adapters)
    run_id = api.start(domain_id='adapter-probe', goal='Pinned execution', workspace=str(workspace), parameters={}, request_id='start')['run_id']
    api.submit(run_id, 'submit')
    adapters.resolve(PROBE_ID).case_id = 'changed after admission'
    with pytest.raises(HarnessError) as error:
        api.verify(run_id, 'verify')
    assert error.value.code == 'ADAPTER_IMPLEMENTATION_CHANGED'
    assert api.status(run_id)['run']['verification_attempts'] == 0


def start_probe(tmp_path, *, ok=True, case_id='opaque case / 1', delay=0):
    workspace = tmp_path / 'workspace'
    workspace.mkdir(parents=True)
    (workspace / 'probe_data.json').write_text(json.dumps({'ok': ok}))
    api = Harness(tmp_path / 'state', domains=DomainRegistry([ProbeDomain()]), adapters=probe_registry(case_id=case_id))
    run_id = api.start(domain_id='adapter-probe', goal='Observe input fact', workspace=str(workspace), parameters={'delay': delay}, request_id='start')['run_id']
    api.submit(run_id, 'submit')
    job_id = api.verify(run_id, 'verify')['job_id']
    return api, run_id, job_id


def wait_job(api, run_id, job_id):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        job = api.status(run_id, job_id)['job']
        if job['status'] not in {'queued', 'running'}:
            return job
        time.sleep(.1)
    api.cancel(run_id, job_id, 'timeout-cleanup')
    pytest.fail('Detached probe worker did not finish')


@pytest.mark.skipif(os.environ.get('BASE_HARNESS_EXTERNAL_LIVE') != '1', reason='registered non-pytest adapter in a detached real Sandbox worker')
@pytest.mark.parametrize('ok', [True, False])
def test_live_external_adapter_detached_worker_and_restart(tmp_path, ok):
    api, run_id, job_id = start_probe(tmp_path, ok=ok)
    restored = Harness(api.store.root, domains=DomainRegistry([ProbeDomain()]), adapters=probe_registry())
    job = wait_job(restored, run_id, job_id)
    assert job['status'] == 'completed', job
    assert job['result']['status'] == ('passed' if ok else 'failed'), job
    case = restored.records(run_id, 'cases', job_id=job_id)['items'][0]
    assert case['case']['case_id'] == 'opaque case / 1'
    assert case['adapter_identity']['implementation']['id'] == PROBE_ID
    assert job['pid'] != os.getpid()
    if ok:
        wrong_config = Harness(api.store.root, domains=DomainRegistry([ProbeDomain()]), adapters=probe_registry(case_id='changed'))
        with pytest.raises(HarnessError) as error:
            wrong_config.finish(run_id, 'wrong-configuration', outcome='completed')
        assert error.value.code == 'ADAPTER_IMPLEMENTATION_CHANGED'
        record = restored.finish(run_id, 'finish', outcome='completed')['record']
        assert record['ready'] is False
        assert record['adapter_bindings']['domain.probe'] == case['adapter_identity']
    else:
        with pytest.raises(HarnessError):
            restored.finish(run_id, 'finish', outcome='completed')


@pytest.mark.skipif(os.environ.get('BASE_HARNESS_EXTERNAL_LIVE') != '1', reason='concurrent detached adapter bindings')
def test_live_two_sessions_do_not_replace_each_others_adapter(tmp_path):
    a, first, a_job = start_probe(tmp_path / 'one', case_id='first')
    b, second, b_job = start_probe(tmp_path / 'two', case_id='second')
    assert wait_job(a, first, a_job)['result']['status'] == 'passed'
    assert wait_job(b, second, b_job)['result']['status'] == 'passed'
    assert a.records(first, 'cases', job_id=a_job)['items'][0]['case']['case_id'] == 'first'
    assert b.records(second, 'cases', job_id=b_job)['items'][0]['case']['case_id'] == 'second'


@pytest.mark.skipif(os.environ.get('BASE_HARNESS_EXTERNAL_LIVE') != '1', reason='registered adapter cancellation and checkpoint ownership')
def test_live_cancelled_plugin_keeps_facts_without_success(tmp_path):
    api, run_id, job_id = start_probe(tmp_path, delay=10)
    deadline = time.monotonic() + 30
    while not api.records(run_id, 'cases', job_id=job_id)['items']:
        assert time.monotonic() < deadline
        time.sleep(.1)
    api.cancel(run_id, job_id, 'cancel')
    deadline = time.monotonic() + 15
    while api.status(run_id, job_id)['job']['cleanup'] != 'complete':
        assert time.monotonic() < deadline
        time.sleep(.1)
    assert api.records(run_id, 'cases', job_id=job_id)['items'][0]['case']['outcome'] == 'passed'
    assert api.resume(run_id)['gates']['status'] == 'unsatisfied'
    with pytest.raises(HarnessError):
        api.finish(run_id, 'finish', outcome='completed')
