"""Standard-library version of the dogfood regressions for the strict Sandbox."""
from pathlib import Path
from tempfile import TemporaryDirectory
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from harness_external.errors import HarnessError
from harness_external.service import Harness
from harness_external.worker import execute_job


PARAMETERS = {"profile": "structural", "inputs": ["value.txt"], "artifacts": ["value.txt"],
              "expectations": [{"id": "value", "path": "value.txt", "operator": "equals", "expected": "one"}]}


class EvidenceLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="harness-dogfood-")
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "value.txt").write_text("one")
        self.api = Harness(self.root / "state")
        self.spawn = patch("harness_external.worker.spawn_worker", return_value=None)
        self.spawn.start()

    def tearDown(self):
        self.spawn.stop()
        self.temporary.cleanup()

    def start(self, key, *, mode="strict", budget=None):
        return self.api.start(domain_id="develop", goal="Exercise evidence lifecycle", workspace=str(self.workspace),
                              parameters=PARAMETERS, request_id=key, mode=mode, budget=budget)["run_id"]

    def measure(self, run_id, key):
        self.api.submit(run_id, "submit-" + key)
        job_id = self.api.verify(run_id, "verify-" + key)["job_id"]
        execute_job(self.api.store.root, job_id)
        measurements = self.api.records(run_id, "measurements", job_id=job_id)["items"]
        return job_id, measurements

    def delete_measurement(self, observation_id):
        with self.api.store.transaction() as connection:
            connection.execute("DELETE FROM measurements WHERE observation_id=?", (observation_id,))

    def test_default_summary_validates_references(self):
        run_id = self.start("summary")
        job_id, measurements = self.measure(run_id, "summary")
        self.delete_measurement(measurements[0]["observation_id"])
        with self.assertRaises(HarnessError) as caught:
            self.api.status(run_id, job_id, view="summary")
        self.assertEqual(caught.exception.code, "RESULT_BINDING")

    def test_closed_resume_validates_references(self):
        run_id = self.start("closed")
        _, measurements = self.measure(run_id, "closed")
        self.api.finish(run_id, "finish-closed", outcome="completed")
        self.delete_measurement(measurements[0]["observation_id"])
        with self.assertRaises(HarnessError) as caught:
            self.api.resume(run_id)
        self.assertEqual(caught.exception.code, "RESULT_BINDING")

    def test_contextual_assessment_citations_are_visible(self):
        run_id = self.start("assessment", mode="exploratory")
        _, measurements = self.measure(run_id, "old")
        old = measurements[0]
        self.workspace.joinpath("value.txt").write_text("two")
        current = self.api.submit(run_id, "submit-current")["candidate"]["subject"]
        assessment = self.api.assess(run_id, {"interpretation_revision": 1, "status": "satisfied",
            "summary": "The caller intentionally cites an older Candidate", "uncertainties": [],
            "cited_observation_ids": [old["observation_id"]]}, "assessment-contextual")["assessment"]
        self.assertEqual(assessment["subject"], current)
        self.assertEqual(assessment["citation_summary"], {"current": 0, "contextual": 1})
        binding = assessment["citation_bindings"][0]
        self.assertEqual(binding["observation_id"], old["observation_id"])
        self.assertFalse(binding["current_subject"])
        self.assertTrue(binding["current_interpretation"])
        self.assertTrue(binding["current_check_set"])
        self.assertEqual(binding["relevance"], "contextual")

    def test_persisted_limit_transition_is_idempotent(self):
        run_id = self.start("budget", budget={"max_actions": 1})
        self.api.observe(run_id, {"note": "Consume the only action"}, "one")
        codes = []
        for attempt in range(2):
            caller = self.api if attempt == 0 else Harness(self.api.store.root)
            with self.assertRaises(HarnessError) as caught:
                caller.submit(run_id, "same-over-budget")
            codes.append(caught.exception.code)
        self.assertEqual(codes, ["ACTION_BUDGET_EXHAUSTED", "ACTION_BUDGET_EXHAUSTED"])


if __name__ == "__main__":
    unittest.main()
