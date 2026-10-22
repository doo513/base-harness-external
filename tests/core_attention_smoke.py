"""Standard-library integration probe, also runnable inside the strict Sandbox."""
import base64
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from harness.common import canonical_hash
from harness_external.develop_analysis import guidance
from harness_external.service import Harness
from harness_external.worker import execute_job


def main():
    with tempfile.TemporaryDirectory(prefix="core-attention-probe-") as directory:
        root = Path(directory)
        workspace = root / "work"
        workspace.mkdir()
        output = workspace / "result.txt"
        output.write_text("broken", encoding="utf-8")
        api = Harness(root / "state")
        run = api.start(domain_id="develop", goal="R1. Produce ready", workspace=str(workspace),
                        request_id="start", mode="strict", parameters={
                            "profile": "structural", "inputs": ["result.txt"], "artifacts": ["result.txt"],
                            "expectations": [{"id": "result", "path": "result.txt", "operator": "equals", "expected": "ready"}],
                            "requirements": [{"id": "R1", "kind": "output", "statement": "Produce ready", "check_ids": ["domain.result"],
                                              "source_refs": [{"source_id": "goal", "start_line": 1, "end_line": 1}]}]})["run_id"]
        api.submit(run, "submit")
        with patch("harness_external.worker.spawn_worker", return_value=None):
            job = api.verify(run, "verify")["job_id"]
        execute_job(api.store.root, job)
        before = api.status(run)["run"]
        observed = api.resume(run, view="attention")
        issue = next(item for item in observed["attention"] if item["kind"] == "check")
        assert issue["comparison_status"] == "failed" and issue["scope"] == "current"
        assert observed["selected_job"]["is_current_verification"]
        assert not observed["ready"] and observed["gates"]["status"] == "unsatisfied"
        assert "VERIFICATION_REQUIRED" in observed["operation_constraints"]["operations"]["finish_completed"]["blocked_by"]
        detail = issue["detail"]
        restored = api.records(run, **{**detail, "detail": {**detail["detail"], "limit": 16384}})
        assert restored["next_offset"] is None
        record = json.loads(base64.b64decode(restored["data"]))
        assert canonical_hash(record) == detail["detail"]["record_digest"]
        assert record["observation_id"] == issue["observation_id"]
        assert api.status(run)["run"] == before
        output.write_text("ready", encoding="utf-8")
        api.submit(run, "resubmit")
        assert api.resume(run, view="attention")["selected_job"] is None
        historical = api.status(run, job, view="attention")
        assert historical["selected_job"]["relevance"] == "historical"
        assert not historical["selected_job"]["is_current_verification"]
        closed = api.finish(run, "partial", outcome="partial", summary="New version is not verified yet")
        assert closed["record"]["outcome"] == "partial"
        chosen = guidance(perspectives=["verification"])
        assert [item["id"] for item in chosen["analysis_perspectives"]] == ["verification"]
        assert chosen["persistence_required"] is False
        assert len(json.dumps(chosen)) < len(json.dumps(guidance()))
    print("core-attention-smoke: passed")


if __name__ == "__main__":
    main()
