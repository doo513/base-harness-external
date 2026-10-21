"""Public caller handoff checks, runnable without pytest inside the Sandbox.

These establish transport and authority boundaries, not prose quality or model
effectiveness. Guidance effectiveness needs separate development trials.
"""
import importlib.util
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from harness_external.errors import HarnessError
from harness_external.local_transport import HarnessServer
from harness_external.service import Harness
from harness_external.worker import execute_job


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "guidance_handoff_client", ROOT / "skills/harness-workflow/scripts/harness_client.py")
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class GuidanceHandoff(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="guidance-handoff-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        (self.workspace / "result.txt").write_text("pending", encoding="utf-8")
        self.api = Harness(self.root / "state")
        self.runtime = self.root / "runtime"
        self.runtime.mkdir(mode=0o700)

    def client(self, socket):
        return helper.Client({"schema_version": 1, "harness_repo": str(ROOT),
                              "state_dir": str(self.api.store.root), "state_lifetime": "ephemeral",
                              "server_socket": str(socket)}, timeout=10)

    def start(self, key, goal):
        return self.api.start(domain_id="develop", goal=goal, workspace=str(self.workspace),
                              mode="strict", request_id=key,
                              parameters={"profile": "structural", "inputs": ["result.txt"],
                                          "artifacts": ["result.txt"], "expectations": [
                                              {"id": "result", "path": "result.txt", "operator": "equals", "expected": "ready"}]},
                              analysis_selection={"perspectives": ["design"]})

    def test_socket_discovery_filters_without_selecting_or_mutating_a_run(self):
        first = self.start("first", "Original task")["run_id"]
        second = self.start("second", "Another task")["run_id"]
        usage = {run: self.api.resume(run)["usage"] for run in (first, second)}
        with HarnessServer(self.api, self.runtime / "h.sock", [self.workspace]) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                client = self.client(server.socket_path)
                with patch.object(helper.subprocess, "run", side_effect=AssertionError("No local Core fallback")):
                    found = client.find(str(self.workspace), "develop", "Original task")
                    self.assertEqual([item["run_id"] for item in found["matches"]], [first])
                    self.assertIsNone(found["selected_run_id"])
                    self.assertFalse(found["truncated"])
                    self.assertEqual(client.find(str(self.workspace), "other")["matches"], [])
                    self.assertEqual(client.find(str(self.workspace.parent), "develop")["matches"], [])
                    self.assertEqual(len(client.find(str(self.workspace), "develop")["matches"]), 2)
            finally:
                server.shutdown()
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())
        self.assertEqual(self.api.list_runs()["total"], 2)
        self.assertEqual({run: self.api.resume(run)["usage"] for run in (first, second)}, usage)

    def test_socket_discovery_pages_guarded_reads_and_keeps_truncation(self):
        client = self.client(self.runtime / "unused.sock")
        row = {"workspace": str(self.workspace), "domain_id": "develop", "goal": "task", "run_id": "one"}
        pages = [{"ok": True, "items": [dict(row, workspace=str(self.workspace.parent))], "next_offset": 100},
                 {"ok": True, "items": [row], "next_offset": None}]
        with patch.object(client, "_socket_execute", side_effect=pages) as send:
            found = client.find(str(self.workspace), "develop", "task", max_pages=2)
        self.assertEqual(found["matches"], [row])
        self.assertIsNone(found["selected_run_id"])
        self.assertFalse(found["truncated"])
        self.assertEqual([call.args[0] for call in send.call_args_list], [
            {"operation": "list-runs", "arguments": {"offset": offset, "limit": 100}} for offset in (0, 100)])
        with patch.object(client, "_socket_execute", return_value=pages[0]):
            limited = client.find(str(self.workspace), "develop", max_pages=1)
        self.assertTrue(limited["truncated"])
        self.assertEqual(limited["next_offset"], 100)
        failure = {"ok": False, "error": {"code": "STATE_CORRUPT"}}
        with patch.object(client, "_socket_execute", return_value=failure):
            self.assertEqual(client.find(str(self.workspace), "develop"), failure)

    def test_selected_advice_is_not_a_question_gate_or_verifier_fact(self):
        started = self.start("advice", "Choose a suitable design and produce a result")
        run = started["run_id"]
        advice = started["domain_preparation"]["analysis_guidance"]
        self.assertEqual(advice["selection"]["perspectives"], ["design"])
        self.assertFalse(advice["persistence_required"])
        before = self.api.resume(run)
        self.assertEqual(before["domain_questions"], [])
        self.api.observe(run, {"note": "Claim: the chosen design already satisfies the goal."}, "claim")
        after = self.api.resume(run)
        self.assertEqual(after["measurement"], before["measurement"])
        self.assertEqual(after["policy"], before["policy"])
        with self.assertRaises(HarnessError):
            self.api.finish(run, "claim-is-not-proof", outcome="completed")
        (self.workspace / "result.txt").write_text("ready", encoding="utf-8")
        self.api.submit(run, "submit")
        with patch("harness_external.worker.spawn_worker", return_value=None):
            job = self.api.verify(run, "verify")["job_id"]
        execute_job(self.api.store.root, job)
        self.assertEqual(self.api.records(run, "needs")["total"], 0)
        self.assertEqual(self.api.resume(run)["measurement"]["status"], "passed")
        self.assertFalse(self.api.finish(run, "finish", outcome="completed")["record"]["ready"])


if __name__ == "__main__":
    unittest.main()
