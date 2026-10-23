import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/change_lens"
sys.path.insert(0, str(ROOT / "src"))

from change_lens import analyze
from change_lens.analysis import load_rules
from change_lens.graph import DependencyGraph
from change_lens.scanner import AnalysisError


class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
        return target

    def edges(self, analysis):
        return {(e["source"], e["target"]) for e in analysis.edges}

    def test_demo_facts(self):
        result = analyze(FIXTURES / "demo").report()
        self.assertEqual(result["summary"]["python_files"], 7)
        self.assertEqual(result["summary"]["local_import_edges"], 10)
        self.assertEqual(result["summary"]["test_files"], 1)
        self.assertEqual(result["diagnostics"], [])

    def test_demo_cycle(self):
        self.assertEqual(analyze(FIXTURES / "demo").report()["cycles"], [["pkg/cycle_a.py", "pkg/cycle_b.py"]])

    def test_demo_impact_with_evidence_path(self):
        result = analyze(FIXTURES / "demo").impact(["pkg/core.py"])
        self.assertEqual(result["tests"], ["tests/test_app.py"])
        self.assertEqual(result["affected"][-1]["via"], ["pkg/core.py", "pkg/service.py", "pkg/app.py", "tests/test_app.py"])
        self.assertEqual(len(result["affected"]), 4)

    def test_src_layout_and_relative_import(self):
        self.write("src/pkg/__init__.py", "")
        self.write("src/pkg/a.py", "from . import b\n")
        self.write("src/pkg/b.py", "value=1\n")
        a = analyze(self.root)
        self.assertIn(("src/pkg/a.py", "src/pkg/b.py"), self.edges(a))
        self.assertEqual(a.by_path["src/pkg/a.py"]["module"], "pkg.a")

    def test_namespace_package(self):
        self.write("pkg/a.py", "from .b import value\n")
        self.write("pkg/b.py", "value=1\n")
        self.assertIn(("pkg/a.py", "pkg/b.py"), self.edges(analyze(self.root)))

    def test_missing_submodule_is_not_hidden_by_package_initializer(self):
        self.write("pkg/__init__.py", "")
        self.write("app.py", "import pkg.missing\n")
        result = analyze(self.root)
        self.assertEqual(result.unresolved[0]["reason"], "missing_local_module")
        self.assertEqual(result.impact(["app.py"])["unresolved_import_count"], 1)
        self.assertEqual(result.impact(["app.py"])["runtime_dependency_completeness"], "not_established")

    def test_duplicate_module_does_not_guess(self):
        self.write("src/a.py", "")
        self.write("a.py", "")
        self.write("client.py", "import a\n")
        result = analyze(self.root)
        self.assertFalse(result.edges)
        self.assertTrue(any(x["code"] == "AMBIGUOUS_MODULE" for x in result.diagnostics))
        self.assertEqual(result.unresolved[0]["reason"], "ambiguous_local")

    def test_from_attribute_resolves_package_not_fictional_module(self):
        self.write("pkg/__init__.py", "value = 1\n")
        self.write("app.py", "from pkg import value\n")
        self.assertEqual(self.edges(analyze(self.root)), {("app.py", "pkg/__init__.py")})

    def test_invalid_relative_import_reported(self):
        self.write("pkg/a.py", "from ...missing import x\n")
        result = analyze(self.root)
        self.assertEqual(result.unresolved[0]["reason"], "invalid_relative_import")

    def test_dotted_import_includes_package_initializers(self):
        self.write("pkg/__init__.py", "")
        self.write("pkg/sub/__init__.py", "")
        self.write("pkg/sub/leaf.py", "")
        self.write("client.py", "import pkg.sub.leaf\n")
        self.assertEqual(self.edges(analyze(self.root)), {("client.py", p) for p in ["pkg/__init__.py", "pkg/sub/__init__.py", "pkg/sub/leaf.py"]})

    def test_dynamic_import_is_marked_not_executed(self):
        self.write("dynamic.py", "import importlib\nimportlib.import_module('missing')\n")
        result = analyze(self.root).report()
        self.assertTrue(any(d["code"] == "DYNAMIC_IMPORT_CANDIDATE" for d in result["diagnostics"]))
        self.assertFalse(result["complete_local_scan"])

    def test_import_trap_never_executes(self):
        marker = self.root / "trap.txt"
        self.write("trap.py", f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\nraise RuntimeError('trap')\n")
        self.assertEqual(analyze(self.root).report()["summary"]["parsed_files"], 1)
        self.assertFalse(marker.exists())

    def test_parse_error_keeps_other_files(self):
        self.write("good.py", "value=1\n")
        self.write("bad.py", "def broken(\n")
        result = analyze(self.root).report()
        self.assertEqual(result["summary"]["python_files"], 2)
        self.assertEqual(result["summary"]["parsed_files"], 1)
        self.assertEqual(result["diagnostics"][0]["code"], "PARSE_ERROR")

    def test_invalid_utf8_is_diagnostic(self):
        (self.root / "bad.py").write_bytes(b"\xffinvalid")
        self.write("good.py", "")
        result = analyze(self.root).report()
        self.assertEqual(result["summary"]["python_files"], 1)
        self.assertEqual(result["diagnostics"][0]["code"], "FILE_UNREADABLE")

    def test_encoding_cookie_supported(self):
        (self.root / "latin.py").write_bytes(b"# coding: latin-1\nvalue='caf\xe9'\n")
        self.assertEqual(analyze(self.root).report()["summary"]["parsed_files"], 1)

    def test_unicode_and_empty_files(self):
        self.write("한글.py", "")
        self.write("호출.py", "import 한글\n")
        self.assertEqual(self.edges(analyze(self.root)), {("호출.py", "한글.py")})

    def test_excluded_directories(self):
        for name in (".git", ".venv", "venv", "build", "dist", "__pycache__", "node_modules", "thing.egg-info"):
            self.write(name + "/bad.py", "not valid Python @")
        self.write("main.py", "")
        self.assertEqual([m["path"] for m in analyze(self.root).modules], ["main.py"])

    def test_root_must_exist_and_be_directory(self):
        path = self.write("file.py", "")
        for invalid in (path, self.root / "missing"):
            with self.assertRaises(AnalysisError):
                analyze(invalid)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_link_outside_tree_is_not_followed(self):
        self.write("ok.py", "")
        try:
            (self.root / "linked").symlink_to(ROOT, target_is_directory=True)
        except OSError:
            self.skipTest("symlink permission unavailable")
        result = analyze(self.root)
        self.assertEqual(len(result.modules), 1)
        self.assertEqual(result.diagnostics[0]["code"], "LINK_SKIPPED")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX FIFO")
    def test_special_python_file_does_not_block(self):
        os.mkfifo(self.root / "pipe.py")
        self.assertEqual(analyze(self.root).diagnostics[0]["code"], "FILE_UNREADABLE")

    def test_file_count_limit_is_visible(self):
        for i in range(3):
            self.write(f"m{i}.py", "")
        result = analyze(self.root, max_files=2).report()
        self.assertEqual(result["summary"]["python_files"], 2)
        self.assertFalse(result["complete_local_scan"])
        self.assertEqual(result["diagnostics"][0]["code"], "FILE_COUNT_LIMIT")

    def test_file_byte_limit(self):
        self.write("large.py", "value='long string'\n")
        result = analyze(self.root, max_file_bytes=4)
        self.assertFalse(result.modules)
        self.assertEqual(result.diagnostics[0]["code"], "FILE_UNREADABLE")

    def test_total_byte_limit(self):
        self.write("a.py", "a=1\n")
        self.write("b.py", "b=1\n")
        result = analyze(self.root, max_total_bytes=4)
        self.assertEqual(len(result.modules), 1)
        self.assertEqual(result.diagnostics[0]["code"], "TOTAL_BYTE_LIMIT")

    def test_entrypoint_candidates(self):
        self.write("pkg/__main__.py", "")
        self.write("main.py", "if '__main__' == __name__:\n    pass\n")
        self.assertTrue(all(m["entrypoint_candidate"] for m in analyze(self.root).modules))

    def test_rules_observe_import_site(self):
        result = analyze(FIXTURES / "demo").boundaries(load_rules(FIXTURES / "rules.json"))
        self.assertEqual(result["violations"], [{"source": "pkg/service.py", "target": "pkg/core.py", "line": 1,
                                                "source_layer": "service", "target_layer": "core"}])

    def test_ambiguous_layer_rules_rejected(self):
        self.write("a.py", "")
        with self.assertRaises(AnalysisError):
            analyze(self.root).boundaries({"layers": {"one": ["a"], "two": ["a"]}, "allow": {"one": [], "two": []}})

    def test_unknown_rule_targets_rejected(self):
        with self.assertRaises(AnalysisError):
            analyze(self.root).boundaries({"layers": {"one": ["a"]}, "allow": {"one": ["absent"]}})

    def test_duplicate_rule_keys_rejected(self):
        path = self.write("rules.json", '{"layers":{},"layers":{},"allow":{}}')
        with self.assertRaises(AnalysisError):
            load_rules(path)

    def test_unknown_changed_path_is_not_safe(self):
        self.write("a.py", "")
        report = analyze(self.root).impact(["deleted.py"])
        self.assertFalse(report["complete_local_scan"])
        self.assertEqual(report["unresolved_changes"], ["deleted.py"])

    def test_changed_path_cannot_escape_root(self):
        for path in ("../outside.py", "/outside.py"):
            with self.assertRaises(AnalysisError):
                analyze(self.root).impact([path])

    def test_no_input_changes(self):
        target = self.write("project.py", "def value(): return 42\n")
        before = (target.stat().st_mtime_ns, hashlib.sha256(target.read_bytes()).hexdigest(), list(self.root.iterdir()))
        analyze(self.root).report()
        after = (target.stat().st_mtime_ns, hashlib.sha256(target.read_bytes()).hexdigest(), list(self.root.iterdir()))
        self.assertEqual(before, after)


class GraphTests(unittest.TestCase):
    def test_deep_graph_and_cycle_without_recursion(self):
        paths = [str(i) for i in range(3000)]
        graph = DependencyGraph(paths, [(str(i), str(i + 1)) for i in range(2999)] + [("2999", "0")])
        self.assertEqual(len(graph.cycles()[0]), 3000)
        self.assertEqual(len(graph.impact(["2999"])[0]), 3000)

    def test_shortest_deterministic_impact_path(self):
        graph = DependencyGraph(["a", "b", "c", "d"], [("b", "a"), ("c", "a"), ("d", "b"), ("d", "c")])
        distance, parent = graph.impact(["a", "a"])
        self.assertEqual(distance["d"], 2)
        self.assertEqual(parent["d"], "b")

    def test_self_cycle(self):
        self.assertEqual(DependencyGraph(["a"], [("a", "a")]).cycles(), [["a"]])


class CLITests(unittest.TestCase):
    def cli(self, *args):
        return subprocess.run([sys.executable, "-B", "-m", "change_lens", *map(str, args)], cwd=ROOT,
                              capture_output=True, text=True, encoding="utf-8", timeout=15, env={**os.environ, "PYTHONPATH": str(ROOT / "src")})

    def test_json_stable(self):
        first = self.cli("inspect", FIXTURES / "demo", "--format", "json")
        second = self.cli("inspect", FIXTURES / "demo", "--format", "json")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stdout, second.stdout)
        self.assertEqual(json.loads(first.stdout)["summary"]["python_files"], 7)

    def test_invalid_path_has_structured_error(self):
        result = self.cli("inspect", FIXTURES / "missing")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stderr)["error"], "ANALYSIS_INPUT")
        self.assertNotIn("Traceback", result.stderr)

    def test_context_discloses_omissions(self):
        result = self.cli("context", FIXTURES / "demo", "--changed", "pkg/core.py", "--limit", 2, "--format", "json")
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value["affected_count"], 4)
        self.assertEqual(value["omitted_files"], 2)
        self.assertEqual(len(value["selected"]), 2)

    def test_mermaid(self):
        result = self.cli("inspect", FIXTURES / "demo", "--format", "mermaid")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith("flowchart LR\n"))
        self.assertIn(" --> ", result.stdout)

    def test_invalid_limit(self):
        result = self.cli("context", FIXTURES / "demo", "--changed", "pkg/core.py", "--limit", 0)
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
