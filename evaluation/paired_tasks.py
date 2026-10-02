"""Frozen benign software tasks for the opt-in agent comparison, not Core policy.

Expected observations stay in the evaluator. The subject driver receives only
operation/input data and runs exclusively in the strict verification Sandbox.
"""

import copy


SETTINGS_SPEC = """Repair settings.py and settings_cli.py using only Python's standard library.
Keep normalize(document), convert_file(source, destination), and main(argv=None).

normalize accepts a JSON-compatible dict with integer version 1 or 2 (not bool).
Version 1: timeout_ms defaults to 1000 and must be a nonnegative integer, retries
defaults to 0 and must be a nonnegative integer (neither accepts bool). labels
defaults to {} and maps strings to strings. Other top-level keys are copied into
extensions. Convert to {version:2, timeout_seconds:timeout_ms/1000,
attempts:retries+1, labels:labels, extensions:unknown_fields}.
Version 2: only version, timeout_seconds, attempts, labels, extensions are allowed.
Defaults are 1.0 seconds, 1 attempt, {}, {}. Seconds must be finite/nonnegative int
or float, not bool. Attempts must be a positive integer, not bool. extensions must
be a dict of JSON-compatible data. Reject unsupported versions, invalid types,
non-string object keys and nonfinite numbers anywhere with ValueError. Do not
mutate input; no mutable objects may be shared between input and result. A second
normalization of a canonical version-2 result must produce the same value.

convert_file reads UTF-8 JSON (reject duplicate keys), normalizes it and atomically
replaces destination with the resulting JSON, returning that dict. Parent directory
must already exist. Reject source and destination resolving to the same path with
ValueError. Never modify source. Validation/read failure must preserve any existing
destination. Do not leave temporary files on success or failure.
The CLI `python3 settings_cli.py SOURCE DEST` prints the result JSON and exits 0;
invalid data or I/O error prints a JSON object with an error string to stdout and
exits 2, without a traceback. main accepts an explicit argument list.
Add useful tests and usage notes; preserve these public interfaces."""

JOURNAL_SPEC = """Repair journal.py and journal_cli.py using Python standard library on POSIX.
Keep Journal(path), Journal.read_all(), Journal.append(key, value), main(argv=None).

The journal is UTF-8 NDJSON. Each newline-terminated record is an object with exactly
key (nonempty string) and value (finite JSON-compatible data, string object keys).
Reject duplicate JSON keys, invalid full records or conflicting repeated keys with
ValueError. Ignore blank lines. Identical repeated records collapse to the first
occurrence, ordered by first occurrence. Equality means the same canonical JSON
(sorted keys, compact separators, ensure_ascii=False, allow_nan=False), so true
and 1, or 1 and 1.0, are different values.
read_all returns [{key:..., value:...}, ...]. A missing journal reads as []; reading
does not create or change files. Ignore a final non-newline-terminated fragment,
even if that fragment alone is valid JSON: it is an uncommitted record.

append validates arguments before touching the file. Return True for a new key,
False for an identical committed value; a different value for an existing key is
a ValueError with no file changes. A new successful append discards an uncommitted
tail, appends a complete newline-terminated JSON record and flushes it durably.
Duplicate/no-op and invalid operations must leave bytes unchanged. Corrupt complete
records must not be truncated, overwritten or silently ignored. Multiple independent
processes appending to the same file must serialize validation and writing so no
records are lost and duplicate-key idempotency is preserved. Parent exists; do not
replace the inode while holding a lock. Fresh Journal instances see committed data.

CLI: `python3 journal_cli.py put FILE KEY JSON_VALUE` prints {created:bool};
`python3 journal_cli.py list FILE` prints the record list. Successful commands exit
0. Invalid data or I/O errors print {error:string} to stdout and exit 2 without a
traceback. main accepts an explicit argument list. Add tests and usage notes."""

