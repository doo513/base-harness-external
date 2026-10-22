"""Data delivery is not a new reasoner, evidence source or Run ledger."""
import copy
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from harness.common import canonical_hash
from harness_external.dispatch import invoke
from harness_external.errors import HarnessError
from harness_external.service import Harness
from harness_external.worker import execute_job


class DeliveryFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.work = self.root / "workspace"
        self.work.mkdir()
        (self.work / "value.txt").write_text("ready\n", encoding="utf-8")
        self.api = Harness(self.root / "state")
        self.params = {"profile": "structural", "inputs": ["value.txt"], "artifacts": ["value.txt"],
                       "expectations": [{"id": "value", "path": "value.txt", "operator": "equals", "expected": "ready\n"}]}
        self.run = self.api.start(domain_id="develop", goal="원문 목표\nDo not infer success.",
                                  workspace=str(self.work), parameters=self.params, mode="strict", request_id="start")["run_id"]

    def record(self, key="note", **payload):
        return self.api.observe(self.run, {"note": "", "payload": payload}, key)

    def bound(self, source, value, key="reuse"):
        return {"operation": "revise", "request_id": key,
                "context": {"workspace": str(self.work), "domain": "develop"},
                "arguments": {"run_id": self.run, "data": {"expected_revision": 1, "goal_summary": value}},
                "input_bindings": [{"target": ["data", "goal_summary"], "source": source}]}

    def test_nullable_fields_and_extensions_are_preserved_without_becoming_evidence(self):
        value = "한글 \\" + '"' + "\n\t${do_not_execute}\r\n"
        reply = self.record(fields={"analysis": value, "unknown": None}, extensions={"passed": True, "ready": True})
        payload = reply["observation"]["payload"]
        self.assertEqual(payload, {"fields": {"analysis": value, "unknown": None},
                                   "extensions": {"passed": True, "ready": True}, "parents": []})
        self.assertEqual(reply["record_ref"]["record_digest"], canonical_hash(reply["observation"]))
        self.assertEqual(reply["field_refs"]["analysis"]["sha256"], canonical_hash(value))
        self.assertEqual(self.api.records(self.run, "measurements")["total"], 0)
        self.assertFalse(reply["ready"])
        with self.assertRaises(HarnessError):
            self.api.finish(self.run, "premature", outcome="completed")

    def test_exact_previous_field_is_used_by_existing_domain_core_path(self):
        value = "Current interpretation, not a replacement for the original goal."
        previous = self.record(fields={"summary": value})
        request = self.bound(previous["field_refs"]["summary"], value)
        result = invoke(self.api, request)
        self.assertEqual(self.api.resume(self.run)["interpretation"]["goal_summary"], value)
        self.assertEqual(result["input_bindings"], request["input_bindings"])
        self.assertEqual(result["interpretation"]["input_bindings"], request["input_bindings"])
        self.assertEqual(self.api.resume(self.run)["intent"]["original_goal"], "원문 목표\nDo not infer success.")
        self.assertEqual(invoke(Harness(self.root / "state"), copy.deepcopy(request)), result)
        unbound = copy.deepcopy(request)
        unbound.pop("input_bindings")
        with self.assertRaises(HarnessError) as error:
            invoke(self.api, unbound)
        self.assertEqual(error.exception.code, "REQUEST_CONFLICT")

    def test_mismatching_or_foreign_reference_does_not_mutate_the_run(self):
        previous = self.record(fields={"summary": "original"})
        original = self.api.resume(self.run)["interpretation"]["ref"]
        for changed in ("replaced", ""):
            with self.assertRaises(HarnessError) as error:
                invoke(self.api, self.bound(previous["field_refs"]["summary"], changed))
            self.assertEqual(error.exception.code, "DELIVERY_VALUE_MISMATCH")
        source = copy.deepcopy(previous["field_refs"]["summary"])
        source["run_id"] = "run_" + "0" * 32
        with self.assertRaises(HarnessError) as error:
            invoke(self.api, self.bound(source, "original"))
        self.assertEqual(error.exception.code, "DELIVERY_RUN_MISMATCH")
        self.assertEqual(self.api.resume(self.run)["interpretation"]["ref"], original)

    def test_parent_links_are_checked_and_old_records_are_unchanged(self):
        first = self.record(fields={"summary": "first"})
        second = self.record("next", fields={"summary": "second"}, parents=[first["record_ref"]])
        self.assertEqual(second["observation"]["payload"]["parents"], [first["record_ref"]])
        self.assertEqual(self.api.records(self.run, "notes")["items"][0], first["observation"])
        altered = copy.deepcopy(first["record_ref"])
        altered["record_digest"] = "0" * 64
        with self.assertRaises(HarnessError):
            self.record("bad-parent", parents=[altered])
        self.assertEqual(self.api.records(self.run, "notes")["total"], 2)

    def test_model_field_cannot_supply_assessment_status_or_measurement_citations(self):
        first = self.record(fields={"status": "satisfied"})
        req = self.bound(first["field_refs"]["status"], "satisfied")
        req["operation"] = "assess"
        req["arguments"]["data"] = {"status": "satisfied", "summary": "claim", "uncertainties": [],
                                         "cited_observation_ids": [], "interpretation_revision": 1}
        req["input_bindings"][0]["target"] = ["data", "status"]
        with self.assertRaises(HarnessError) as error:
            invoke(self.api, req)
        self.assertEqual(error.exception.code, "DELIVERY_TARGET")

    def test_actual_worker_observations_remain_separate_from_claims(self):
        self.record(fields={"claim": "All passed"}, extensions={"observation_id": "made-up"})
        self.api.submit(self.run, "submit")
        with patch("harness_external.worker.spawn_worker", return_value=None):
            job = self.api.verify(self.run, "verify")["job_id"]
        execute_job(self.api.store.root, job)
        facts = self.api.records(self.run, "measurements", job_id=job)["items"]
        self.assertEqual(len(facts), 1)
        state = self.api.status(self.run)
        self.assertEqual(state["resolution"]["gate_status"], "passed")
        self.assertEqual(self.api.records(self.run, "notes")["total"], 1)

    def test_empty_record_and_ordinary_extra_field_names_do_not_infer_meaning(self):
        empty = self.record()
        self.assertEqual(empty["observation"]["payload"], {"fields": {}, "extensions": {}, "parents": []})
        value = {"password_policy": "Do not store passwords", "token_count": 0, "items": list(range(250))}
        populated = self.record("names", fields=value)
        self.assertEqual(populated["observation"]["payload"]["fields"], value)
        self.assertEqual(self.api.resume(self.run)["gates"]["status"], "unsatisfied")

    def test_missing_and_changed_reference_fail_closed(self):
        first = self.record(fields={"summary": "first"})
        for changed in ({"record_id": 99}, {"sha256": "0" * 64}, {"path": ["missing"]}):
            with self.subTest(changed=changed), self.assertRaises(HarnessError):
                invoke(self.api, self.bound({**first["field_refs"]["summary"], **changed}, "first"))
        # Accidental on-disk changes are detected; this is not a protected
        # certification against a writer who can also rewrite every digest.
        with self.api.store.transaction() as connection:
            connection.execute("UPDATE run_records SET data='{}' WHERE run_id=? AND kind='observations'", (self.run,))
        with self.assertRaises(HarnessError) as error:
            invoke(self.api, self.bound(first["field_refs"]["summary"], "first"))
        self.assertEqual(error.exception.code, "SEMANTIC_RECORD_CORRUPT")

    def test_same_values_with_different_source_are_a_different_request(self):
        first = self.record(fields={"summary": "same"})
        second = self.record("next", fields={"summary": "same"})
        request = self.bound(first["field_refs"]["summary"], "same")
        invoke(self.api, request)
        request["input_bindings"][0]["source"] = second["field_refs"]["summary"]
        with self.assertRaises(HarnessError) as error:
            invoke(self.api, request)
        self.assertEqual(error.exception.code, "REQUEST_CONFLICT")

    def test_outdated_revision_and_overlapping_bindings_do_not_succeed(self):
        first = self.record(fields={"summary": "same"})
        request = self.bound(first["field_refs"]["summary"], "same")
        invoke(self.api, request)
        request["request_id"] = "new-intent-old-revision"
        with self.assertRaises(HarnessError) as error:
            invoke(self.api, request)
        self.assertEqual(error.exception.code, "STALE_INTERPRETATION")
        request["input_bindings"].append(copy.deepcopy(request["input_bindings"][0]))
        with self.assertRaises(HarnessError) as error:
            invoke(self.api, request)
        self.assertEqual(error.exception.code, "DELIVERY_TARGET")

    def test_bound_source_is_rechecked_before_publish(self):
        first = self.record(fields={"summary": "first"})
        request = self.bound(first["field_refs"]["summary"], "first")
        original = self.api._publish_prepared_run
        def changed_during_preparation(*args, **kwargs):
            other = Harness(self.root / "state")
            with other.store.transaction() as connection:
                connection.execute("UPDATE run_records SET data='{}' WHERE run_id=? AND kind='observations'", (self.run,))
            return original(*args, **kwargs)
        with patch.object(self.api, "_publish_prepared_run", side_effect=changed_during_preparation):
            with self.assertRaises(HarnessError) as error:
                invoke(self.api, request)
        self.assertEqual(error.exception.code, "SEMANTIC_RECORD_CORRUPT")
        with self.api.store.transaction(write=False) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM requests WHERE scope=? AND request_id='reuse'", (self.run,)).fetchone()[0], 0)

    def test_payload_bounds_and_legacy_notes_remain_strict(self):
        for invalid in ([], {"fields": []}, {"extensions": []}, {"unknown": True},
                        {"fields": {"number": float("nan")}}, {"fields": {"many": "a" * 64000}}):
            with self.subTest(value=type(invalid).__name__), self.assertRaises(HarnessError):
                self.api.observe(self.run, {"note": "", "payload": invalid}, "invalid")
        with self.assertRaises(HarnessError):
            self.api.observe(self.run, {"note": ""}, "empty-legacy")
        self.assertEqual(self.api.records(self.run, "notes")["total"], 0)

    def test_two_sessions_cannot_mix_thread_local_sources(self):
        other = self.api.start(domain_id="develop", goal="other", workspace=str(self.work),
                               parameters=self.params, mode="strict", request_id="other")["run_id"]
        first = self.record(fields={"summary": "first"})
        second = self.api.observe(other, {"note": "", "payload": {"fields": {"summary": "second"}}}, "note")
        a = self.bound(first["field_refs"]["summary"], "first")
        b = self.bound(second["field_refs"]["summary"], "second")
        b["arguments"]["run_id"] = other
        with ThreadPoolExecutor(max_workers=2) as threads:
            replies = list(threads.map(lambda request: invoke(self.api, request), (a, b)))
        self.assertEqual([reply["interpretation"]["goal_summary"] for reply in replies], ["first", "second"])
        self.assertEqual(self.api.resume(self.run)["interpretation"]["goal_summary"], "first")
        self.assertEqual(self.api.resume(other)["interpretation"]["goal_summary"], "second")

    def test_historical_measurement_can_be_linked_but_cannot_pass_new_candidate(self):
        from harness_external.delivery import ref
        self.api.submit(self.run, "old-submit")
        with patch("harness_external.worker.spawn_worker", return_value=None):
            job = self.api.verify(self.run, "old-verify")["job_id"]
        execute_job(self.api.store.root, job)
        old = self.api.records(self.run, "measurements", job_id=job)["items"][0]
        source = ref(self.run, "measurements", old["observation_id"], old)
        (self.work / "value.txt").write_text("changed", encoding="utf-8")
        self.api.submit(self.run, "new-submit")
        request = {"operation": "observe", "request_id": "link-old",
                   "context": {"workspace": str(self.work), "domain": "develop"},
                   "arguments": {"run_id": self.run, "data": {"note": "", "payload": {"fields": {"old": old}}}},
                   "input_bindings": [{"target": ["data", "payload", "fields", "old"], "source": source}]}
        linked = invoke(self.api, request)
        self.assertEqual(linked["observation"]["trust"], "untrusted")
        self.assertEqual(self.api.resume(self.run)["gates"]["status"], "unsatisfied")
        with self.assertRaises(HarnessError):
            self.api.finish(self.run, "premature", outcome="completed")

    def test_check_assessment_and_closeout_keep_input_lineage(self):
        check = {"kind": "file", "path": "value.txt", "operator": "equals", "expected": "ready\n"}
        note = self.record(fields={"check": check, "summary": "Only the measured file predicates passed."})
        context = {"workspace": str(self.work), "domain": "develop"}
        sources = [{"target": ["data", "parameters"], "source": note["field_refs"]["check"]}]
        added = invoke(self.api, {"operation": "check", "request_id": "check", "context": context,
                       "arguments": {"run_id": self.run, "data": {"check_id": "extra", "interpretation_revision": 1, "parameters": check}},
                       "input_bindings": sources})
        self.assertEqual(added["check"]["input_bindings"], sources)
        self.api.submit(self.run, "submit")
        with patch("harness_external.worker.spawn_worker", return_value=None):
            job = self.api.verify(self.run, "verify")["job_id"]
        execute_job(self.api.store.root, job)
        measurements = self.api.records(self.run, "measurements", job_id=job)["items"]
        source = note["field_refs"]["summary"]
        summary = note["observation"]["payload"]["fields"]["summary"]
        assessment = invoke(self.api, {"operation": "assess", "request_id": "assessment", "context": context,
            "arguments": {"run_id": self.run, "data": {"status": "satisfied", "summary": summary, "uncertainties": [],
                "cited_observation_ids": [item["observation_id"] for item in measurements], "interpretation_revision": 1}},
            "input_bindings": [{"target": ["data", "summary"], "source": source}]})
        self.assertEqual(assessment["assessment"]["input_bindings"], assessment["input_bindings"])
        result = invoke(self.api, {"operation": "finish", "request_id": "finish", "context": context,
            "arguments": {"run_id": self.run, "outcome": "completed", "summary": summary},
            "input_bindings": [{"target": ["summary"], "source": source}]})
        self.assertEqual(result["record"]["input_bindings"], result["input_bindings"])
        self.assertEqual(result["resolution"]["display_status"], "closed_checks_passed")
        self.assertFalse(result["ready"])


if __name__ == "__main__":
    unittest.main()
