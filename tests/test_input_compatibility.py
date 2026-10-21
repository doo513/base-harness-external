"""Accepted inputs stay accepted across Domain, Core, schemas and caller catalogs."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from harness_external import application_schema, operation_schema
from harness_external.develop_requirements import resolve_references
from harness_external.domain import DevelopModule
from harness_external.errors import HarnessError
from harness_external.service import Harness
from harness_external.worker import execute_job


def parameters(files=1, commands=0, *, longest_ids=False):
    def name(prefix, index):
        value = f"{prefix}{index:03d}"
        return value.ljust(80, "x") if longest_ids else value
    return {"profile": "execution" if commands else "structural",
            "inputs": ["value.txt"], "artifacts": ["value.txt"],
            "expectations": [{"id": name("file", i), "path": "value.txt", "operator": "equals", "expected": "ready"}
                             for i in range(files)],
            "execution_checks": [{"id": name("exec", i), "kind": "command", "argv": ["python3", "-c", "pass"]}
                                 for i in range(commands)]}


class InputCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        (self.workspace / "value.txt").write_text("ready", encoding="utf-8")
        self.api = Harness(self.root / "state")

    def start(self, params=None, key="start"):
        return self.api.start(domain_id="develop", goal="Preserve the declared value", workspace=str(self.workspace),
                              parameters=params or parameters(), request_id=key)["run_id"]

    def measure(self, run_id, key="measure"):
        self.api.submit(run_id, key + "-submit")
        with patch("harness_external.worker.spawn_worker", return_value=None):
            job = self.api.verify(run_id, key)["job_id"]
        execute_job(self.api.store.root, job)
        result = self.api.status(run_id, job)["job"]
        self.assertEqual(result["result"]["status"], "passed")
        items = []
        offset = 0
        while True:
            page = self.api.records(run_id, "measurements", job_id=job, offset=offset, limit=100)
            items.extend(page["items"])
            if page["next_offset"] is None:
                break
            offset = page["next_offset"]
        return [item["observation_id"] for item in items]

    def assessment(self, ids):
        return {"status": "satisfied", "summary": "These declared measurements passed.",
                "uncertainties": ["The checks are not complete goal proof."],
                "interpretation_revision": 1, "cited_observation_ids": ids}

    def test_exact_id_wins_over_another_checks_alias_at_real_start(self):
        params = parameters(commands=2)
        params["execution_checks"][0]["id"] = "probe"
        params["execution_checks"][1]["id"] = "domain.probe"
        params["requirements"] = [{"id": "R1", "statement": "Execute the selected probe",
                                    "check_ids": ["domain.probe"], "minimum_evidence": "command"}]
        run = self.start(params)
        with self.api.store.transaction(write=False) as connection:
            contract = self.api.store.run(connection, run)["contract"]
        self.assertEqual(contract["requirements"][0]["check_ids"], ["domain.probe"])
        self.assertIn("domain.domain.probe", {item["check_id"] for item in contract["checks"]})

    def test_aliases_still_work_but_cannot_override_ineligible_exact_ids(self):
        index = {"domain.probe": {"check_key": "named:probe"},
                 "domain.domain.probe": {"check_key": "named:domain.probe"}}
        self.assertEqual(resolve_references(["probe"], index), ["domain.probe"])
        self.assertEqual(resolve_references(["domain.probe"], index), ["domain.probe"])
        with self.assertRaises(HarnessError):
            resolve_references(["domain.probe"], index, eligible_ids=["domain.domain.probe"])
        with self.assertRaises(HarnessError):
            resolve_references(["probe", "domain.probe"], index)

    def test_unknown_and_ambiguous_aliases_remain_errors(self):
        index = {"first": {"check_key": "named:alias"}, "second": {"definition_key": "named:alias"}}
        for ref in ("alias", "missing"):
            with self.subTest(ref=ref), self.assertRaises(HarnessError) as caught:
                resolve_references([ref], index)
            self.assertEqual(caught.exception.code, "REQUIREMENT_REFERENCE")

    def test_reference_map_accepts_domain_maximum_even_with_longest_ids(self):
        for commands in (1, 16):
            with self.subTest(commands=commands):
                params = parameters(128, commands, longest_ids=True)
                prepared = DevelopModule().prepare("goal", params, {})
                self.assertEqual(len(prepared["check_references"]), 128 + commands)
                self.assertLessEqual(len(json.dumps(prepared["check_references"], separators=(",", ":")).encode("utf-8")), 32000)
                run = self.start(params, "max-" + str(commands))
                restored = self.api.resume(run)["domain_preparation"]["check_references"]
                self.assertEqual(restored, prepared["check_references"])

    def test_display_bound_does_not_expand_domain_admission(self):
        for params in (parameters(129, 1), parameters(128, 17)):
            with self.assertRaises(HarnessError):
                self.start(params)
        self.assertEqual(self.api.list_runs()["total"], 0)

    def test_reference_metadata_remains_bounded_by_count_and_bytes(self):
        base = {"status": "proceed", "available_operations": []}
        for refs in ([{}] * 257, [{"description": "x" * 32000}]):
            with self.assertRaises(HarnessError) as caught:
                self.api._domain_preparation_view({**base, "check_references": refs})
            self.assertEqual(caught.exception.code, "DOMAIN_REFERENCE_MAP")

    def test_citation_schema_and_offline_catalog_preserve_128_limit(self):
        schemas = [
            operation_schema.ASSESSMENT["properties"]["cited_observation_ids"],
            operation_schema.REVISE["properties"]["observation_ids"],
            operation_schema.REVISE["properties"]["assumptions"]["items"]["properties"]["observation_ids"],
            operation_schema.OBSERVE["properties"]["observation_ids"],
            application_schema.knowledge_schema()["properties"]["cited_observation_ids"],
        ]
        for schema in schemas:
            self.assertEqual(schema["maxItems"], 128)
        for count in (100, 101, 127, 128):
            operation_schema.validate_shape(self.assessment([f"observation:{i}" for i in range(count)]), operation_schema.ASSESSMENT)
        with self.assertRaises(HarnessError):
            operation_schema.validate_shape(self.assessment([f"observation:{i}" for i in range(129)]), operation_schema.ASSESSMENT)
        offline = json.loads((ROOT / "skills/harness-workflow/scripts/operation_catalog.json").read_text(encoding="utf-8"))
        self.assertEqual(offline, operation_schema.catalog())
        for operation, key in (("assess", "data"), ("finish", "assessment")):
            self.assertEqual(offline["operations"][operation]["properties"][key]["properties"]["cited_observation_ids"]["maxItems"], 128)

    def test_real_101_to_128_citations_work_for_assess_and_finish(self):
        run = self.start(parameters(128))
        ids = self.measure(run)
        self.assertEqual(len(ids), 128)
        other = self.start(key="foreign")
        foreign_id = self.measure(other)[0]
        for count in (100, 101, 127, 128):
            with self.subTest(count=count):
                measured = self.api.assess(run, self.assessment(ids[:count]), "assess-" + str(count))
                self.assertEqual(measured["assessment"]["citation_summary"], {"current": count, "contextual": 0})
        for bad, code in ((ids[:100] + [ids[0]], "OBSERVATION_REFERENCE"),
                          (ids[:100] + [foreign_id], "OBSERVATION_REFERENCE"),
                          (["missing"], "OBSERVATION_REFERENCE"),
                          (ids + [foreign_id], "INVALID_PARAMETERS")):
            with self.subTest(invalid=code), self.assertRaises(HarnessError) as caught:
                self.api.assess(run, self.assessment(bad), "bad-" + str(len(bad)) + "-" + bad[-1])
            self.assertEqual(caught.exception.code, code)
        with self.assertRaises(HarnessError) as caught:
            self.api.finish(run, "finish-too-many", outcome="completed", assessment=self.assessment(ids + [foreign_id]))
        self.assertEqual(caught.exception.code, "INVALID_PARAMETERS")
        final = self.api.finish(run, "finish-128", outcome="completed", assessment=self.assessment(ids))
        self.assertEqual(final["record"]["assessment"]["citation_summary"]["current"], 128)
        self.assertEqual(final["resolution"]["gate_status"], "passed")
        self.assertFalse(final["ready"])

    def test_old_candidate_citation_stays_contextual_not_current_evidence(self):
        run = self.start()
        ids = self.measure(run)
        (self.workspace / "value.txt").write_text("different", encoding="utf-8")
        self.api.submit(run, "new-submission")
        assessed = self.api.assess(run, self.assessment(ids), "historical")
        self.assertEqual(assessed["assessment"]["citation_summary"], {"current": 0, "contextual": 1})
        with self.assertRaises(HarnessError):
            self.api.finish(run, "premature", outcome="completed")


if __name__ == "__main__":
    unittest.main()
