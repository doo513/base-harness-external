"""Application routing isolates tasks without changing Core Run semantics."""
import copy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from harness.common import canonical_hash
from harness_external.dispatch import invoke
from harness_external.errors import HarnessError
from harness_external.service import Harness


def scoped_start(workspace, request_id="start"):
    arguments = {"domain": "develop", "goal": "Keep the content", "workspace": str(workspace),
                 "mode": "exploratory", "parameters": {
                     "profile": "structural", "inputs": ["value.txt"], "artifacts": ["value.txt"],
                     "expectations": [{"id": "value", "path": "value.txt", "operator": "equals", "expected": "ready"}]}}
    task_id = "task_" + canonical_hash({"request_id": request_id, "workspace": str(workspace), "domain": "develop"})[:32]
    return {"operation": "start", "arguments": arguments, "request_id": request_id, "task_id": task_id}


def bound(request, result, operation, arguments=None, request_id=None):
    return {"operation": operation, "arguments": {"run_id": result["run_id"], **(arguments or {})},
            "task_id": request["task_id"], "context": result["context"],
            **({"request_id": request_id} if request_id else {})}


class TaskStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = self.root / "work"
        self.work.mkdir()
        (self.work / "value.txt").write_text("ready", encoding="utf-8")
        self.api = Harness(self.root / "state")

    def test_separate_requests_in_same_workspace_have_separate_databases(self):
        a, b = scoped_start(self.work, "a"), scoped_start(self.work, "b")
        first, second = invoke(self.api, a), invoke(self.api, b)
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertEqual(first["task_id"], a["task_id"])
        for request in (a, b):
            self.assertTrue((self.api.store.root / "tasks" / request["task_id"] / "runs.sqlite3").is_file())
        self.assertFalse(self.api.store.database.exists())
        self.assertEqual(sorted(p.name for p in self.work.iterdir()), ["value.txt"])
        with self.assertRaises(HarnessError):
            invoke(self.api, bound(a, second, "resume"))

    def test_identical_replay_and_fresh_session_use_same_task_and_run(self):
        request = scoped_start(self.work)
        first = invoke(self.api, request)
        self.assertEqual(invoke(Harness(self.api.store.root), request), first)
        resumed = invoke(Harness(self.api.store.root), bound(request, first, "resume"))
        self.assertEqual(resumed["run"]["run_id"], first["run_id"])
        changed = copy.deepcopy(request)
        changed["arguments"]["goal"] = "A different intention"
        with self.assertRaises(HarnessError) as caught:
            invoke(self.api, changed)
        self.assertEqual(caught.exception.code, "REQUEST_CONFLICT")

    def test_missing_database_is_not_recreated_even_on_start_replay(self):
        request = scoped_start(self.work)
        result = invoke(self.api, request)
        database = self.api.store.root / "tasks" / request["task_id"] / "runs.sqlite3"
        database.rename(database.with_suffix(".saved"))
        for call in (request, bound(request, result, "resume")):
            with self.assertRaises(HarnessError) as caught:
                invoke(self.api, call)
            self.assertEqual(caught.exception.code, "TASK_STORE_MISSING")
        self.assertFalse(database.exists())

    def test_unknown_or_invalid_task_does_not_create_a_store(self):
        request = scoped_start(self.work)
        with self.assertRaises(HarnessError):
            invoke(self.api, {**request, "operation": "resume", "arguments": {"run_id": "run_" + "0" * 32},
                              "context": {"workspace": str(self.work), "domain": "develop"}})
        self.assertFalse(self.api.store.root.exists())
        with self.assertRaises(HarnessError):
            invoke(self.api, {**request, "task_id": "../outside"})
        self.assertFalse(self.api.store.root.exists())

    def test_truncated_database_is_not_silently_reinitialized(self):
        request = scoped_start(self.work)
        result = invoke(self.api, request)
        database = self.api.store.root / "tasks" / request["task_id"] / "runs.sqlite3"
        database.write_bytes(b"")
        for call in (request, bound(request, result, "resume")):
            with self.assertRaises(HarnessError) as caught:
                invoke(self.api, call)
            self.assertEqual(caught.exception.code, "TASK_STORE_CORRUPT")
        self.assertEqual(database.stat().st_size, 0)

    def test_existing_task_cannot_be_reused_for_an_unrelated_start(self):
        request = scoped_start(self.work)
        invoke(self.api, request)
        with self.assertRaises(HarnessError) as caught:
            invoke(self.api, {**request, "request_id": "different"})
        self.assertEqual(caught.exception.code, "TASK_START_CONFLICT")

    def test_interrupted_provision_does_not_publish_a_partial_store(self):
        request = scoped_start(self.work)
        with patch("harness_external.task_storage.os.rename", side_effect=OSError("interrupted publication")):
            with self.assertRaises(OSError):
                invoke(self.api, request)
        target = self.api.store.root / "tasks" / request["task_id"]
        self.assertFalse(target.exists())
        begun = invoke(self.api, request)
        self.assertEqual(begun["task_id"], request["task_id"])

    def test_explicit_runtime_continuation_keeps_task_and_never_inherits_results(self):
        request = scoped_start(self.work)
        begun = invoke(self.api, request)
        invoke(self.api, bound(request, begun, "finish", {"outcome": "partial"}, "close"))
        next_request = copy.deepcopy(request)
        next_request["request_id"] = "runtime-transition"
        next_request["arguments"].update(predecessor_run_id=begun["run_id"], continuation_reason="Explicit runtime transition")
        continued = invoke(self.api, next_request)
        self.assertEqual(continued["task_id"], request["task_id"])
        self.assertNotEqual(continued["run_id"], begun["run_id"])
        state = invoke(self.api, bound(request, continued, "resume"))
        self.assertEqual(state["measurement"]["status"], "not_run")

    def test_concurrent_start_creates_one_run(self):
        request = scoped_start(self.work)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: invoke(Harness(self.api.store.root), request), range(2)))
        self.assertEqual(results[0], results[1])
        listed = invoke(self.api, {"operation": "list-runs", "arguments": {}, "task_id": request["task_id"]})
        self.assertEqual(listed["total"], 1)

    def test_legacy_envelopes_still_use_original_store(self):
        request = scoped_start(self.work)
        request.pop("task_id")
        result = invoke(self.api, request)
        self.assertNotIn("task_id", result)
        self.assertTrue(self.api.store.database.exists())
        self.assertEqual(invoke(self.api, request), result)
        self.assertFalse((self.api.store.root / "tasks").exists())

    def test_task_discovery_is_metadata_only_and_paginated(self):
        a, b = scoped_start(self.work, "a"), scoped_start(self.work, "b")
        invoke(self.api, a)
        invoke(self.api, b)
        page = invoke(self.api, {"operation": "list-tasks", "arguments": {"workspace": str(self.work), "limit": 1}})
        self.assertEqual(len(page["items"]), 1)
        self.assertEqual(page["total"], 2)
        self.assertIsNotNone(page["next_offset"])
        self.assertNotIn("goal", page["items"][0])
        self.assertFalse(self.api.store.database.exists())


if __name__ == "__main__":
    unittest.main()
