"""Actual CLI/socket use with portable bindings; never calls an LLM."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from harness_external.local_transport import HarnessServer
from harness_external.service import Harness

spec = importlib.util.spec_from_file_location("task_client", ROOT / "skills/harness-workflow/scripts/harness_client.py")
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class TaskCallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.work = self.root / "작업 space"
        self.work.mkdir()
        (self.work / "app.py").write_text("print('ready')\n", encoding="utf-8")
        self.config = helper.configure(self.root / "client.json", str(ROOT), str(self.root / "state"), ephemeral=True)["config"]

    def args(self):
        return {"domain": "develop", "goal": "Keep the program", "workspace": str(self.work),
                "mode": "exploratory", "provenance": {"declared_author": "model"},
                "parameters": {"profile": "structural", "inputs": ["app.py"], "artifacts": ["app.py"],
                               "expectations": [{"path": "app.py", "operator": "contains", "expected": "ready"}]}}

    def start(self, client, name="start", arguments=None):
        path = self.root / (name + "-binding.json")
        result = client.call_operation("start", arguments or self.args(), action_id=name, binding_output=path)
        self.assertTrue(result["ok"], result)
        return result, helper.read_json(str(path)), path

    def test_cli_new_task_and_replay_survive_client_replacement(self):
        client = helper.Client(self.config)
        first, binding, path = self.start(client)
        self.assertEqual(binding["schema_version"], "harness-caller-binding-v2")
        self.assertEqual(binding["task_id"], first["task_id"])
        self.assertFalse((self.root / "state/runs.sqlite3").exists())
        fresh = helper.Client(self.config)
        replay = fresh.call(helper.read_json(first["caller_request"]["request_file"]))
        self.assertEqual(replay["run_id"], first["run_id"])
        self.assertEqual(fresh.validate_binding(binding)["run"]["run_id"], first["run_id"])
        second, _, _ = self.start(fresh, "another")
        self.assertNotEqual(first["task_id"], second["task_id"])
        metadata = fresh.call_operation("list-tasks", {"workspace": str(self.work)})
        self.assertEqual(metadata["total"], 2)
        selected = fresh.find(str(self.work), "develop", task_id=first["task_id"])
        self.assertEqual(len(selected["matches"]), 1)

    def test_start_response_loss_preserves_exact_task_identity(self):
        client = helper.Client(self.config)
        execute = client.execute
        def lose(*args, **kwargs):
            execute(*args, **kwargs)
            raise helper.ClientError("CALL_OUTCOME_UNKNOWN", "Controlled response loss")
        target = self.root / "request.json"
        binding_path = self.root / "binding.json"
        with patch.object(client, "execute", side_effect=lose):
            with self.assertRaises(helper.ClientError):
                client.call_operation("start", self.args(), action_id="start", output=target, binding_output=binding_path)
        fresh = helper.Client(self.config)
        envelope = helper.read_json(str(target))
        response = fresh.call(envelope)
        fresh.bind_started(response, binding_path)
        page = fresh.call_operation("list-runs", {}, task_id=envelope["task_id"])
        self.assertEqual(page["total"], 1)

    def test_finish_knowledge_and_selected_reads_work_over_actual_cli(self):
        client = helper.Client(self.config)
        _, binding, path = self.start(client)
        selected = {"title": "Replay safely", "when": "Retrying the same work", "lesson": "Keep the saved request.",
                    "limitations": ["No behavior test in this example."], "keywords": ["retry"]}
        ended = client.call_operation("finish", {"outcome": "partial"}, binding=binding, binding_path=path,
                                      action_id="finish", knowledge=selected)
        self.assertTrue(ended["ok"], ended)
        note = ended["knowledge_publication"]
        self.assertEqual(note["status"], "published")
        found = client.call_operation("knowledge-find", {"query": "retry"})
        self.assertEqual(found["total"], 1)
        read = client.call_operation("knowledge-read", {"entry_id": note["entry_id"]})
        self.assertIn("No measurements cited", read["markdown"])
        self.assertEqual(read["entry"]["outcome"], "partial")
        self.assertFalse(read["ready"])

    def server_client(self):
        directory = self.root / "socket"
        directory.mkdir(mode=0o700)
        server = HarnessServer(Harness(self.root / "state"), directory / "s.sock", [self.work])
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def close():
            server.shutdown()
            thread.join(timeout=10)
            server.server_close()
        self.addCleanup(close)
        return helper.Client({**self.config, "server_socket": str(server.socket_path)})

    @unittest.skipUnless(os.name == "posix", "Unix socket transport")
    def test_socket_uses_task_store_and_filters_shared_document_sources(self):
        client = self.server_client()
        first, binding, path = self.start(client)
        self.assertEqual(client.validate_binding(binding)["task_id"], first["task_id"])
        note = {"title": "Allowed note", "when": "Reading this project", "lesson": "Preserve existing files.", "limitations": []}
        result = client.call_operation("finish", {"outcome": "abandoned"}, binding=binding, binding_path=path,
                                       action_id="finish", knowledge=note)
        self.assertEqual(result["knowledge_publication"]["status"], "published")
        foreign = self.root / "foreign"
        foreign.mkdir()
        (foreign / "app.py").write_text("print('ready')\n", encoding="utf-8")
        local = helper.Client(self.config)
        args = {**self.args(), "workspace": str(foreign)}
        _, other, other_path = self.start(local, "outside", args)
        external = local.call_operation("finish", {"outcome": "partial"}, binding=other, binding_path=other_path,
                                        action_id="end", knowledge={**note, "title": "Outside note"})
        self.assertEqual(client.call_operation("knowledge-find", {})["total"], 1)
        denied = client.call_operation("knowledge-read", {"entry_id": external["knowledge_publication"]["entry_id"]})
        self.assertFalse(denied["ok"])
        self.assertEqual(denied["error"]["code"], "WORKSPACE_NOT_ALLOWED")
        self.assertEqual(client.call_operation("list-tasks", {})["total"], 1)

    @unittest.skipUnless(os.environ.get("BASE_HARNESS_EXTERNAL_LIVE") == "1" and os.name == "posix", "real strict Sandbox over socket")
    def test_live_socket_verification_and_publication_bind_actual_measurement(self):
        client = self.server_client()
        args = self.args()
        args["required_check"] = ["domain.process"]
        args["parameters"].update(profile="execution", execution_checks=[
            {"id": "process", "kind": "command", "argv": ["python3", "app.py"], "timeout_seconds": 10}])
        _, binding, path = self.start(client, arguments=args)
        verified = client.checkpoint(binding, "measure", wait_seconds=30)
        self.assertTrue(verified["ok"], verified)
        while verified.get("wait_status") == "pending":
            verified = client.wait(binding, verified["job"]["job_id"], wait_seconds=30)
        self.assertEqual(verified["job"]["result"]["status"], "passed")
        refs = client.call_operation("records", {"kind": "measurements", "job_id": verified["job"]["job_id"]},
                                      binding=binding)["items"]
        data = {"title": "Python process observed", "when": "Running this submitted program",
                "lesson": "The configured command completed.", "limitations": ["Command exit does not prove complete behavior."],
                "cited_observation_ids": [item["observation_id"] for item in refs]}
        result = client.call_operation("finish", {"outcome": "completed"}, binding=binding, binding_path=path,
                                       action_id="finish", knowledge=data)
        self.assertEqual(result["record"]["gates"]["status"], "passed")
        self.assertEqual(result["knowledge_publication"]["status"], "published")
        note = client.call_operation("knowledge-read", {"entry_id": result["knowledge_publication"]["entry_id"]})
        self.assertIn(refs[0]["observation_id"], note["markdown"])
        self.assertFalse(note["gate_eligible"])


if __name__ == "__main__":
    unittest.main()
