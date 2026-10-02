import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from leasequeue import Queue


class Acceptance(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "queue.sqlite"
        self.queue = Queue(self.path)

    def test_durable_success(self):
        self.assertTrue(self.queue.add("a", {"x": [1]}, request_id="a"))
        job = self.queue.claim("w", 0, 10, request_id="c")
        self.assertEqual(job["id"], "a")
        self.assertTrue(Queue(self.path).finish("a", job["token"], 1, True, {"done": True}, request_id="f"))
        self.assertEqual(Queue(self.path).get("a")["result"], {"done": True})

    def test_request_replay(self):
        self.queue.add("a", 1, request_id="a")
        self.assertTrue(Queue(self.path).add("a", 1, request_id="a"))
        with self.assertRaises(ValueError):
            self.queue.add("a", 2, request_id="a")
        self.assertEqual(len(self.queue.events()), 1)

    def test_fenced_expiry(self):
        self.queue.add("a", None, request_id="a")
        old = self.queue.claim("w", 0, 2, request_id="c1")
        new = self.queue.claim("v", 2, 4, request_id="c2")
        self.assertNotEqual(old["token"], new["token"])
        self.assertFalse(self.queue.finish("a", old["token"], 3, True, request_id="f1"))
        self.assertTrue(self.queue.finish("a", new["token"], 3, True, request_id="f2"))

    def test_dependency(self):
        self.queue.add("a", None, request_id="a")
        self.queue.add("b", None, ["a"], priority=100, request_id="b")
        job = self.queue.claim("w", 0, 5, request_id="c")
        self.assertEqual(job["id"], "a")
        self.queue.finish("a", job["token"], 1, True, request_id="f")
        self.assertEqual(self.queue.claim("w", 1, 5, request_id="d")["id"], "b")

    def test_renew_boundary(self):
        self.queue.add("a", 0, request_id="a")
        job = self.queue.claim("w", 2, 3, request_id="c")
        self.assertFalse(self.queue.renew("a", job["token"], 5, 10, request_id="r"))

    def test_cli(self):
        request = {"op": "add", "args": {"job_id": "a", "payload": 1, "request_id": "a"}}
        result = subprocess.run([sys.executable, "leasequeue_cli.py", str(self.path), json.dumps(request)], capture_output=True, text=True, timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"ok": True, "value": True})