PLANNER_SPEC = """Repair planner.py and planner_cli.py using only Python standard library.
Keep plan(tasks, completed=None), impact(tasks, changed), and main(argv=None).

tasks is a list of dicts with a unique nonempty string id and optional requires
(default []). No other keys are allowed. requires is a list of unique nonempty
string task IDs. Unknown dependencies, self dependencies, duplicate IDs, wrong
types and cycles raise ValueError. Validate the ENTIRE graph even when some or
all tasks are marked completed. Do not mutate inputs.
completed is None or a list of unique declared IDs. plan returns deterministic
topological waves of unfinished tasks: each wave contains ALL currently ready
unfinished IDs sorted lexicographically; completed tasks have already satisfied
dependencies and must not appear. Empty tasks or all completed yields [].
impact takes a list of unique declared changed IDs and returns a sorted list of
those IDs plus all transitive dependents; validate the same graph and input rules.

CLI: `python3 planner_cli.py FILE [--completed ID,ID] [--changed ID,ID]` reads a UTF-8
JSON task list and emits {waves:[...], affected:[...]}; omitted/empty flags mean [].
Success exits 0. Invalid graph, JSON, duplicate JSON keys, or I/O error emits a JSON
object with an error string to stdout and exits 2, without a traceback. Do not
modify input files; main accepts an explicit argument list. Add tests and notes."""


def normal(version=2, seconds=1.0, attempts=1, labels=None, extensions=None):
    return {"version": version, "timeout_seconds": seconds, "attempts": attempts,
            "labels": labels or {}, "extensions": extensions or {}}


def case(name, operation, data, expected):
    return {"name": name, "request": {"operation": operation, **data}, "expected": expected}


SETTINGS_CASES = [
    case("v1-defaults", "normalize", {"document": {"version": 1}}, {"value": normal(), "unchanged": True, "detached": True}),
    case("v1-conversion", "normalize", {"document": {"version": 1, "timeout_ms": 2500, "retries": 2, "labels": {"env": "dev"}, "nested": {"a": [1, True, None]}}},
         {"value": normal(seconds=2.5, attempts=3, labels={"env": "dev"}, extensions={"nested": {"a": [1, True, None]}}), "unchanged": True, "detached": True}),
    case("v2-defaults", "normalize", {"document": {"version": 2}}, {"value": normal(), "unchanged": True, "detached": True}),
    case("v2-detached-idempotent", "normalize", {"document": normal(seconds=0, attempts=2, labels={"x": "y"}, extensions={"list": [{"a": 1}]})},
         {"value": normal(seconds=0, attempts=2, labels={"x": "y"}, extensions={"list": [{"a": 1}]}), "unchanged": True, "detached": True}),
]
for name, value in [
    ("version-bool", {"version": True}), ("version-unsupported", {"version": 9}),
    ("timeout-bool", {"version": 1, "timeout_ms": False}), ("negative-timeout", {"version": 1, "timeout_ms": -1}),
    ("fractional-ms", {"version": 1, "timeout_ms": 1.2}), ("attempts-zero", {"version": 2, "attempts": 0}),
    ("attempts-bool", {"version": 2, "attempts": True}), ("labels-not-strings", {"version": 1, "labels": {"x": 1}}),
    ("unknown-v2-key", {"version": 2, "extra": 1}), ("extensions-array", {"version": 2, "extensions": []}),
    ("document-not-object", []),
]:
    SETTINGS_CASES.append(case(name, "normalize", {"document": value}, {"error": "ValueError", "unchanged": True}))
SETTINGS_CASES += [
    case("recursive-nonfinite", "normalize_nonfinite", {}, {"error": "ValueError", "unchanged": True}),
    case("nonstring-key", "normalize_nonstring", {}, {"error": "ValueError", "unchanged": True}),
    case("file-conversion", "settings_file", {"raw": '{"version":1,"timeout_ms":0}'},
         {"value": normal(seconds=0.0), "destination": normal(seconds=0.0), "source_unchanged": True, "leftovers": []}),
    case("invalid-file-preserves-output", "settings_file", {"raw": '{"version":3}'},
         {"error": "ValueError", "destination": {"sentinel": True}, "source_unchanged": True, "leftovers": []}),
    case("duplicate-file-key", "settings_file", {"raw": '{"version":1,"version":2}'},
         {"error": "ValueError", "destination": {"sentinel": True}, "source_unchanged": True, "leftovers": []}),
    case("same-path-refused", "settings_same_path", {}, {"error": "ValueError", "unchanged": True}),
    case("settings-cli", "settings_cli", {"raw": '{"version":1,"retries":4}'},
         {"exit": 0, "output": normal(attempts=5), "source_unchanged": True, "traceback": False}),
    case("settings-cli-error", "settings_cli", {"raw": 'bad json'},
         {"exit": 2, "output": {"error_string": True}, "source_unchanged": True, "traceback": False}),
]

