"""Sandbox subject driver: receives operations, never expected answers."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile


def error_name(error):
    return "ValueError" if isinstance(error, ValueError) else type(error).__name__


def run(request):
    from leasequeue import Queue
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "queue.sqlite"
        saved, tokens = {}, {}
        def resolve(value):
            if isinstance(value, dict) and set(value) == {"$token"}:
                return saved[value["$token"]]["token"]
            if isinstance(value, dict):
                return {key: resolve(item) for key, item in value.items()}
            if isinstance(value, list):
                return [resolve(item) for item in value]
            return value
        def clean(value):
            if isinstance(value, dict):
                result = {}
                for key, item in value.items():
                    if key == "token" and item is not None:
                        if not isinstance(item, str) or not item.strip():
                            result[key] = {"invalid_token": True}
                        else:
                            result[key] = tokens.setdefault(item, "token-" + str(len(tokens)+1))
                    else:
                        result[key] = clean(item)
                return result
            if isinstance(value, list):
                return [clean(item) for item in value]
            return value
        def execute(action, cli=False):
            args = resolve(action.get("args", {}))
            special = action.get("special")
            if special:
                args["payload"] = {"nan": float("nan")} if special == "nan" else {1: "bad"} if special == "key" else (1, 2)
            if cli:
                raw = action.get("raw", json.dumps({"op": action["op"], "args": args}))
                process = subprocess.run([sys.executable, "leasequeue_cli.py", str(path), raw], capture_output=True, text=True, timeout=8)
                try:
                    value = json.loads(process.stdout)
                    if value.get("ok") is False:
                        value = {**value, "error": bool(isinstance(value.get("error"), str) and value["error"])}
                    if action.get("save"):
                        saved[action["save"]] = value["value"]
                except Exception:
                    value = {"invalid_json_output": True}
                return {"exit": process.returncode, "output": clean(value), "traceback": "Traceback" in process.stderr}
            try:
                value = getattr(Queue(path), action["op"])(**args)
                if action.get("save"):
                    saved[action["save"]] = value
                observed = clean(value)
                if action.get("mutate_return") and isinstance(value, dict):
                    value["payload"]["items"].append("caller-edit")
                if action.get("mutate_argument"):
                    args["payload"]["items"].append("caller-edit")
                return observed
            except Exception as error:
                return {"error": error_name(error)}
        if request["operation"] in ("sequence", "cli"):
            values = [execute(action, request["operation"] == "cli") for action in request["actions"]]
            try:
                events = Queue(path).events()
            except Exception as error:
                events = {"error": error_name(error)}
            return {"outputs": values, "events": events}
        kind = request["kind"]
        queue = Queue(path)
        if kind in ("claims", "replay", "finish"):
            for index in range(6 if kind == "claims" else 1):
                queue.add("j"+str(index), index, request_id="setup"+str(index))
        initial = queue.claim("w", 0, 10, request_id="setup-claim") if kind == "finish" else None
        calls = []
        for index in range(9 if kind == "claims" else 6):
            if kind == "adds":
                op, args = "add", dict(job_id="j"+str(index), payload=index, request_id="add"+str(index))
            elif kind == "add-replay":
                op, args = "add", dict(job_id="j0", payload=1, request_id="same-add")
            elif kind == "finish":
                op, args = "finish", dict(job_id="j0", token=initial["token"], now=1, success=True, request_id="finish"+str(index))
            else:
                op, args = "claim", dict(worker="w", now=0, lease_seconds=10, request_id="same" if kind == "replay" else "claim"+str(index))
            code = "import json,sys;from leasequeue import Queue;print(json.dumps(getattr(Queue(sys.argv[1]),sys.argv[2])(**json.loads(sys.argv[3]))))"
            calls.append(subprocess.Popen([sys.executable, "-c", code, str(path), op, json.dumps(args)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
        values, exits = [], []
        for process in calls:
            try:
                stdout, _ = process.communicate(timeout=20)
                values.append(json.loads(stdout))
            except Exception:
                process.kill(); process.communicate()
                values.append({"process_error": True})
            exits.append(process.returncode)
        events = Queue(path).events()
        result = {"exits": sorted(exits), "event_count": len(events), "contiguous_events": [e["seq"] for e in events] == list(range(1, len(events)+1))}
        if kind in ("claims", "replay"):
            jobs = [v for v in values if isinstance(v, dict) and "id" in v]
            result.update(ids=sorted(v["id"] for v in jobs), none_count=sum(v is None for v in values),
                          unique_tokens=len({v["token"] for v in jobs}), attempts=sorted(v["attempts"] for v in jobs))
        else:
            result.update(true_count=sum(v is True for v in values), false_count=sum(v is False for v in values))
        return result


if __name__ == "__main__":
    results = []
    for request in json.loads(Path(sys.argv[1]).read_text()):
        try:
            value = run(request)
            json.dumps(value, allow_nan=False)
        except Exception as error:
            value = {"driver_error": error_name(error)}
        results.append(value)
    print("__STUDY_OBSERVATIONS__" + json.dumps(results, ensure_ascii=False, allow_nan=False))
