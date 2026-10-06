"""A scripted Host develops a feature through the real CLI/worker/Sandbox.

This validates integration, not autonomous model reasoning or a performance A/B.
"""
import hashlib
import os

import pytest

from test_skill_workflow import caller, request


@pytest.mark.skipif(os.environ.get("BASE_HARNESS_EXTERNAL_LIVE") != "1", reason="real CLI, detached worker and strict Sandbox")
def test_need_discovery_decision_implementation_and_measured_repair(caller):
    client, workspace, _ = caller
    schema = "Existing nested server.host wins collisions. Preserve unknown fields. Reject scalar server when migrating host. Never mutate the input.\n"
    (workspace / "schema.txt").write_text(schema, encoding="utf-8")
    (workspace / "unrelated.txt").write_text("This is not needed to resolve the collision policy.", encoding="utf-8")
    broken = "def migrate(config):\n    return {'server': {'host': config.get('host', 'localhost')}}\n"
    (workspace / "migrator.py").write_text(broken, encoding="utf-8")
    tests = '''import copy
import unittest
from migrator import migrate

class Migration(unittest.TestCase):
    def test_new_host(self):
        self.assertEqual(migrate({'host': 'new'}), {'server': {'host': 'new'}})
    def test_collision_and_unknown_fields(self):
        value = {'host': 'legacy', 'server': {'host': 'existing'}, 'unknown': {'v': [1]}}
        original = copy.deepcopy(value)
        self.assertEqual(migrate(value), {'server': {'host': 'existing'}, 'unknown': {'v': [1]}})
        self.assertEqual(value, original)
    def test_scalar_server_is_not_discarded(self):
        with self.assertRaises(ValueError):
            migrate({'host': 'new', 'server': 'preserve-me'})

if __name__ == '__main__':
    unittest.main(verbosity=2)
'''
    (workspace / "check_migration.py").write_text(tests, encoding="utf-8")
    fixed = '''from copy import deepcopy

def migrate(config):
    result = deepcopy(config)
    if 'host' in result:
        legacy = result.pop('host')
        server = result.get('server', {})
        if not isinstance(server, dict):
            raise ValueError('Cannot merge a host into scalar server')
        server.setdefault('host', legacy)
        result['server'] = server
    return result
'''
    prepared = client.call({"operation": "start", "request_id": "start", "arguments": {
        "domain": "develop", "workspace": str(workspace), "goal": "Implement a compatible host-to-server migration",
        "mode": "strict", "provenance": {"declared_author": "model"},
        "parameters": {"profile": "execution", "inputs": ["migrator.py", "check_migration.py", "schema.txt"],
                       "artifacts": ["migrator.py"],
                       "expectations": [{"id": "entry", "path": "migrator.py", "operator": "contains", "expected": "def migrate("}],
                       "execution_checks": [{"kind": "command", "id": "behavior", "argv": ["python3", "check_migration.py"], "timeout_seconds": 30}],
                       "requirements": [{"id": "R1", "statement": "Implement the migration while preserving the schema's compatibility rules",
                                         "check_ids": ["domain.behavior"], "minimum_evidence": "command"}]}}})
    assert prepared["ok"], prepared
    run = prepared["run_id"]
    assert "compatibility" in {item["id"] for item in prepared["domain_preparation"]["analysis_guidance"]["knowledge_map"]}
    need = {"id": "collision", "expected_revision": 0, "state": "open", "details": {
        "kind": "decision", "question": "Which field wins a migration collision?",
        "reason": "Avoid losing existing data", "resolution_criterion": "Find the applicable schema rule and a discriminating example",
        "requirement_ids": ["R1"], "knowledge_refs": ["compatibility"]}}
    created = client.call(request(workspace, run, "observe", "need", data={"kind": "need", "note": "Resolve the policy before choosing the implementation", "need": need}))
    assert created["ok"], created

    # The Host, not Domain/Core, chooses and opens the relevant sources.
    opened = []
    def inspect(path):
        opened.append(path)
        return (workspace / path).read_text(encoding="utf-8")
    assert inspect("schema.txt") == schema
    assert inspect("migrator.py") == broken
    assert opened == ["schema.txt", "migrator.py"]
    decision = client.call(request(workspace, run, "observe", "decision", data={"kind": "decision", "note": "Preserve nested values and reject incompatible scalar merges, as the supplied schema requires",
                                                                                 "references": ["schema.txt", "migrator.py:migrate"]}))
    assert decision["ok"], decision
    first = client.call(request(workspace, run, "checkpoint", "before", wait_seconds=30))
    assert first["ok"] and first["wait_status"] == "terminal", first
    assert first["job"]["result"]["status"] == "failed"
    failed = client.call(request(workspace, run, "records", kind="measurements", job_id=first["job"]["job_id"]))["items"]
    need.update(expected_revision=1, state="addressed", conclusion="The schema settles the policy; current measurements expose a data-loss counterexample",
                activity_ids=[decision["activity"]["ref"]["id"]], check_ids=["domain.behavior"])
    linked = client.call(request(workspace, run, "observe", "linked", data={"kind": "need", "note": "Connect the decision to a failing measurement", "need": need,
        "references": ["schema.txt", "migrator.py:migrate"], "observation_ids": [item["observation_id"] for item in failed]}))
    assert linked["ok"] and linked["need"]["trust"] == "untrusted", linked
    assert client.call(request(workspace, run, "resume"))["measurement"]["status"] == "failed"

    before_test_hash = hashlib.sha256((workspace / "check_migration.py").read_bytes()).hexdigest()
    (workspace / "migrator.py").write_text(fixed, encoding="utf-8")
    after = client.call(request(workspace, run, "checkpoint", "after", wait_seconds=30))
    assert after["ok"] and after["wait_status"] == "terminal", after
    assert after["job"]["result"]["status"] == "passed", after
    measured = client.call(request(workspace, run, "records", kind="measurements", job_id=after["job"]["job_id"]))["items"]
    command = next(item for item in measured if item["check_id"] == "domain.behavior")
    assert command["report"]["sandbox"]["containment"] == "user_mount_pid_net_namespace"
    assert command["report"]["sandbox"]["network"] == "loopback_only"
    assert first["submitted_candidate_hash"] != after["submitted_candidate_hash"]
    assert hashlib.sha256((workspace / "check_migration.py").read_bytes()).hexdigest() == before_test_hash
    assert after["job"]["result"]["requirement_observations"]["requirements"][0]["linked_checks_passed"]

    need.update(expected_revision=2, conclusion="The unchanged behavioral checks now pass for the new Candidate")
    addressed = client.call(request(workspace, run, "observe", "measured", data={"kind": "need", "note": "Retain measured scope, not a claim of exhaustive correctness", "need": need,
        "references": ["schema.txt", "check_migration.py"], "observation_ids": [command["observation_id"]]}))
    assert addressed["ok"], addressed
    recovered = client.call(request(workspace, run, "resume"))
    assert recovered["needs"]["context_changed_ids"] == []
    assert recovered["gates"]["status"] == "passed"
    assert client.call(request(workspace, run, "records", kind="needs"))["items"][0]["ref"]["revision"] == 3
    finished = client.call(request(workspace, run, "finish", "finish", outcome="completed"))
    assert finished["ok"] and finished["record"]["ready"] is False, finished
