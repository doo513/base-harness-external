from pathlib import Path
import subprocess
import sys

from harness_external.identity import verifier_identity
from harness_external.registry import DomainRegistry


class Parent:
    domain_id = "probe"
    revision = "1"
    threshold = 1

    def prepare(self, *args, **kwargs):
        return self.threshold

    def normalize_check(self, *args, **kwargs):
        return {}


def test_inherited_configuration_and_effective_overrides(monkeypatch):
    class Child(Parent):
        pass
    module = Child()
    registry = DomainRegistry([module])
    initial = registry.identity("probe")
    monkeypatch.setattr(Parent, "threshold", 2)
    assert registry.identity("probe") != initial
    module.threshold = 3
    fixed = registry.identity("probe")
    monkeypatch.setattr(Parent, "threshold", 4)
    assert registry.identity("probe") == fixed
    module.threshold = 5
    assert registry.identity("probe") != fixed


def test_mro_and_explicit_config_without_evaluating_properties(monkeypatch):
    class Other:
        threshold = 9
    class Child(Parent, Other):
        @property
        def ignored(self):
            raise AssertionError("Do not execute descriptors")
    registry = DomainRegistry([Child()])
    before = registry.identity("probe")
    monkeypatch.setattr(Other, "threshold", 10)
    assert registry.identity("probe") == before
    module = Child()
    module.identity_config = {"chosen": 1}
    registry = DomainRegistry([module])
    before = registry.identity("probe")
    monkeypatch.setattr(Parent, "threshold", 20)
    assert registry.identity("probe") == before
    module.identity_config["chosen"] = 2
    assert registry.identity("probe") != before


def test_presentation_is_not_execution_identity(monkeypatch):
    original = Path.read_bytes
    before = verifier_identity()
    def changed(path):
        data = original(path)
        return data + b"\n# view-only change\n" if path.name == "queries.py" else data
    monkeypatch.setattr(Path, "read_bytes", changed)
    assert verifier_identity() == before
    def measurement_change(path):
        data = original(path)
        return data + b"\n# execution boundary change\n" if path.name == "worker.py" else data
    monkeypatch.setattr(Path, "read_bytes", measurement_change)
    assert verifier_identity() != before


def test_cli_and_service_queries_do_not_import_the_execution_engine():
    root = Path(__file__).resolve().parents[1]
    code = "import sys; sys.path.insert(0,sys.argv[1]); import harness_external.__main__; from harness_external.service import Harness; assert 'harness.measurement_v5' not in sys.modules; assert 'harness_external.worker' not in sys.modules"
    result = subprocess.run([sys.executable, "-I", "-c", code, str(root / "src")], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
