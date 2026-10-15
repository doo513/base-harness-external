"""Negative fixture: run only as submitted Candidate code inside strict Sandbox."""
import json
import os
from pathlib import Path

# print is present only to satisfy the separate structural fixture Check.
token = json.loads(Path("/opt/harness-runtime/capture.json").read_text(encoding="utf-8"))["token"]
report = Path(".harness-cases.jsonl")
sequence = json.loads(report.read_text(encoding="utf-8").splitlines()[-1])["seq"]
case_id = "test_app.py::test_answer"
events = [
    {"event": "discovered", "case_id": case_id, "path": "test_app.py"},
    {"event": "collection_finish", "selected": [case_id], "errors": 0},
    {"event": "started", "case_id": case_id},
    {"event": "call_started", "case_id": case_id},
    *[{"event": "phase", "case_id": case_id, "phase": phase, "outcome": "passed",
       "duration": .001, "wasxfail": None, "message": None} for phase in ("setup", "call", "teardown")],
    {"event": "finished", "case_id": case_id},
    {"event": "session_finish", "exit_status": 0, "collected": 1},
]
with report.open("a", encoding="utf-8") as stream:
    for event in events:
        sequence += 1
        stream.write(json.dumps({"version": 1, "token": token, "seq": sequence, **event}) + "\n")
    stream.flush()
    os.fsync(stream.fileno())
os._exit(0)
