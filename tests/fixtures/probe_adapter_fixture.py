"""Test-only adapter: no pytest, Domain, Worker or process-launch dependencies."""
import copy
import hashlib
from pathlib import Path

from harness.common import canonical_bytes
from harness_external.errors import fields, require

RUNNER = '''import json, sys, time
from pathlib import Path
settings = json.loads(Path('/opt/harness-runtime/capture.json').read_text())
report = Path(sys.argv[1])
sequence = 0
def emit(event, **data):
    global sequence
    sequence += 1
    with report.open('a') as stream:
        stream.write(json.dumps(dict(token=settings['token'], seq=sequence, event=event, **data)) + '\\n')
emit('collection')
ok = json.loads(Path('probe_data.json').read_text())['ok'] is True
case = dict(case_id=settings['case_id'], discovered=True, selected=True, started=True,
            executed=True, finished=True, outcome='passed' if ok else 'failed', phases=[])
emit('case', case=case)
time.sleep(float(sys.argv[2]))
emit('end')
'''


class ProbeObserver:
    def __init__(self, token, case_id):
        self.token, self.case_id, self.seq = token, case_id, 0
        self.collected = self.complete = False
        self.case = None

    def feed(self, event):
        require(event['token'] == self.token and event['seq'] == self.seq + 1 and not self.complete,
                'CASE_REPORT_PROTOCOL', 'Invalid probe session')
        self.seq += 1
        if event['event'] == 'collection':
            require(not self.collected, 'CASE_REPORT_PROTOCOL', 'Repeated collection')
            self.collected = True
            self.case = dict(case_id=self.case_id, discovered=True, selected=True, started=False,
                             executed=False, finished=False, outcome='not_run', phases=[])
        elif event['event'] == 'case':
            require(self.collected and not self.case['finished'] and event['case']['case_id'] == self.case_id,
                    'CASE_REPORT_PROTOCOL', 'Case binding mismatch')
            self.case = copy.deepcopy(event['case'])
            return copy.deepcopy(self.case)
        elif event['event'] == 'end':
            require(self.collected and self.case['finished'], 'CASE_REPORT_PROTOCOL', 'Incomplete probe')
            self.complete = True
        else:
            require(False, 'CASE_REPORT_PROTOCOL', 'Unknown probe event')

    def finish(self, error=None):
        return {'framework': 'fixture-probe', 'collection_complete': self.collected,
                'session_complete': self.complete and error is None, 'protocol_error': error,
                'cases': [copy.deepcopy(self.case)] if self.case else []}


class ProbeAdapter:
    adapter_id = 'fixture-probe-v1'
    revision = '1'
    report_path = '.probe-cases.jsonl'

    def __init__(self, case_id='opaque case / 1', timeout_boost=0):
        self.case_id, self.timeout_boost = case_id, timeout_boost

    def runtime(self, state_root, pinned=None):
        identity = {'runner_hash': hashlib.sha256(RUNNER.encode()).hexdigest(), 'case_id': self.case_id}
        require(pinned is None or pinned == identity, 'ADAPTER_RUNTIME_CHANGED', 'Probe runtime differs')
        return identity

    def command(self, check):
        selection = fields(check['adapter'], {'id', 'delay', 'rules'}, {'id', 'rules'})
        require(selection['id'] == self.adapter_id, 'ADAPTER_COMMAND', 'Wrong probe selection')
        return {'kind': 'command', 'argv': ['python3', '-I', '/opt/harness-runtime/probe.py', self.report_path, str(selection.get('delay', 0))],
                'cwd': '.', 'expectedExitCode': 0, 'timeout_seconds': check.get('timeout_seconds', 30) + self.timeout_boost}

    def stage(self, state_root, runtime, destination, token):
        self.runtime(state_root, runtime)
        (destination / 'probe.py').write_text(RUNNER)
        (destination / 'capture.json').write_bytes(canonical_bytes({'token': token, 'case_id': self.case_id}))

    def observer(self, token, runtime):
        return ProbeObserver(token, runtime['case_id'])
