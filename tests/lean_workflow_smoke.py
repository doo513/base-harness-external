"""Dependency-free integration checks runnable inside the real Harness Sandbox.

The outer observer measures this command's exit/report, not independent authority
for every assertion. Full pytest/CLI/socket regressions are recorded separately.
"""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from harness_external.service import Harness
from harness_external.dispatch import invoke
from harness_external.errors import HarnessError
from harness_external import worker, runtime_identity
from change_lens import analyze

spec = importlib.util.spec_from_file_location("lean_smoke_client", ROOT / "skills/harness-workflow/scripts/harness_client.py")
client_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client_module)


class LeanSmoke(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="lean-smoke-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.workspace = self.base / "project"
        self.workspace.mkdir()
        (self.workspace / "value.txt").write_text("unfinished", encoding="utf-8")
        self.api = Harness(self.base / "state")
        self.goal = "Keep the original request and measure ready bytes."
        self.run = self.api.start(domain_id="develop", goal=self.goal, workspace=str(self.workspace), request_id="begin",
            mode="exploratory", required_checks=["domain.value"], parameters={"profile": "structural", "inputs": ["value.txt"],
            "artifacts": ["value.txt"], "expectations": [{"id": "value", "path": "value.txt", "operator": "equals", "expected": "ready"}]})["run_id"]
        self.context = {"workspace": str(self.workspace), "domain": "develop"}

    def request(self, operation, request_id=None, **arguments):
        return {"operation": operation, "arguments": {"run_id": self.run, **arguments}, "context": self.context,
                **({"request_id": request_id} if request_id else {})}

    def checkpoint(self, key="measure", **arguments):
        with patch.object(worker, "spawn_worker", side_effect=lambda state, job: worker.execute_job(state, job)):
            return invoke(self.api, self.request("checkpoint", key, view="brief", wait_seconds=0, **arguments))

    def test_runtime_pin_and_portable_implementation_guard(self):
        config = client_module.configure(self.base / "client.json", str(ROOT), str(self.base / "other-state"),
                                          ephemeral=True, python_executable=sys.executable)["config"]
        self.assertEqual(config["python_runtime"]["identity"], runtime_identity.python_identity())
        before = self.api.domains.identity("develop")
        with patch.object(runtime_identity, "python_identity", return_value={**before["runtime"]["python"], "version": [99, 0, 0]}):
            after = self.api.domains.identity("develop")
        self.assertEqual(before["implementation_hash"], after["implementation_hash"])
        self.assertEqual(before["configuration_hash"], after["configuration_hash"])
        self.assertNotEqual(before["runtime"], after["runtime"])

    def test_extra_fields_are_claims_and_replay_is_exact(self):
        value = {"fields": {"raw": "원문\nline two", "unresolved": None},
                 "extensions": {"platform": {"ok": False, "count": 0, "items": []}, "ready": True}}
        first = self.checkpoint(payload=value)
        self.assertEqual(first["gates"]["status"], "unsatisfied")
        self.assertFalse(first["ready"])
        (self.workspace / "value.txt").write_text("ready", encoding="utf-8")
        replay = self.checkpoint(payload=value)
        self.assertEqual(replay["selected_job"]["job_id"], first["selected_job"]["job_id"])
        self.assertEqual(replay["selected_job"]["result_status"], "failed")
        notes = self.api.records(self.run, "notes")["items"]
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["payload"], {**value, "parents": []})
        self.assertEqual(json.dumps(notes[0]["payload"], sort_keys=True), json.dumps({**value, "parents": []}, sort_keys=True))
        self.assertEqual(notes[0]["trust"], "untrusted")

    def test_empty_context_and_compact_output_do_not_finish(self):
        (self.workspace / "value.txt").write_text("ready", encoding="utf-8")
        result = self.checkpoint()
        self.assertEqual(result["gates"]["status"], "passed")
        self.assertLess(len(json.dumps(result).encode("utf-8")), 8192)
        state = self.api.resume(self.run)
        self.assertEqual(state["history_counts"]["notes"], 0)
        self.assertEqual(state["history_counts"]["assessments"], 0)
        self.assertEqual(state["run"]["status"], "active")

    def test_reference_copy_and_cross_run_rejection(self):
        note = self.api.observe(self.run, {"note": "", "payload": {"extensions": {"decision": {"next": None}}}}, "note")
        reference = note["extension_refs"]["decision"]
        request = self.request("checkpoint", "linked", view="brief", wait_seconds=0,
                               payload={"fields": {"prior": {"next": None}}})
        request["input_bindings"] = [{"target": ["payload", "fields", "prior"], "source": reference}]
        with patch.object(worker, "spawn_worker", side_effect=lambda state, job: worker.execute_job(state, job)):
            result = invoke(self.api, request)
        self.assertEqual(result["caller_record"]["record_ref"]["run_id"], self.run)
        request["request_id"] = "foreign"
        request["input_bindings"][0]["source"] = {**reference, "run_id": "run_" + "0" * 32}
        with self.assertRaises(HarnessError):
            invoke(self.api, request)

    def test_changed_context_cannot_reuse_checkpoint_intent(self):
        self.checkpoint()
        with self.assertRaises(HarnessError) as caught:
            self.checkpoint(payload={"extensions": {"late": True}})
        self.assertEqual(caught.exception.code, "REQUEST_CONFLICT")
        self.assertEqual(self.api.resume(self.run)["history_counts"]["notes"], 0)

    def test_selected_knowledge_is_separate_from_task_records(self):
        (self.workspace / "value.txt").write_text("ready", encoding="utf-8")
        self.checkpoint()
        observation = self.api.records(self.run, "measurements")["items"][0]["observation_id"]
        request = self.request("finish", "finish", outcome="completed")
        request["knowledge"] = {"title": "Keep claims separate", "when": "Retaining an exception note", "lesson": "A caller note is not a measured pass.",
                                "limitations": ["One file fixture only."], "cited_observation_ids": [observation]}
        result = invoke(self.api, request)
        self.assertEqual(result["knowledge_publication"]["status"], "published")
        self.assertFalse(result["knowledge_publication"]["gate_eligible"])
        root = self.api.store.root / "knowledge"
        self.assertTrue((root / "INDEX.md").is_file())
        self.assertEqual(len(list((root / "entries").glob("*.md"))), 1)
        self.assertFalse((self.workspace / "knowledge").exists())

    def test_optional_explorer_cache_is_not_evidence(self):
        source = self.base / "source"
        source.mkdir()
        (source / "a.py").write_text("value=1\n", encoding="utf-8")
        (source / "b.py").write_text("import a\n", encoding="utf-8")
        first = analyze(source, cache_dir=self.base / "cache")
        cached = analyze(source, cache_dir=self.base / "cache")
        self.assertEqual(first.edges, cached.edges)
        self.assertEqual(cached.cache_info["reused_files"], 2)
        self.assertFalse(cached.cache_info["verification_eligible"])
        (source / "b.py").write_text("value=2\n", encoding="utf-8")
        changed = analyze(source, cache_dir=self.base / "cache")
        self.assertEqual(changed.edges, [])
        self.assertEqual(self.api.resume(self.run)["usage"]["verification_attempts"], 0)

    def test_real_local_client_checkpoint_preserves_scope(self):
        config = client_module.configure(self.base / "client.json", str(ROOT), str(self.api.store.root),
                                          ephemeral=True, python_executable=sys.executable)["config"]
        client = client_module.Client(config)
        binding_path = self.base / "binding.json"
        binding = client.bind(self.run, str(self.workspace), "develop", binding_path)["binding"]
        (self.workspace / "value.txt").write_text("ready", encoding="utf-8")
        result = client.checkpoint(binding, "actual-cli", view="brief", wait_seconds=15, binding_path=binding_path)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["gates"]["status"], "passed")
        self.assertTrue(Path(result["caller_request"]["request_file"]).is_file())


if __name__ == "__main__":
    outcome = unittest.TextTestRunner(stream=sys.stderr, verbosity=1).run(unittest.defaultTestLoader.loadTestsFromTestCase(LeanSmoke))
    print(json.dumps({"tests_run": outcome.testsRun, "failures": len(outcome.failures), "errors": len(outcome.errors), "skipped": len(outcome.skipped)}))
    raise SystemExit(0 if outcome.wasSuccessful() and outcome.testsRun >= 8 else 1)
