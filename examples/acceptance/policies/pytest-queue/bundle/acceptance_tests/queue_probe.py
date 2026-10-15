"""Produce sample API responses; predicates are evaluated outside this process.

The historical policy directory keeps its name for existing documentation links.
This is a small observation sample, not proof of all queue behavior.
"""
import json
from pathlib import Path
import tempfile

from leasequeue import Queue


results = {}
with tempfile.TemporaryDirectory() as temporary:
    for label, dependencies in (("none", None), ("empty", [])):
        queue = Queue(Path(temporary) / (label + ".sqlite3"))
        try:
            first = queue.add("job", {"value": 1}, request_id="same")
            replay = queue.add("job", {"value": 1}, depends_on=dependencies, request_id="same")
            results[label] = [first, replay]
        except Exception as error:
            results[label] = {"error": type(error).__name__}
    queue = Queue(Path(temporary) / "events.sqlite3")
    queue.add("job", {}, request_id="same")
    queue.add("job", {}, request_id="same")
    results["events"] = len(queue.events())
print(json.dumps(results, sort_keys=True))
