"""Acceptance tests for Check provenance, bounded resume reads and portable Domain identity."""
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SOURCE))

from harness_external.domain import DevelopModule
from harness_external.registry import DomainRegistry
from harness_external.service import Harness


class DomainQualityFixes(unittest.TestCase):
    def start_structural_run(self, root):
        workspace = root / "workspace"
        workspace.mkdir()
        (workspace / "main.py").write_text("def add(a, b): return a + b\n")
        api = Harness(root / "state")
        run_id = api.start(domain_id="develop", goal="Implement addition", workspace=str(workspace),
                           parameters={"profile": "structural", "inputs": ["main.py"], "artifacts": ["main.py"],
                                       "expectations": [{"id": "entry", "path": "main.py", "operator": "contains", "expected": "def add"}]},
                           mode="exploratory", request_id="start")["run_id"]
        return api, run_id

    def test_individual_check_retains_unverified_approval_reference(self):
        with tempfile.TemporaryDirectory() as temporary:
            api, run_id = self.start_structural_run(Path(temporary))
            response = api.register_check(run_id, {
                "check_id": "review-note",
                "interpretation_revision": 1,
                "parameters": {"kind": "file", "path": "main.py", "operator": "contains", "expected": "def add"},
                "provenance": {"declared_author": "user", "approval_reference": "review-42"},
            }, "check")
        claim = response["check"]["approval_claim"]
        self.assertEqual(claim["status"], "unverified")
        self.assertEqual(claim["claimed_reference"], "review-42")
        self.assertIsNone(claim["authenticated_by"])

    def test_resume_loads_only_the_five_recent_jobs(self):
        with tempfile.TemporaryDirectory() as temporary:
            api, run_id = self.start_structural_run(Path(temporary))
            with api.store.transaction() as connection:
                for index in range(20):
                    api.store.save_job(connection, {"job_id": "job_" + format(index, "032x"), "run_id": run_id,
                                                    "status": "interrupted", "result": None})
            original = api.store.job
            visited = []

                def counted(connection, job_id):
                visited.append(job_id)
                return original(connection, job_id)

            api.store.job = counted
            summary = api.resume(run_id)
        self.assertEqual(len(visited), 5)
        self.assertEqual(summary["history_counts"]["jobs"], 20)
        self.assertEqual([job["job_id"] for job in summary["recent_jobs"]],
                         ["job_" + format(index, "032x") for index in range(19, 14, -1)])

    def test_domain_identity_is_independent_of_install_location(self):
        source = """from pathlib import Path
class ExampleDomain:
    domain_id = 'portable-example'
    revision = '1'
    identity_files = (str(Path(__file__).with_name('domain-rules.json')),)
    def prepare(self, goal, parameters, verifier, *, exploratory=False, intent=None): return {}
    def normalize_check(self, parameters, contract): return parameters
"""
        identities = []
        with tempfile.TemporaryDirectory() as temporary:
            for name in ("install-one", "install-two"):
                directory = Path(temporary) / name
                directory.mkdir()
                (directory / "domain_plugin.py").write_text(source)
                (directory / "domain-rules.json").write_text('{"same": true}\n')
                spec = importlib.util.spec_from_file_location("portable_domain_plugin", directory / "domain_plugin.py")
                module = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = module
                spec.loader.exec_module(module)
                identities.append(DomainRegistry([module.ExampleDomain()]).identity("portable-example"))
        self.assertEqual(identities[0], identities[1])


if __name__ == "__main__":
    unittest.main()