GOOD_ROW = '{"key":"a","value":1}\n'
JOURNAL_CASES = [
    case("missing-read-no-create", "journal", {"actions": [{"read": True}]}, {"results": [[]], "exists": False, "last_unchanged": True}),
    case("append-reopen", "journal", {"actions": [{"key": "a", "value": {"x": [1, False]}}, {"read": True}]},
         {"results": [True, [{"key": "a", "value": {"x": [1, False]}}]], "exists": True, "last_unchanged": True}),
    case("duplicate-noop", "journal", {"initial": GOOD_ROW, "actions": [{"key": "a", "value": 1}]},
         {"results": [False], "exists": True, "last_unchanged": True}),
    case("conflict-keeps-bytes", "journal", {"initial": GOOD_ROW, "actions": [{"key": "a", "value": 2}]},
         {"results": [{"error": "ValueError"}], "exists": True, "last_unchanged": True}),
    case("bool-is-not-int", "journal", {"initial": GOOD_ROW, "actions": [{"key": "a", "value": True}]},
         {"results": [{"error": "ValueError"}], "exists": True, "last_unchanged": True}),
    case("float-is-not-int", "journal", {"initial": GOOD_ROW, "actions": [{"key": "a", "value": 1.0}]},
         {"results": [{"error": "ValueError"}], "exists": True, "last_unchanged": True}),
    case("tail-ignored-readonly", "journal", {"initial": GOOD_ROW + '{"key":', "actions": [{"read": True}]},
         {"results": [[{"key": "a", "value": 1}]], "exists": True, "last_unchanged": True}),
    case("tail-repair-on-new-append", "journal", {"initial": GOOD_ROW + '{"key":', "actions": [{"key": "b", "value": "한글"}, {"read": True}]},
         {"results": [True, [{"key": "a", "value": 1}, {"key": "b", "value": "한글"}]], "exists": True, "last_unchanged": True}),
    case("valid-uncommitted-tail", "journal", {"initial": '{"key":"lost","value":9}', "actions": [{"key": "new", "value": None}, {"read": True}]},
         {"results": [True, [{"key": "new", "value": None}]], "exists": True, "last_unchanged": True}),
    case("noop-preserves-tail", "journal", {"initial": GOOD_ROW + 'unfinished', "actions": [{"key": "a", "value": 1}]},
         {"results": [False], "exists": True, "last_unchanged": True}),
    case("canonical-object-noop", "journal", {"initial": '{"key":"a","value":{"z":2,"a":1}}\n', "actions": [{"key": "a", "value": {"a": 1, "z": 2}}]},
         {"results": [False], "exists": True, "last_unchanged": True}),
    case("committed-identical-duplicates", "journal", {"initial": GOOD_ROW + '\n' + GOOD_ROW, "actions": [{"read": True}]},
         {"results": [[{"key": "a", "value": 1}]], "exists": True, "last_unchanged": True}),
]
for name, raw in [("corrupt-full-line", "broken\n"), ("duplicate-json-key", '{"key":"a","key":"b","value":1}\n'),
                  ("missing-field", '{"key":"a"}\n'), ("conflicting-committed", GOOD_ROW + '{"key":"a","value":2}\n')]:
    JOURNAL_CASES.append(case(name, "journal", {"initial": raw, "actions": [{"key": "z", "value": 1}]},
                              {"results": [{"error": "ValueError"}], "exists": True, "last_unchanged": True}))
JOURNAL_CASES += [
    case("invalid-key-no-create", "journal", {"actions": [{"key": "", "value": 1}]},
         {"results": [{"error": "ValueError"}], "exists": False, "last_unchanged": True}),
    case("concurrent-distinct", "journal_concurrent", {"duplicate": False}, {"exits": [0] * 6, "count": 6, "keys": ["k" + str(i) for i in range(6)]}),
    case("concurrent-identical", "journal_concurrent", {"duplicate": True}, {"exits": [0] * 6, "count": 1, "keys": ["same"]}),
    case("journal-cli-roundtrip", "journal_cli", {}, {"exits": [0, 0], "outputs": [{"created": True}, [{"key": "x", "value": [1, 2]}]], "traceback": False}),
]

