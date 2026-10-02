"""Evaluation positive control, hidden from the model's task environment."""
import json
import math
import sqlite3
import uuid


def data(value):
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in value:
            data(item)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            data(item)
        return
    raise ValueError("invalid JSON data")


def canonical(value):
    data(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def text(value):
    if type(value) is not str or not value.strip():
        raise ValueError("nonblank string required")


def integer(value, minimum=None):
    if type(value) is not int or (minimum is not None and value < minimum):
        raise ValueError("invalid integer")


class Queue:
    def __init__(self, path):
        self.path = str(path)
        with sqlite3.connect(self.path, timeout=15) as db:
            db.execute("CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL)")
            db.execute("INSERT OR IGNORE INTO state VALUES (1, ?)", (canonical({"jobs": {}, "events": [], "requests": {}}),))

    @staticmethod
    def event(state, job, kind):
        state["events"].append({"seq": len(state["events"]) + 1, "job_id": job["id"], "kind": kind, "attempt": job["attempts"]})

    @staticmethod
    def clear(job):
        job.update(worker=None, token=None, lease_until=None)

    def mutate(self, operation, arguments, request_id, apply):
        text(request_id)
        fingerprint = canonical([operation, arguments])
        db = sqlite3.connect(self.path, timeout=15)
        try:
            db.execute("BEGIN IMMEDIATE")
            state = json.loads(db.execute("SELECT body FROM state WHERE id=1").fetchone()[0])
            if request_id in state["requests"]:
                old = state["requests"][request_id]
                if old["fingerprint"] != fingerprint:
                    raise ValueError("request conflict")
                db.rollback()
                return old["value"]
            result = apply(state)
            state["requests"][request_id] = {"fingerprint": fingerprint, "value": json.loads(canonical(result))}
            # Preserve dictionary insertion order of jobs, unlike canonical request fingerprints.
            db.execute("UPDATE state SET body=? WHERE id=1", (json.dumps(state, ensure_ascii=False, allow_nan=False),))
            db.commit()
            return json.loads(canonical(result))
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def add(self, job_id, payload, depends_on=None, priority=0, max_attempts=3, *, request_id):
        text(job_id); data(payload); integer(priority); integer(max_attempts, 1)
        deps = [] if depends_on is None else depends_on
        if type(deps) is not list:
            raise ValueError("dependencies must be a list")
        for dep in deps:
            text(dep)
        if len(set(deps)) != len(deps) or job_id in deps:
            raise ValueError("invalid dependencies")
        def apply(state):
            if job_id in state["jobs"] or any(dep not in state["jobs"] for dep in deps):
                raise ValueError("job/dependency identity")
            job = dict(id=job_id, payload=json.loads(canonical(payload)), dependencies=list(deps), priority=priority,
                       max_attempts=max_attempts, attempts=0, status="queued", worker=None, token=None, lease_until=None, result=None)
            state["jobs"][job_id] = job
            self.event(state, job, "added")
            return True
        return self.mutate("add", [job_id, payload, deps, priority, max_attempts], request_id, apply)

    def claim(self, worker, now, lease_seconds, *, request_id):
        text(worker); integer(now, 0); integer(lease_seconds, 1)
        def apply(state):
            jobs = state["jobs"]
            for job in jobs.values():
                if job["status"] == "running" and job["lease_until"] <= now:
                    job["status"] = "failed" if job["attempts"] >= job["max_attempts"] else "queued"
                    self.clear(job)
                    self.event(state, job, "failed" if job["status"] == "failed" else "expired")
            while True:
                changed = False
                for job in jobs.values():
                    if job["status"] == "queued" and any(jobs[dep]["status"] in ("failed", "blocked") for dep in job["dependencies"]):
                        job["status"] = "blocked"
                        self.event(state, job, "blocked")
                        changed = True
                if not changed:
                    break
            eligible = [j for j in jobs.values() if j["status"] == "queued" and all(jobs[d]["status"] == "succeeded" for d in j["dependencies"])]
            if not eligible:
                return None
            job = max(eligible, key=lambda j: j["priority"])
            job.update(status="running", attempts=job["attempts"]+1, worker=worker, token=uuid.uuid4().hex, lease_until=now+lease_seconds)
            self.event(state, job, "claimed")
            return job
        return self.mutate("claim", [worker, now, lease_seconds], request_id, apply)

    def _complete(self, operation, job_id, token, now, extra, request_id):
        text(job_id); text(token); integer(now, 0)
        def apply(state):
            job = state["jobs"].get(job_id)
            if not job or job["status"] != "running" or job["token"] != token or not now < job["lease_until"]:
                return False
            if operation == "renew":
                job["lease_until"] = now + extra[0]
                kind = "renewed"
            else:
                success, result = extra
                job["status"] = "succeeded" if success else "failed" if job["attempts"] >= job["max_attempts"] else "queued"
                job["result"] = json.loads(canonical(result)) if success else None
                self.clear(job)
                kind = "succeeded" if success else "failed" if job["status"] == "failed" else "retry"
            self.event(state, job, kind)
            return True
        return self.mutate(operation, [job_id, token, now, *extra], request_id, apply)

    def renew(self, job_id, token, now, lease_seconds, *, request_id):
        integer(lease_seconds, 1)
        return self._complete("renew", job_id, token, now, [lease_seconds], request_id)

    def finish(self, job_id, token, now, success, result=None, *, request_id):
        if type(success) is not bool:
            raise ValueError("boolean success required")
        data(result)
        return self._complete("finish", job_id, token, now, [success, result], request_id)

    def get(self, job_id):
        text(job_id)
        with sqlite3.connect(self.path, timeout=15) as db:
            return json.loads(db.execute("SELECT body FROM state WHERE id=1").fetchone()[0])["jobs"].get(job_id)

    def events(self):
        with sqlite3.connect(self.path, timeout=15) as db:
            return json.loads(db.execute("SELECT body FROM state WHERE id=1").fetchone()[0])["events"]
