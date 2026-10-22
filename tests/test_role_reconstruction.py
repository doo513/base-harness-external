"""Role boundaries and real fault discrimination, without a model or pytest dependency."""
import ast
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from harness.common import canonical_hash
from harness_external.errors import HarnessError
from harness_external.registry import DomainRegistry
from harness_external.service import Harness
from harness_external.worker import execute_job

helper_spec = importlib.util.spec_from_file_location("role_client", ROOT / "skills/harness-workflow/scripts/harness_client.py")
helper = importlib.util.module_from_spec(helper_spec)
helper_spec.loader.exec_module(helper)


class IndependentDomain:
    domain_id = "independent-pair"
    revision = "1"

    def prepare(self, goal, parameters, verifier, *, exploratory=False, intent=None):
        checks = [{"kind": "command", "argv": ["python3", "probe.py"], "cwd": ".",
                   "expectedExitCode": 0, "timeout_seconds": 10, "check_id": "probe", "check_key": "probe"}]
        body = {"original_goal": goal, "domain_id": self.domain_id, "domain_revision": self.revision,
                "inputs": parameters["inputs"], "artifacts": ["app.py"], "profile": "execution", "checks": checks,
                "verifier": verifier, "rules": {"all_checks_required": True, "snapshot_required": True}, "limitations": []}
        body["contract_hash"] = canonical_hash(body)
        return {"contract": body, "questions": [], "status": "proceed", "available_operations": ["submit", "verify", "finish_completed"]}

    def normalize_check(self, parameters, contract):
        return copy.deepcopy(parameters)

    def prepare_comparison(self, parameters, contract, *, checks):
        return {"check_ids": ["probe"], "shared_inputs": parameters["shared_inputs"]}