GRAPH = [{"id": "d", "requires": ["a", "b"]}, {"id": "c", "requires": ["a"]}, {"id": "b"}, {"id": "a"}]
PLANNER_CASES = [
    case("all-ready-wave", "plan", {"tasks": GRAPH}, {"value": [["a", "b"], ["c", "d"]], "unchanged": True}),
    case("completed-unblocks", "plan", {"tasks": GRAPH, "completed": ["a"]}, {"value": [["b", "c"], ["d"]], "unchanged": True}),
    case("all-completed", "plan", {"tasks": GRAPH, "completed": ["a", "b", "c", "d"]}, {"value": [], "unchanged": True}),
    case("empty-plan", "plan", {"tasks": []}, {"value": [], "unchanged": True}),
    case("impact-transitive", "impact", {"tasks": GRAPH, "changed": ["a"]}, {"value": ["a", "c", "d"], "unchanged": True}),
    case("impact-two-roots", "impact", {"tasks": GRAPH, "changed": ["c", "b"]}, {"value": ["b", "c", "d"], "unchanged": True}),
    case("impact-empty", "impact", {"tasks": GRAPH, "changed": []}, {"value": [], "unchanged": True}),
]
for name, tasks, complete in [
    ("cycle", [{"id": "a", "requires": ["b"]}, {"id": "b", "requires": ["a"]}], []),
    ("completed-cycle-invalid", [{"id": "a", "requires": ["b"]}, {"id": "b", "requires": ["a"]}], ["a", "b"]),
    ("unknown-dependency", [{"id": "a", "requires": ["missing"]}], []),
    ("self-dependency", [{"id": "a", "requires": ["a"]}], []),
    ("duplicate-id", [{"id": "a"}, {"id": "a"}], []),
    ("duplicate-edge", [{"id": "a"}, {"id": "b", "requires": ["a", "a"]}], []),
    ("requires-string", [{"id": "a", "requires": "b"}], []),
    ("empty-id", [{"id": ""}], []), ("bool-id", [{"id": True}], []),
    ("unknown-field", [{"id": "a", "weight": 1}], []),
    ("unknown-completed", GRAPH, ["other"]), ("duplicate-completed", GRAPH, ["a", "a"]),
]:
    PLANNER_CASES.append(case(name, "plan", {"tasks": tasks, "completed": complete}, {"error": "ValueError", "unchanged": True}))
PLANNER_CASES += [
    case("unknown-changed", "impact", {"tasks": GRAPH, "changed": ["other"]}, {"error": "ValueError", "unchanged": True}),
    case("planner-cli", "planner_cli", {"raw": __import__('json').dumps(GRAPH), "completed": "a", "changed": "a"},
         {"exit": 0, "output": {"waves": [["b", "c"], ["d"]], "affected": ["a", "c", "d"]}, "unchanged": True, "traceback": False}),
    case("planner-cli-duplicate-key", "planner_cli", {"raw": '[{"id":"a","id":"b"}]'},
         {"exit": 2, "output": {"error_string": True}, "unchanged": True, "traceback": False}),
]

