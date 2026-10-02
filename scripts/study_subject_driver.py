"""Evaluation subprocess driver. Run only inside the strict Sandbox.

This file receives operations and inputs, never the evaluator's expected answers.
It measures candidate behavior; it cannot declare a task or Harness Run passed.
"""
import copy
import importlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile


def ids(value):
    result = set()
    if isinstance(value, (dict, list)):
        result.add(id(value))
        for child in value.values() if isinstance(value, dict) else value:
            result.update(ids(child))
    return result


def error_output(value):
    if isinstance(value, dict) and "error" in value:
        return {"error_string": isinstance(value["error"], str)}
    return value


def error_kind(error):
    # Contracts saying "raises ValueError" allow its subclasses, including
    # JSONDecodeError and UnicodeDecodeError. Do not reject valid implementations
    # solely because they expose a more specific built-in exception.
    return "ValueError" if isinstance(error, ValueError) else type(error).__name__


def cli(module, args):
    p = subprocess.run([sys.executable, module + ".py", *args], capture_output=True, text=True, timeout=8)
    try:
        output = error_output(json.loads(p.stdout))
    except ValueError:
        output = {"invalid_json": p.stdout[:300]}
    return p.returncode, output, "Traceback" in p.stdout + p.stderr


def observe(request):
    operation = request["operation"]
    if operation.startswith("normalize"):
        from settings import normalize
        document = request.get("document", {"version": 1, "nested": {"bad": float("inf")}} if operation == "normalize_nonfinite" else {"version": 1, 1: "bad"})
        saved = copy.deepcopy(document)
        try:
            value = normalize(document)
            return {"value": value, "unchanged": saved == document, "detached": not bool(ids(document) & ids(value))}
        except Exception as error:
            return {"error": error_kind(error), "unchanged": saved == document}
    if operation.startswith("settings_"):
        from settings_cli import convert_file
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root / "source.json", root / "output.json"
            raw = request.get("raw", '{"version":1}').encode()
            source.write_bytes(raw)
            target.write_text('{"sentinel":true}')
            if operation == "settings_same_path":
                try:
                    convert_file(source, source)
                    result = {"value": None}
                except Exception as error:
                    result = {"error": error_kind(error)}
                return {**result, "unchanged": source.read_bytes() == raw}
            if operation == "settings_cli":
                code, output, traceback = cli("settings_cli", [str(source), str(target)])
                return {"exit": code, "output": output, "source_unchanged": source.read_bytes() == raw, "traceback": traceback}
            try:
                result = {"value": convert_file(source, target)}
            except Exception as error:
                result = {"error": error_kind(error)}
            try:
                destination = json.loads(target.read_text())
            except Exception:
                destination = {"unreadable_output": True}
            return {**result, "destination": destination, "source_unchanged": source.read_bytes() == raw,
                    "leftovers": sorted(p.name for p in root.iterdir() if p not in (source, target))}
    if operation.startswith("journal"):
        from journal import Journal
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.ndjson"
            if operation == "journal_cli":
                first = cli("journal_cli", ["put", str(path), "x", "[1,2]"])
                second = cli("journal_cli", ["list", str(path)])
                return {"exits": [first[0], second[0]], "outputs": [first[1], second[1]], "traceback": first[2] or second[2]}
            if operation == "journal_concurrent":
                code = "import sys; from journal import Journal; Journal(sys.argv[1]).append(sys.argv[2],1)"
                processes = [subprocess.Popen([sys.executable, "-c", code, str(path), "same" if request["duplicate"] else "k" + str(i)],
                                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for i in range(6)]
                exits = [p.wait(timeout=8) for p in processes]
                rows = Journal(path).read_all()
                return {"exits": exits, "count": len(rows), "keys": sorted(row["key"] for row in rows)}
            if "initial" in request:
                path.write_text(request["initial"])
            results, unchanged = [], True
            for action in request["actions"]:
                before = path.read_bytes() if path.exists() else None
                try:
                    journal = Journal(path)
                    value = journal.read_all() if action.get("read") else journal.append(action["key"], action["value"])
                    results.append(value)
                except Exception as error:
                    results.append({"error": error_kind(error)})
                unchanged = before == (path.read_bytes() if path.exists() else None)
            return {"results": results, "exists": path.exists(), "last_unchanged": unchanged}
    if operation in {"plan", "impact"}:
        module = importlib.import_module("planner")
        tasks = request["tasks"]
        original = copy.deepcopy(tasks)
        try:
            value = module.plan(tasks, request.get("completed")) if operation == "plan" else module.impact(tasks, request["changed"])
            return {"value": value, "unchanged": tasks == original}
        except Exception as error:
            return {"error": error_kind(error), "unchanged": tasks == original}
    if operation == "planner_cli":
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.json"
            raw = request["raw"].encode()
            path.write_bytes(raw)
            code, value, traceback = cli("planner_cli", [str(path), "--completed", request.get("completed", ""), "--changed", request.get("changed", "")])
            return {"exit": code, "output": value, "unchanged": path.read_bytes() == raw, "traceback": traceback}
    raise ValueError("Unknown evaluation operation")


if __name__ == "__main__":
    requests = json.loads(Path(sys.argv[1]).read_text())
    observed = []
    for request in requests:
        try:
            value = observe(request)
            json.dumps(value, allow_nan=False)
            observed.append(value)
        except Exception as error:
            observed.append({"driver_exception": type(error).__name__, "message": str(error)[:200]})
    print("__STUDY_OBSERVATIONS__" + json.dumps(observed, ensure_ascii=False, allow_nan=False))
