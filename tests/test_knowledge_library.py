"""Cross-task knowledge is derived advice, never another verification ledger."""
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from harness_external.dispatch import invoke
from harness_external.errors import HarnessError
from harness_external.service import Harness
from harness_external.worker import execute_job
import test_task_storage as storage
from test_task_storage import scoped_start, bound


class KnowledgeTests(unittest.TestCase):
    setUp = storage.TaskStorageTests.setUp
    def close(self, label="a", *, outcome="completed"):
        request = scoped_start(self.work, label)
        started = invoke(self.api, request)
        invoke(self.api, bound(request, started, "submit", request_id="submit"))
        with patch("harness_external.worker.spawn_worker", return_value=None):
            queued = invoke(self.api, bound(request, started, "verify", request_id="verify"))
        directory = self.api.store.root / "tasks" / request["task_id"]
        execute_job(directory, queued["job_id"])
        facts = invoke(self.api, bound(request, started, "records", {"kind": "measurements"}))
        observation_id = facts["items"][0]["observation_id"]
        final = invoke(self.api, bound(request, started, "finish", {"outcome": outcome}, "finish"))
        return request, started, final, observation_id

    def data(self, observation):
        return {"title": "중복 요청 보존", "when": "재시도와 중복 요청을 처리할 때",
                "lesson": "Preserve the existing result before replacing it.",
                "limitations": ["Only the declared file predicate was measured."],
                "keywords": ["retry", "보존"], "cited_observation_ids": [observation]}

    def test_publish_find_read_and_replay_preserve_source_and_no_gate_authority(self):
        request, started, final, observation = self.close()
        command = bound(request, started, "knowledge-publish", {"data": self.data(observation)}, "publish")
        published = invoke(self.api, command)
        self.assertEqual(invoke(self.api, command), published)
        found = invoke(self.api, {"operation": "knowledge-find", "arguments": {"query": "retry"}})
        self.assertEqual(found["total"], 1)
        self.assertNotIn("markdown", found["items"][0])
        read = invoke(self.api, {"operation": "knowledge-read", "arguments": {"entry_id": published["entry_id"]}})
        self.assertIn(observation, read["markdown"])
        self.assertEqual(read["entry"]["record_hash"], final["record"]["record_hash"])
        self.assertFalse(read["gate_eligible"])
        self.assertEqual(invoke(self.api, bound(request, started, "resume"))["record"]["record_hash"], final["record"]["record_hash"])
        newer = scoped_start(self.work, "b")
        other = invoke(self.api, newer)
        with self.assertRaises(HarnessError) as caught:
            invoke(self.api, bound(newer, other, "assess", {"data": {"status": "satisfied", "summary": "Reuse",
                   "uncertainties": [], "interpretation_revision": 1, "cited_observation_ids": [observation]}}, "assess"))
        self.assertEqual(caught.exception.code, "OBSERVATION_REFERENCE")

    def test_finish_export_failure_does_not_undo_closeout_and_can_retry_publication(self):
        request = scoped_start(self.work)
        started = invoke(self.api, request)
        data = self.data("missing")
        data["cited_observation_ids"] = []
        finish = {**bound(request, started, "finish", {"outcome": "partial"}, "finish"), "knowledge": data}
        with patch("harness_external.knowledge.atomic_write", side_effect=OSError("disk failure")):
            result = invoke(self.api, finish)
        self.assertEqual(result["record"]["outcome"], "partial")
        self.assertEqual(result["knowledge_publication"]["status"], "failed")
        retried = invoke(self.api, bound(request, started, "knowledge-publish", {"data": data}, "publish-retry"))
        self.assertEqual(retried["status"], "published")
        self.assertEqual(invoke(self.api, bound(request, started, "resume"))["record"]["record_hash"], result["record"]["record_hash"])

    def test_open_run_or_foreign_observation_cannot_be_published_as_measured(self):
        request = scoped_start(self.work)
        started = invoke(self.api, request)
        with self.assertRaises(HarnessError) as caught:
            invoke(self.api, bound(request, started, "knowledge-publish", {"data": self.data("fake")}, "pub"))
        self.assertEqual(caught.exception.code, "KNOWLEDGE_RUN_OPEN")
        invoke(self.api, bound(request, started, "finish", {"outcome": "abandoned"}, "end"))
        with self.assertRaises(HarnessError) as caught:
            invoke(self.api, bound(request, started, "knowledge-publish", {"data": self.data("fake")}, "pub"))
        self.assertEqual(caught.exception.code, "OBSERVATION_REFERENCE")

    def test_corrupted_index_does_not_replace_documents_or_break_search(self):
        request, started, _, observation = self.close(outcome="partial")
        result = invoke(self.api, bound(request, started, "knowledge-publish", {"data": self.data(observation)}, "pub"))
        index = self.api.store.root / "knowledge" / "INDEX.md"
        index.write_text("stale index", encoding="utf-8")
        self.assertEqual(invoke(self.api, {"operation": "knowledge-find", "arguments": {}})["total"], 1)
        # Replaying publication repairs the derived index, not the original Run.
        replayed = invoke(self.api, bound(request, started, "knowledge-publish", {"data": self.data(observation)}, "pub"))
        self.assertEqual(replayed, result)
        self.assertIn(result["entry_id"], index.read_text(encoding="utf-8"))
        document = self.api.store.root / "knowledge" / "entries" / (result["entry_id"] + ".md")
        document.write_text("forged success", encoding="utf-8")
        with self.assertRaises(HarnessError) as caught:
            invoke(self.api, {"operation": "knowledge-read", "arguments": {"entry_id": result["entry_id"]}})
        self.assertEqual(caught.exception.code, "KNOWLEDGE_CORRUPT")

    def test_concurrent_duplicate_publication_and_changed_payload_conflict(self):
        request, started, _, observation = self.close()
        command = bound(request, started, "knowledge-publish", {"data": self.data(observation)}, "pub")
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: invoke(Harness(self.api.store.root), command), range(2)))
        self.assertEqual(results[0], results[1])
        changed = {**command, "arguments": {**command["arguments"], "data": {**self.data(observation), "lesson": "Different claim"}}}
        with self.assertRaises(HarnessError) as caught:
            invoke(self.api, changed)
        self.assertEqual(caught.exception.code, "REQUEST_CONFLICT")
        self.assertEqual(invoke(self.api, {"operation": "knowledge-find", "arguments": {}})["total"], 1)

    def test_failed_completion_does_not_create_a_knowledge_document(self):
        request = scoped_start(self.work)
        request["arguments"]["mode"] = "strict"
        started = invoke(self.api, request)
        finish = {**bound(request, started, "finish", {"outcome": "completed"}, "finish"), "knowledge": self.data("fake")}
        with self.assertRaises(HarnessError):
            invoke(self.api, finish)
        self.assertFalse((self.api.store.root / "knowledge").exists())


if __name__ == "__main__":
    unittest.main()