PUBLIC = {
    "settings": '''import unittest\nimport settings\nclass Acceptance(unittest.TestCase):\n    def test_defaults(self):\n        self.assertEqual(settings.normalize({"version":1}), {"version":2,"timeout_seconds":1.0,"attempts":1,"labels":{},"extensions":{}})\n    def test_invalid(self):\n        with self.assertRaises(ValueError): settings.normalize({"version":True})\n    def test_extensions(self):\n        original={"version":1,"extra":{"items":[1]}}\n        result=settings.normalize(original)\n        result["extensions"]["extra"]["items"].append(2)\n        self.assertEqual(original["extra"]["items"],[1])\n''',
    "journal": '''import tempfile\nfrom pathlib import Path\nimport unittest\nfrom journal import Journal\nclass Acceptance(unittest.TestCase):\n    def test_roundtrip(self):\n        with tempfile.TemporaryDirectory() as d:\n            j=Journal(Path(d)/"log")\n            self.assertTrue(j.append("a",1))\n            self.assertFalse(j.append("a",1))\n            self.assertEqual(j.read_all(),[{"key":"a","value":1}])\n    def test_recovery(self):\n        with tempfile.TemporaryDirectory() as d:\n            p=Path(d)/"log"\n            p.write_text('{"key":"a","value":1}\\nunfinished')\n            j=Journal(p)\n            self.assertEqual(j.read_all(),[{"key":"a","value":1}])\n            self.assertTrue(j.append("b",2))\n            self.assertEqual(len(Journal(p).read_all()),2)\n''',
    "planner": '''import unittest\nfrom planner import plan,impact\nclass Acceptance(unittest.TestCase):\n    def test_waves(self):\n        tasks=[{"id":"b","requires":["a"]},{"id":"c"},{"id":"a"}]\n        self.assertEqual(plan(tasks),[["a","c"],["b"]])\n        self.assertEqual(impact(tasks,["a"]),["a","b"])\n    def test_cycle(self):\n        with self.assertRaises(ValueError): plan([{"id":"a","requires":["a"]}])\n''',
}

PUBLIC["settings"] += '''    def test_cli(self):\n        import json,subprocess,sys,tempfile\n        from pathlib import Path\n        with tempfile.TemporaryDirectory() as d:\n            source,target=Path(d)/"in.json",Path(d)/"out.json"\n            source.write_text('{"version":1}')\n            result=subprocess.run([sys.executable,"settings_cli.py",str(source),str(target)],capture_output=True,text=True,timeout=5)\n            self.assertEqual(result.returncode,0,result.stderr)\n            self.assertEqual(json.loads(result.stdout)["version"],2)\n            self.assertEqual(json.loads(target.read_text())["attempts"],1)\n'''
PUBLIC["journal"] += '''    def test_cli(self):\n        import json,subprocess,sys\n        with tempfile.TemporaryDirectory() as d:\n            path=str(Path(d)/"log")\n            result=subprocess.run([sys.executable,"journal_cli.py","put",path,"x","3"],capture_output=True,text=True,timeout=5)\n            self.assertEqual(result.returncode,0,result.stderr)\n            self.assertEqual(json.loads(result.stdout),{"created":True})\n'''
PUBLIC["planner"] += '''    def test_cli(self):\n        import json,subprocess,sys,tempfile\n        from pathlib import Path\n        with tempfile.TemporaryDirectory() as d:\n            path=Path(d)/"tasks.json"\n            path.write_text('[{"id":"a"}]')\n            result=subprocess.run([sys.executable,"planner_cli.py",str(path)],capture_output=True,text=True,timeout=5)\n            self.assertEqual(result.returncode,0,result.stderr)\n            self.assertEqual(json.loads(result.stdout),{"waves":[["a"]],"affected":[]})\n'''

SOURCES = {
    "settings": {"settings.py": "def normalize(document):\n    return dict(document)\n",
                 "settings_cli.py": "def convert_file(source, destination):\n    raise NotImplementedError()\n\ndef main(argv=None):\n    raise NotImplementedError()\n\nif __name__ == '__main__':\n    raise SystemExit(main())\n"},
    "journal": {"journal.py": "class Journal:\n    def __init__(self,path): self.path=path\n    def read_all(self): return []\n    def append(self,key,value): return True\n",
                "journal_cli.py": "def main(argv=None):\n    raise NotImplementedError()\n\nif __name__ == '__main__':\n    raise SystemExit(main())\n"},
    "planner": {"planner.py": "def plan(tasks, completed=None):\n    return [[t['id'] for t in tasks]]\n\ndef impact(tasks, changed):\n    return sorted(changed)\n",
                "planner_cli.py": "def main(argv=None):\n    raise NotImplementedError()\n\nif __name__ == '__main__':\n    raise SystemExit(main())\n"},
}

TASKS = [{"id": name, "spec": spec, "sources": copy.deepcopy(SOURCES[name]), "acceptance_test": PUBLIC[name], "cases": cases}
         for name, spec, cases in (("settings", SETTINGS_SPEC, SETTINGS_CASES), ("journal", JOURNAL_SPEC, JOURNAL_CASES), ("planner", PLANNER_SPEC, PLANNER_CASES))]
