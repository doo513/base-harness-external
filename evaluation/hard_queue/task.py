"""Fixed interactions; golden outputs are measured from the hidden positive control
and sanity-checked before either model runs. No expected values enter the driver.
"""
from pathlib import Path
import random

HERE = Path(__file__).parent


def action(op, **args):
    return {"op": op, "args": args}


def add(job="a", payload=None, **kwargs):
    return action("add", job_id=job, payload=payload, **kwargs)


def claim(label="c", now=0, lease=5, **kwargs):
    return {**action("claim", worker="w", now=now, lease_seconds=lease, **kwargs), "save": label}


def finish(label="c", job="a", now=1, success=True, **kwargs):
    return action("finish", job_id=job, token={"$token": label}, now=now, success=success, **kwargs)


def renew(label="c", job="a", now=1, lease=5, **kwargs):
    return action("renew", job_id=job, token={"$token": label}, now=now, lease_seconds=lease, **kwargs)


def get(job="a"):
    return action("get", job_id=job)


def sequence(name, actions, operation="sequence"):
    for index, item in enumerate(actions):
        if item["op"] not in ("get", "events"):
            item["args"].setdefault("request_id", "r"+str(index))
    return {"name": name, "request": {"operation": operation, "actions": actions}, "expected": None}


CASES = [
    sequence("priority-and-insertion-ties", [add("z", priority=1), add("a", priority=1), add("high", priority=2), claim("c1"), claim("c2"), claim("c3"), claim("empty")]),
    sequence("dependency-unblocks-only-after-success", [add(), add("b", depends_on=["a"], priority=100), claim(), claim("none"), finish(), claim("b", now=1), get("b")]),
    sequence("retry-exhaustion", [add(max_attempts=2), claim(), finish(success=False), claim("c2", now=2), finish("c2", now=3, success=False), claim("none", now=4), get()]),
    sequence("expiry-and-transitive-blocking", [add(max_attempts=1), add("b", depends_on=["a"]), add("c", depends_on=["b"]), claim(), claim("none", now=5), get(), get("b"), get("c")]),
    sequence("finish-at-exact-deadline", [add(), claim(), finish(now=5), get(), claim("new", now=5), finish(now=6), finish("new", now=6), get()]),
    sequence("renewal-uses-now-not-old-deadline", [add(), claim(), renew(now=2, lease=4), get(), finish(now=6), claim("new", now=6), get()]),
    sequence("renew-at-exact-deadline", [add(), claim(), renew(now=5), get()]),
    sequence("renewal-may-shorten-lease", [add(), claim(lease=20), renew(now=2, lease=1), finish(now=3), claim("new", now=3)]),
    sequence("replay-original-claim-after-completion", [add(request_id="add"), claim(request_id="claim"), finish(result={"ok": [1]}), claim("old", request_id="claim"), get()]),
    sequence("replay-none-after-new-job", [claim("none", request_id="empty"), add(), claim("replayed", request_id="empty"), get(), claim("real", request_id="real")]),
    sequence("replay-false-stays-false", [action("finish", job_id="a", token="wrong", now=1, success=True, request_id="f"), add(), claim(), action("finish", job_id="a", token="wrong", now=1, success=True, request_id="f"), get()]),
    sequence("cross-method-request-conflict", [add(request_id="same"), claim(request_id="same"), get()]),
    sequence("canonical-objects-but-distinct-number-types", [add(payload={"x": 1, "y": True}, request_id="a"), add(payload={"y": True, "x": 1}, request_id="a"), add(payload={"x": 1.0, "y": True}, request_id="a"), add(payload={"x": 1, "y": 1}, request_id="a"), get()]),
    sequence("defaults-equivalent-on-replay", [add(request_id="a"), add(depends_on=[], priority=0, max_attempts=3, request_id="a"), get()]),
    sequence("invalid-request-does-not-reserve-id", [add(priority=True, request_id="same"), add(request_id="same"), get()]),
    sequence("duplicate-job-atomic-error", [add(), add(payload=2), get()]),
    sequence("dependency-validation-is-atomic", [add("z", depends_on=["missing"], request_id="z"), add(), add("z", depends_on=["a", "a"], request_id="z"), add("z", depends_on=["z"], request_id="z"), add("z", depends_on=["a"], request_id="z"), get("z")]),
    sequence("json-value-validation", [{**add(request_id="same"), "special": "nan"}, {**add(request_id="same"), "special": "key"}, {**add(request_id="same"), "special": "tuple"}, add(request_id="same"), get()]),
    sequence("detached-input-and-return", [{**add(payload={"items": [1]}), "mutate_argument": True}, {**get(), "mutate_return": True}, get()]),
    sequence("token-from-other-job-refused", [add(), add("b"), claim("ca"), claim("cb"), finish("cb"), get(), get("b"), finish("ca"), finish("cb", job="b")]),
    sequence("retry-retains-original-position", [add("z"), add("a"), claim(), finish(job="z", success=False), claim("again", now=2), get("z")]),
    sequence("read-does-not-reconcile", [add(max_attempts=1), claim(), get(), action("events"), renew(now=100), get(), action("events")]),
    sequence("all-expiries-reconciled-before-selection", [add("a", max_attempts=1), add("b", max_attempts=1), claim("a"), claim("b"), add("fresh", priority=100), claim("new", now=5), get("a"), get("b")]),
    sequence("failed-parent-added-child-blocks", [add(max_attempts=1), claim(), finish(success=False), add("b", depends_on=["a"]), get("b"), claim("none"), get("b")]),
    sequence("argument-type-boundaries", [add(max_attempts=True), add(priority=1.5), add(job=" "), add(), action("claim", worker="w", now=True, lease_seconds=5), action("claim", worker="w", now=0, lease_seconds=0), claim(), finish(success=1), renew(now=-1), finish(result=[{"ok": False}]), get()]),
    sequence("missing-and-wrong-token-are-noops", [get(), action("renew", job_id="missing", token="x", now=0, lease_seconds=1), action("finish", job_id="missing", token="x", now=0, success=True), add(), claim(), action("finish", job_id="a", token="x", now=1, success=True), get()]),
    sequence("unicode-identifiers-and-negative-priority", [add(" 한글 ", {"한글": [False, 1, None]}, priority=-10), add("z", priority=-1), claim("z"), claim("u"), finish("u", job=" 한글 "), get(" 한글 ")]),
    sequence("cli-durable-roundtrip", [add(payload={"x": [1]}), claim(), finish(result={"r": 2}), get(), action("events")], "cli"),
    sequence("cli-invalid-schema-and-duplicate-keys", [
        {**action("get", job_id="none"), "raw": '{"op":"get","op":"events","args":{}}'},
        {**action("get", job_id="none"), "raw": '{"op":"get","args":{"job_id":"x"},"extra":1}'},
        {**action("get", job_id="none"), "raw": '{"op":"unknown","args":{}}'},
        {**action("get", job_id="none"), "raw": 'invalid'},
        action("get", job_id="none"), action("events")], "cli"),
]
for seed in (61, 902):
    rng = random.Random(seed)
    actions = [add("j"+str(i), {"i": i}, depends_on=[] if i < 3 else ["j"+str(rng.randrange(i))], priority=rng.randrange(-3, 4), max_attempts=rng.randrange(1, 4)) for i in range(18)]
    for tick in range(35):
        # May return None, intentionally exercising complete dependency/expiry reconciliation.
        actions.append(claim("tick"+str(tick), now=tick*3, lease=2))
    actions.extend(get("j"+str(i)) for i in range(18))
    CASES.append(sequence("seeded-expiry-dag-"+str(seed), actions))
for kind in ("adds", "add-replay", "claims", "replay", "finish"):
    CASES.append({"name": "concurrent-"+kind, "request": {"operation": "concurrent", "kind": kind}, "expected": None})

TASK = {"id": "leasequeue", "title": "Transactional lease queue with dependency and request recovery",
        "spec": (HERE / "spec.txt").read_text(),
        "sources": {"leasequeue.py": "class Queue:\n    def __init__(self,path): self.path=path\n    def add(self,*a,**k): raise NotImplementedError()\n    def claim(self,*a,**k): raise NotImplementedError()\n    def renew(self,*a,**k): raise NotImplementedError()\n    def finish(self,*a,**k): raise NotImplementedError()\n    def get(self,*a,**k): raise NotImplementedError()\n    def events(self): raise NotImplementedError()\n",
                    "leasequeue_cli.py": "def main(argv=None):\n    raise NotImplementedError()\n\nif __name__=='__main__':\n    raise SystemExit(main())\n"},
        "acceptance_test": (HERE / "public_tests.py").read_text(), "cases": CASES}