class RoleReconstruction(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "work"
        self.workspace.mkdir()
        (self.workspace / "app.py").write_text("value = 0\n", encoding="utf-8")
        (self.workspace / "probe.py").write_text("import app\nassert app.value == 1\n", encoding="utf-8")
        (self.workspace / "SPEC.txt").write_text("Do not edit SPEC.txt.\n", encoding="utf-8")
        self.api = Harness(self.root / "state", domains=DomainRegistry([IndependentDomain()]))

    def start(self, **kwargs):
        return self.api.start(domain_id="independent-pair", goal="Do not edit SPEC.txt.", workspace=str(self.workspace),
                              parameters={"inputs": ["app.py", "probe.py"]}, request_id="start", **kwargs)["run_id"]

    def protected(self):
        return {"unchanged_files": [{"path": "SPEC.txt", "source_refs": [{"source_id": "goal", "start_line": 1, "end_line": 1}]}]}

    def test_protection_works_without_develop_or_helper_and_cannot_be_rebased(self):
        with patch.dict(sys.modules, {"harness_external.domain": None}):
            run = self.start(workspace_constraints=self.protected())
            (self.workspace / "SPEC.txt").write_text("changed\n", encoding="utf-8")
            with self.assertRaises(HarnessError) as caught:
                self.api.submit(run, "changed")
            self.assertEqual(caught.exception.code, "WORKSPACE_CONSTRAINT_VIOLATION")
            self.assertIsNone(self.api.resume(run)["candidate"])

    def test_protection_rechecks_after_submit_and_partial_reports_violation(self):
        run = self.start(mode="exploratory", workspace_constraints=self.protected())
        self.api.submit(run, "submit")
        (self.workspace / "SPEC.txt").unlink()
        with self.assertRaises(HarnessError):
            self.api.finish(run, "finish", outcome="completed")
        final = self.api.finish(run, "partial", outcome="partial")
        self.assertEqual(final["record"]["workspace_constraints"]["status"], "violated")

    def test_core_schema_exposes_assessment_and_direct_validation_uses_it(self):
        schema = self.api.describe(operation="finish")["input_schema"]["properties"]["assessment"]
        self.assertIn("interpretation_revision", schema["required"])
        self.assertEqual(schema["properties"]["status"]["enum"], ["satisfied", "partial", "unsolved", "not_assessed"])
        run = self.start()
        with self.assertRaises(HarnessError) as caught:
            self.api.assess(run, {"status": "satisfied"}, "bad")
        self.assertIn("/interpretation_revision", [item["path"] for item in caught.exception.diagnostics])

    def test_missing_domain_declaration_is_not_reported_as_missing_gate_authority(self):
        api = Harness(self.root / "clarification-state")
        run = api.start(domain_id="develop", goal="Produce the result", workspace=str(self.workspace), request_id="start",
                        mode="exploratory", parameters={"inputs": ["app.py", "probe.py"],
                        "execution_checks": [{"kind": "command", "id": "probe", "argv": ["python3", "probe.py"]}]})["run_id"]
        api.submit(run, "submit")
        state = api.resume(run)
        self.assertEqual(state["gates"]["status"], "not_required")
        with self.assertRaises(HarnessError) as error:
            api.finish(run, "premature", outcome="completed")
        self.assertEqual(error.exception.code, "VERIFICATION_REQUIRED")  # retain public error-code compatibility
        self.assertTrue(any(item["code"] == "NEEDS_INPUT" for item in error.exception.diagnostics))
        self.assertNotIn("gate", str(error.exception).lower())
        self.assertEqual(state["operation_constraints"]["operations"]["finish_completed"]["blocked_by"], ["NEEDS_INPUT"])
        api.revise(run, {"expected_revision": 1, "parameters": {"artifacts": ["app.py"],
                   "expectations": [{"path": "app.py", "operator": "contains", "expected": "value"}]}}, "declare")
        api.submit(run, "declared")
        result = api.finish(run, "finish", outcome="completed")
        self.assertEqual(result["resolution"]["display_status"], "closed_unverified")
        self.assertEqual(result["resolution"]["gate_status"], "not_required")

    def test_pair_requires_original_and_does_not_start_a_job_on_error(self):
        run = self.start()
        candidate = self.api.submit(run, "submit")["candidate"]
        with self.assertRaises(HarnessError) as caught:
            self.api.compare(run, "pair", expected_candidate_hash=candidate["candidate_hash"], parameters={"shared_inputs": ["probe.py"]})
        self.assertEqual(caught.exception.code, "BASELINE_REQUIRED")
        self.assertEqual(self.api.records(run, "jobs")["total"], 0)

    def test_domain_comparison_can_select_a_later_registered_check(self):
        api = Harness(self.root / "registered-state")
        params = {"inputs": ["app.py", "probe.py"], "artifacts": ["app.py"],
                  "expectations": [{"path": "app.py", "operator": "contains", "expected": "value"}],
                  "execution_checks": [{"kind": "command", "id": "original", "argv": ["python3", "probe.py"]}]}
        run = api.start(domain_id="develop", goal="measure", workspace=str(self.workspace), parameters=params,
                        mode="exploratory", request_id="start", capture_baseline=True)["run_id"]
        api.register_check(run, {"check_id": "extra", "interpretation_revision": 1,
                                "parameters": {"kind": "command", "argv": ["python3", "probe.py"]}}, "register")
        candidate = api.submit(run, "submit")["candidate"]
        with patch("harness_external.worker.spawn_worker", return_value=None):
            job = api.compare(run, "extra-pair", expected_candidate_hash=candidate["candidate_hash"],
                              parameters={"check_ids": ["extra"], "test_paths": ["probe.py"]})["job_id"]
        self.assertEqual([c["check_id"] for c in api.status(run, job)["job"]["checks"]], ["extra"])

    def test_core_catalog_matches_portable_copy_and_requires_no_domain(self):
        from harness_external.operation_schema import catalog
        self.assertEqual(catalog(), helper.CATALOG)
        with patch.dict(sys.modules, {"harness_external.domain": None}):
            value = self.api.describe(operation="finish")
        self.assertEqual(value["input_schema"], helper.describe("finish")["core_input_schema"])

    def test_domain_alone_normalizes_names_and_selects_shared_files(self):
        from harness_external.domain import DevelopModule
        module = DevelopModule()
        params = {"inputs": ["app.py", "probe.py"], "artifacts": ["app.py"],
                  "expectations": [{"id": "entry", "path": "app.py", "operator": "contains", "expected": "value"}],
                  "execution_checks": [{"kind": "command", "id": "probe", "argv": ["python3", "probe.py"]}],
                  "requirements": [{"id": "R1", "statement": "value", "check_ids": ["probe"], "minimum_evidence": "command"}]}
        prepared = module.prepare("goal", params, {}, exploratory=False)
        body = prepared["contract"]
        self.assertIn({"input_id": "probe", "check_id": "domain.probe"}, prepared["check_references"])
        self.assertEqual(body["requirements"][0]["check_ids"], ["domain.probe"])
        with patch("subprocess.Popen", side_effect=AssertionError("No execution in Domain")), patch("pathlib.Path.open", side_effect=AssertionError("No project I/O in comparison preparation")):
            self.assertEqual(module.prepare_comparison({"check_ids": ["probe"], "test_paths": ["probe.py"]}, body),
                             {"check_ids": ["domain.probe"], "shared_inputs": ["probe.py"]})
        with self.assertRaises(HarnessError):
            module.prepare_comparison({"check_ids": ["probe"], "test_paths": ["app.py"]}, body)
        params["requirements"][0]["check_ids"] = ["missing"]
        with self.assertRaises(HarnessError) as error:
            module.prepare("goal", params, {})
        self.assertIn("domain.probe", str(error.exception.diagnostics))
        self.assertEqual(error.exception.diagnostics[0]["path"], "/requirements/0/check_ids/0")

    def test_binding_reuse_and_automatic_request_path_preserve_identity(self):
        client = helper.Client({"schema_version": 1, "harness_repo": str(ROOT), "state_dir": str(self.api.store.root), "state_lifetime": "ephemeral"})
        run = self.start(mode="exploratory")
        path = self.root / "binding.json"
        with patch.object(client, "call", side_effect=lambda request: {"ok": True, **__import__("harness_external.dispatch", fromlist=["invoke"]).invoke(self.api, request)}):
            first = client.bind(run, str(self.workspace), "independent-pair", path)
            self.assertEqual(first, client.bind(run, str(self.workspace), "independent-pair", path))
            binding = helper.read_json(str(path))
            reply = client.call_operation("observe", {"data": {"note": "one"}}, binding=binding, binding_path=path, action_id="note")
            replay = client.call_operation("observe", {"data": {"note": "one"}}, binding=binding, binding_path=path, action_id="note")
            self.assertEqual(reply, replay)
            self.assertTrue(Path(reply["caller_request"]["request_file"]).is_file())
            with self.assertRaises(helper.ClientError) as error:
                client.call_operation("observe", {"data": {"note": "changed"}}, binding=binding, binding_path=path, action_id="note")
            self.assertEqual(error.exception.code, "SAVED_REQUEST_CONFLICT")

    def test_start_action_names_are_workspace_scoped_and_legacy_envelopes_replay(self):
        config = {"schema_version": 1, "harness_repo": str(ROOT), "state_dir": str(self.api.store.root), "state_lifetime": "ephemeral"}
        client = helper.Client(config)
        args = {"domain": "independent-pair", "goal": "One original task", "workspace": str(self.workspace),
                "mode": "exploratory", "provenance": {"declared_author": "model"}, "parameters": {"inputs": ["app.py", "probe.py"]}}
        first = client.make_request("start", args, action_id="start")["request"]
        second = client.make_request("start", {**args, "workspace": str(self.root / "another")}, action_id="start")["request"]
        self.assertNotEqual(first["request_id"], second["request_id"])
        self.assertEqual(first, client.make_request("start", args, action_id="start")["request"])
        # Changed content is not an implicit new intent in the same scope.
        changed = client.make_request("start", {**args, "goal": "Different task"}, action_id="start")["request"]
        self.assertEqual(first["request_id"], changed["request_id"])
        legacy = {**first, "request_id": helper.action_key(None, "start")}
        legacy.pop("task_id", None)  # actual v1 saved envelopes have no task routing
        with patch.object(client, "execute", side_effect=lambda op, data: {"ok": True, **__import__("harness_external.dispatch", fromlist=["invoke"]).invoke(self.api, data["request"])}):
            original = client.call(legacy)
            self.assertEqual(original, client.call(copy.deepcopy(legacy)))
        self.assertEqual(self.api.list_runs()["total"], 1)

    def test_workspace_guard_is_explicit_not_a_read_side_effect(self):
        run = self.start(workspace_constraints=self.protected())
        with patch("harness_external.workspace_constraints.inspect", side_effect=AssertionError("No repeated file scan")):
            self.assertEqual(self.api.resume(run)["workspace_constraints"]["status"], "satisfied")
        (self.workspace / "SPEC.txt").write_text("new", encoding="utf-8")
        self.assertEqual(self.api.status(run, check_workspace=True, view="compact")["workspace_constraints"]["status"], "violated")

    def test_workspace_links_read_failure_and_stale_source_are_not_success(self):
        run = self.start(workspace_constraints=self.protected())
        with patch("harness_external.workspace_constraints.stream_file", side_effect=PermissionError("blocked")):
            with self.assertRaises(HarnessError):
                self.api.submit(run, "unreadable")
        (self.workspace / "SPEC.txt").unlink()
        (self.workspace / "SPEC.txt").symlink_to(self.workspace / "app.py")
        with self.assertRaises(HarnessError):
            self.api.submit(run, "link")
        with self.assertRaises(HarnessError):
            self.api.revise(run, {"expected_revision": 1, "workspace_constraints": {"unchanged_files": []}}, "relax")

    def test_runtime_continuation_cannot_erase_or_rebase_workspace_constraints(self):
        run = self.start(workspace_constraints=self.protected())
        self.api.finish(run, "partial", outcome="partial")
        for declaration in (None, self.protected()):
            (self.workspace / "SPEC.txt").write_text("changed", encoding="utf-8")
            with self.assertRaises(HarnessError):
                self.api.start(domain_id="independent-pair", goal="Do not edit SPEC.txt.", workspace=str(self.workspace),
                               parameters={"inputs": ["app.py", "probe.py"]}, request_id="continued",
                               predecessor_run_id=run, continuation_reason="Runtime changed", workspace_constraints=declaration)

    def test_stale_pair_leaves_no_published_views_or_jobs(self):
        run = self.start(capture_baseline=True)
        self.api.submit(run, "submit")
        before = set(self.api.store.directory(run).glob("candidate_*"))
        with self.assertRaises(HarnessError) as error:
            self.api.compare(run, "stale", expected_candidate_hash="0" * 64, parameters={"shared_inputs": ["probe.py"]})
        self.assertEqual(error.exception.code, "CANDIDATE_SUPERSEDED")
        self.assertEqual(before, set(self.api.store.directory(run).glob("candidate_*")))
        self.assertEqual(self.api.records(run, "jobs")["total"], 0)

    def test_abandoned_comparison_staging_is_recoverable_without_touching_candidates(self):
        run = self.start(capture_baseline=True)
        candidate = self.api.submit(run, "submit")["candidate"]
        stage = Path(tempfile.mkdtemp(prefix=".compare_", dir=self.api.store.directory(run)))
        (stage / ".capture.lock").write_bytes(b"")
        old = time.time() - 3600
        os.utime(stage, (old, old))
        result = self.api.cleanup(run, "cleanup", apply=True, min_age_seconds=60)
        row = next(item for item in result["items"] if item["path"] == str(stage))
        self.assertEqual(row["state"], "quarantined")
        self.assertTrue(Path(row["recovery_path"]).is_dir())
        self.assertTrue((self.api.store.directory(run) / candidate["candidate_id"]).is_dir())

    def test_workspace_change_during_capture_is_rejected(self):
        from harness_external import snapshots
        run = self.start(workspace_constraints=self.protected())
        original = snapshots.iter_snapshot_chunks
        def changing(root, relative):
            yield from original(root, relative)
            if relative == "SPEC.txt":
                (Path(root) / relative).write_text("changed during read", encoding="utf-8")
        with patch.object(snapshots, "iter_snapshot_chunks", changing):
            with self.assertRaises(HarnessError):
                self.api.submit(run, "racing")

    def test_acceptance_overlay_cannot_hide_a_forbidden_workspace_edit(self):
        policy = self.root / "policies" / "fixed"
        (policy / "bundle").mkdir(parents=True)
        (policy / "bundle/SPEC.txt").write_text("Do not edit SPEC.txt.\n", encoding="utf-8")
        definition = {"schema_version": "acceptance-policy-v1", "policy_id": "fixed", "revision": "1", "domain_id": "develop",
                      "parameters": {"profile": "structural", "inputs": ["app.py", "SPEC.txt"], "artifacts": ["app.py"],
                                     "expectations": [{"id": "spec", "path": "SPEC.txt", "operator": "equals", "expected": "Do not edit SPEC.txt.\n"},
                                                      {"id": "entry", "path": "app.py", "operator": "contains", "expected": "value"}]},
                      "requirements": [{"id": "R1", "statement": "preserve", "check_ids": ["domain.spec"]}],
                      "required_check_ids": ["domain.spec"], "bundle": {"version": "1", "files": ["SPEC.txt"]},
                      "approval": {"declared_by": "test-operator", "reference": "test-policy"}}
        (policy / "policy.json").write_text(json.dumps(definition), encoding="utf-8")
        api = Harness(self.root / "accept-state", policy_root=policy.parent)
        run = api.start(domain_id="develop", goal="Do not edit SPEC.txt.", workspace=str(self.workspace), parameters={},
                        request_id="start", mode="acceptance", policy_id="fixed", workspace_constraints=self.protected())["run_id"]
        (self.workspace / "SPEC.txt").write_text("changed in the Host workspace", encoding="utf-8")
        with self.assertRaises(HarnessError) as error:
            api.submit(run, "hidden-edit")
        self.assertEqual(error.exception.code, "WORKSPACE_CONSTRAINT_VIOLATION")

    def test_actual_helper_compact_and_discovery_use_consistent_syntax(self):
        config = {"schema_version": 1, "harness_repo": str(ROOT), "state_dir": str(self.api.store.root), "state_lifetime": "ephemeral"}
        config_path, binding_path = self.root / "client.json", self.root / "binding.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        client = helper.Client(config)
        run = self.start(mode="exploratory")
        client.bind(run, str(self.workspace), "independent-pair", binding_path)
        with redirect_stdout(io.StringIO()) as output:
            code = helper.main(["--config", str(config_path), "call", "--operation", "resume", "--binding", str(binding_path), "--format", "compact"])
        self.assertEqual(code, 0, output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["view"], "compact")
        with redirect_stdout(io.StringIO()) as output:
            code = helper.main(["describe", "--helper", "find"])
        self.assertEqual(code, 0)
        self.assertIn("--domain", json.loads(output.getvalue())["required"])
        with redirect_stdout(io.StringIO()) as output:
            code = helper.main(["--config", str(config_path), "describe", "--operation", "finish", "--runtime"])
        self.assertEqual(code, 0, output.getvalue())
        self.assertTrue(json.loads(output.getvalue())["matches_bundled_schema"])

    def test_compact_and_attention_preserve_admission_and_diagnostic_scope(self):
        value = self.api.start(domain_id="independent-pair", goal="Do not edit SPEC.txt.", workspace=str(self.workspace),
                               parameters={"inputs": ["app.py", "probe.py"]}, request_id="compact", workspace_constraints=self.protected())
        value = {"ok": True, **value}
        self.assertEqual(helper.compact_call_result("start", value)["workspace_constraints"], value["workspace_constraints"])
        resumed = {"api_version": "external-harness-v2", "ok": True, "assurance": "local-advisory", "ready": False,
                   **self.api.resume(value["run_id"])}
        from harness_external.attention import attention_report
        result = attention_report(resumed, None, {"items": [], "total": 0, "next_offset": None})
        self.assertEqual(result["workspace_constraints"], resumed["workspace_constraints"])
        self.assertEqual(result["diagnostic"], resumed["diagnostic"])

    def test_comparison_budget_and_original_measurement_remain_separate(self):
        run = self.start(capture_baseline=True, budget={"max_verifications": 1})
        candidate = self.api.submit(run, "submit")["candidate"]
        with patch("harness_external.worker.spawn_worker", return_value=None):
            job = self.api.compare(run, "pair", expected_candidate_hash=candidate["candidate_hash"], parameters={"shared_inputs": ["probe.py"]})["job_id"]
        self.api.cancel(run, job, "cancel")
        for _ in range(2):
            with self.assertRaises(HarnessError) as error:
                self.api.verify(run, "over-budget")
            self.assertEqual(error.exception.code, "VERIFICATION_BUDGET_EXHAUSTED")
        self.assertEqual(self.api.resume(run)["measurement"]["status"], "not_run")

    def test_pair_cancel_replay_and_expired_owner_do_not_change_verification(self):
        run = self.start(capture_baseline=True)
        candidate = self.api.submit(run, "submit")["candidate"]
        before = copy.deepcopy(self.api.resume(run)["measurement"])
        with patch("harness_external.worker.spawn_worker", return_value=None):
            value = self.api.compare(run, "pair", expected_candidate_hash=candidate["candidate_hash"], parameters={"shared_inputs": ["probe.py"]})
            self.assertEqual(value, self.api.compare(run, "pair", expected_candidate_hash=candidate["candidate_hash"], parameters={"shared_inputs": ["probe.py"]}))
        self.assertEqual(self.api.records(run, "jobs")["total"], 1)
        self.api.cancel(run, value["job_id"], "cancel")
        execute_job(self.api.store.root, value["job_id"])
        self.assertEqual(self.api.resume(run)["measurement"], before)
        self.assertEqual(self.api.resume(run)["diagnostic"]["status"], "cancelled")
        self.assertEqual(self.api.records(run, "measurements")["total"], 0)

    def test_new_code_keeps_concrete_domain_and_adapter_out_of_core(self):
        denied = {"domain", "develop_comparison", "develop_requirements", "pytest_adapter", "process_adapter"}
        for filename in ("paired_inputs.py", "workspace_constraints.py", "operation_schema.py", "worker.py"):
            tree = ast.parse((ROOT / "src/harness_external" / filename).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    self.assertNotIn((node.module or "").split(".")[-1], denied)
                    if node.module in (None, "harness_external"):
                        self.assertFalse({item.name for item in node.names} & denied)
                if isinstance(node, ast.Import):
                    self.assertFalse({item.name.split(".")[-1] for item in node.names} & denied)

    @unittest.skipUnless(os.environ.get("BASE_HARNESS_EXTERNAL_LIVE") == "1", "real strict Sandbox")
    def test_live_pair_reuses_new_probe_but_never_satisfies_current_gates(self):
        run = self.start(capture_baseline=True)
        (self.workspace / "app.py").write_text("value = 1\n", encoding="utf-8")
        # The original probe did not detect the issue. Apply this new probe to BOTH subjects.
        (self.workspace / "probe.py").write_text("import app\nassert app.value == 1, 'wrong value'\n", encoding="utf-8")
        candidate = self.api.submit(run, "submit")["candidate"]
        before = copy.deepcopy(self.api.resume(run)["measurement"])
        with patch("harness_external.worker.spawn_worker", return_value=None):
            job = self.api.compare(run, "pair", expected_candidate_hash=candidate["candidate_hash"], parameters={"shared_inputs": ["probe.py"]})["job_id"]
        execute_job(self.api.store.root, job)
        result = self.api.status(run, job)["job"]["result"]
        self.assertEqual(result["diagnostic_comparison"]["changes"][0]["status"], "before_failed_after_passed")
        self.assertEqual(self.api.resume(run)["measurement"], before)
        self.assertEqual(self.api.resume(run)["gates"]["status"], "unsatisfied")
        with self.assertRaises(HarnessError):
            self.api.finish(run, "finish", outcome="completed")

    @unittest.skipUnless(os.environ.get("BASE_HARNESS_EXTERNAL_LIVE") == "1", "real strict Sandbox")
    def test_live_weak_probe_passes_both_subjects_without_quality_claim(self):
        run = self.start(capture_baseline=True)
        (self.workspace / "app.py").write_text("value = 1\n", encoding="utf-8")
        (self.workspace / "probe.py").write_text("import app\nassert isinstance(app.value, int)\n", encoding="utf-8")
        candidate = self.api.submit(run, "submit")["candidate"]
        with patch("harness_external.worker.spawn_worker", return_value=None):
            job = self.api.compare(run, "weak", expected_candidate_hash=candidate["candidate_hash"], parameters={"shared_inputs": ["probe.py"]})["job_id"]
        execute_job(self.api.store.root, job)
        result = self.api.status(run, job)["job"]["result"]["diagnostic_comparison"]
        self.assertEqual(result["changes"][0]["status"], "both_passed")
        self.assertFalse(result["gate_eligible"])


if __name__ == "__main__":
    unittest.main()
