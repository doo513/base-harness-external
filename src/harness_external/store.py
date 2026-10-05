"""Small durable Run/job store, unrelated to Host session databases."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import os
from pathlib import Path
import re
import sqlite3

from harness.json_codec import decode
from harness.common import canonical_bytes, canonical_hash
from .errors import HarnessError, require, validate_contract
from . import semantics
from .snapshots import no_links
from . import journal


class StopRun(HarnessError):
    """A persisted budget/deadline terminal transition, not a rolled-back edit."""


def identifier(value: str, prefix: str) -> str:
    require(isinstance(value, str) and bool(re.fullmatch(prefix + r"_[a-f0-9]{32}", value)), "INVALID_ID", "Invalid Harness identifier")
    return value


def default_state_directory() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state")))
    return base / "base-harness-external"


class Store:
    def __init__(self, directory: str | Path | None = None):
        raw = Path(directory or default_state_directory()).expanduser().absolute()
        no_links(raw)
        self.root = raw.resolve()
        self.database = self.root / "runs.sqlite3"
        self._initialized = False
        self._expected = ContextVar("harness_expected_context", default=None)
        self._current_cache = ContextVar("harness_transaction_cache", default=None)

    @contextmanager
    def expecting(self, context):
        token = self._expected.set(context)
        try:
            yield
        finally:
            self._expected.reset(token)

    def current(self, connection, run_id):
        cache = self._current_cache.get()
        key = (id(connection), run_id)
        if cache is not None and key in cache and cache[key][0] == connection.total_changes:
            return cache[key][1]
        run = self.run(connection, run_id, current=True)
        if cache is not None:
            cache[key] = (connection.total_changes, run)
        return run

    def _guard(self, connection):
        expected = self._expected.get()
        if expected is None:
            return
        run = self.current(connection, expected["run_id"])
        require(run["workspace"] == expected["workspace"] and run["contract"]["domain_id"] == expected["domain"],
                "RUN_CONTEXT_MISMATCH", "Run belongs to another workspace/Domain")
        for name in ("intent", "policy"):
            if name + "_ref" in expected:
                require(run[name]["ref"] == expected[name + "_ref"], "BINDING_IDENTITY_CHANGED", "Pinned intent or policy changed")

    def _initialize(self):
        if self._initialized:
            return
        no_links(self.root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        require(self.root.is_dir(), "STATE_DIRECTORY", "State root must be a directory")
        db = self.database
        no_links(db)
        if db.is_file():
            with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True) as existing:
                names = {row[0] for row in existing.execute("SELECT name FROM sqlite_master")}
                if {"runs", "jobs", "requests", "measurements", "measurements_job", "run_heads", "run_records", "run_events", "case_observations"} <= names and existing.execute("PRAGMA journal_mode").fetchone()[0] == "wal":
                    self._initialized = True
                    return
        with sqlite3.connect(db) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS runs(run_id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs(job_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS jobs_run ON jobs(run_id);
                CREATE TABLE IF NOT EXISTS requests(scope TEXT NOT NULL, request_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, response TEXT NOT NULL, PRIMARY KEY(scope, request_id));
                CREATE TABLE IF NOT EXISTS measurements(observation_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL, job_id TEXT NOT NULL, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS measurements_run ON measurements(run_id);
                CREATE INDEX IF NOT EXISTS measurements_job ON measurements(run_id,job_id);
                CREATE TABLE IF NOT EXISTS case_observations(observation_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL, job_id TEXT NOT NULL, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS case_observations_run_job ON case_observations(run_id,job_id);
            """)
            connection.executescript(journal.DDL)
        db.chmod(0o600)
        self._initialized = True

    @contextmanager
    def transaction(self, *, write=True):
        self._initialize()
        no_links(self.database)
        connection = sqlite3.connect(self.database if write else self.database.as_uri() + "?mode=ro",
                                     uri=not write, timeout=10, isolation_level=None)
        token = self._current_cache.set({})
        try:
            connection.execute("PRAGMA busy_timeout=10000")
            if not write:
                connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            self._guard(connection)
            yield connection
            connection.commit()
        except StopRun:
            connection.commit()
            raise
        except BaseException:
            connection.rollback()
            raise
        finally:
            self._current_cache.reset(token)
            connection.close()

    def directory(self, run_id: str) -> Path:
        path = self.root / "runs" / identifier(run_id, "run")
        no_links(path)
        return path

    @staticmethod
    def run(connection, run_id: str, *, current=False) -> dict:
        identifier(run_id, "run")
        snapshot = journal.head(connection, run_id)
        if snapshot is not None:
            require(connection.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone() is None,
                    "STATE_CORRUPT", "Run has conflicting legacy and current authorities")
            value = journal.load(connection, snapshot, current=current)
            require(value.get("run_id") == run_id, "STATE_CORRUPT", "Run identity mismatch")
            validate_contract(value["contract"])
            semantics.validate_run(value, current=current)
            return value
        row = connection.execute("SELECT data FROM runs WHERE run_id=?", (run_id,)).fetchone()
        require(row is not None, "RUN_NOT_FOUND", "Run does not exist in this state store")
        value = decode(row[0])
        require(value.get("run_id") == run_id, "STATE_CORRUPT", "Run identity mismatch")
        validate_contract(value["contract"])
        # Additive projection of historical strict Runs; no processes are resumed.
        semantics.ensure(value, projected=True)
        semantics.validate_run(value)
        return value

    @staticmethod
    def job(connection, job_id: str) -> dict:
        identifier(job_id, "job")
        row = connection.execute("SELECT data FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        require(row is not None, "JOB_NOT_FOUND", "Verification job does not exist")
        value = decode(row[0])
        require(value.get("job_id") == job_id, "STATE_CORRUPT", "Job identity mismatch")
        if value.get("result"):
            result = value["result"]
            require(result.get("result_hash") == canonical_hash({k: v for k, v in result.items() if k != "result_hash"}),
                    "RESULT_CORRUPT", "Stored Job result digest mismatch")
        return value

    @staticmethod
    def save_run(connection, run: dict):
        run["revision"] += 1
        if run.get("storage_schema") == journal.SCHEMA:
            journal.save(connection, run)
            return
        connection.execute("INSERT INTO runs VALUES(?,?) ON CONFLICT(run_id) DO UPDATE SET data=excluded.data",
                           (run["run_id"], canonical_bytes(run).decode()))

    @staticmethod
    def save_job(connection, job: dict):
        connection.execute("INSERT INTO jobs VALUES(?,?,?) ON CONFLICT(job_id) DO UPDATE SET data=excluded.data",
                           (job["job_id"], job["run_id"], canonical_bytes(job).decode()))
        journal.save_job(connection, job)

    @staticmethod
    def replay(connection, scope: str, request_id: str, fingerprint: str):
        require(isinstance(request_id, str) and 1 <= len(request_id) <= 128, "REQUEST_ID_REQUIRED", "Use a stable request ID for safe retries")
        row = connection.execute("SELECT fingerprint,response FROM requests WHERE scope=? AND request_id=?", (scope, request_id)).fetchone()
        if row is None:
            return None
        require(row[0] == fingerprint, "REQUEST_CONFLICT", "This request ID was already used for different input")
        value = decode(row[1])
        error = value.get("__harness_error__") if isinstance(value, dict) else None
        if error:
            exception = StopRun if error.get("terminal") else HarnessError
            raise exception(error["code"], error["message"])
        return value

    @staticmethod
    def remember(connection, scope: str, request_id: str, fingerprint: str, response: dict):
        connection.execute("INSERT INTO requests VALUES(?,?,?,?)", (scope, request_id, fingerprint, canonical_bytes(response).decode()))

    @staticmethod
    def remember_error(connection, scope: str, request_id: str, fingerprint: str, code: str, message: str, *, terminal=False):
        Store.remember(connection, scope, request_id, fingerprint,
                       {"__harness_error__": {"code": code, "message": message, "terminal": terminal}})

    @staticmethod
    def measurements(connection, run_id: str, job_id: str | None = None, *, limit=None, offset=0):
        query = "SELECT data FROM measurements WHERE run_id=?"
        args = [run_id]
        if job_id:
            query += " AND job_id=?"
            args.append(job_id)
        query += " ORDER BY rowid"
        if limit is not None:
            query += " LIMIT ? OFFSET ?"
            args.extend([limit, offset])
        return Store._measurement_rows(connection.execute(query, args), run_id)

    @staticmethod
    def gate_measurements(connection, run):
        if not run.get("candidate"):
            return []
        result = []
        for ref in run["gate_bindings"].values():
            rows = connection.execute("""SELECT data FROM measurements WHERE run_id=?
                AND json_extract(data,'$.check_ref')=? AND json_extract(data,'$.subject')=?
                ORDER BY rowid DESC LIMIT 1""", (run["run_id"], canonical_bytes(ref).decode(),
                                                  canonical_bytes(run["candidate"]["subject"]).decode()))
            result.extend(Store._measurement_rows(rows, run["run_id"]))
        return result

    @staticmethod
    def validate_result(connection, job):
        value = job.get("result")
        if not value:
            return
        require(value.get("result_hash") == canonical_hash({k: v for k, v in value.items() if k != "result_hash"}),
                "RESULT_CORRUPT", "Verification result digest mismatch")
        if value.get("schema_version") != "verification-result-v2":
            return  # Historical aggregate bodies remain readable, never rewritten.
        require(value["check_set_hash"] == job["check_set_hash"] == semantics.check_set_hash(job["checks"]),
                "RESULT_BINDING", "Verification check set mismatch")
        for ref in value["observation_refs"]:
            rows = connection.execute("SELECT data FROM measurements WHERE observation_id=? AND run_id=? AND job_id=?",
                                      (ref["observation_id"], job["run_id"], job["job_id"]))
            items = Store._measurement_rows(rows, job["run_id"])
            require(len(items) == 1 and items[0]["record_hash"] == ref["record_hash"]
                    and items[0].get("check_set_hash") == job["check_set_hash"], "RESULT_BINDING", "Referenced measurement changed or is missing")
        for ref in value.get("case_observation_refs", []):
            items = Store._case_rows(connection.execute("SELECT data FROM case_observations WHERE observation_id=? AND run_id=? AND job_id=?",
                                     (ref["observation_id"], job["run_id"], job["job_id"])), job["run_id"])
            require(len(items) == 1 and items[0]["record_hash"] == ref["record_hash"] and items[0]["check_set_hash"] == job["check_set_hash"],
                    "RESULT_BINDING", "Referenced case observation is missing or changed")
            Store.validate_case_binding(items[0], job)

    @staticmethod
    def _case_rows(rows, run_id):
        result = []
        for row in rows:
            item = decode(row[0])
            require(item.get("record_type") == "case-observation-v1" and item.get("run_id") == run_id and item.get("origin") == "verifier"
                    and item.get("record_hash") == canonical_hash({k: v for k, v in item.items() if k != "record_hash"}),
                    "CASE_OBSERVATION_CORRUPT", "Case observation identity/digest mismatch")
            result.append(item)
        return result

    @staticmethod
    def validate_case_binding(item, job):
        subject = job.get("baseline") if item["subject_role"] == "baseline" else job["candidate"] if item["subject_role"] == "candidate" else None
        require(subject is not None and item["subject"] == subject["subject"] and item["candidate_hash"] == subject["candidate_hash"]
                and item["run_id"] == job["run_id"] and item["job_id"] == job["job_id"] and item["contract_hash"] == job["contract_hash"]
                and item["check_set_hash"] == job["check_set_hash"] and item["attempt"] == job.get("attempt")
                and item["policy_ref"] == job["policy_ref"] and item["interpretation_ref"] == job["interpretation_ref"]
                and item.get("acceptance_binding_hash") == job.get("acceptance_binding_hash") and item["domain_identity"] == job["domain_identity"]
                and any(c["ref"] == item["check_ref"] and c["check_id"] == item["check_id"] for c in job["checks"])
                and item["adapter_identity"] == job["adapter_bindings"].get(item["check_id"]),
                "CASE_OBSERVATION_BINDING", "Case observation does not belong to its execution")

    @staticmethod
    def cases(connection, run_id, job_id=None, *, limit=None, offset=0):
        sql, args = "SELECT data FROM case_observations WHERE run_id=?", [run_id]
        if job_id:
            sql += " AND job_id=?"
            args.append(job_id)
        sql += " ORDER BY rowid"
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            args.extend([limit, offset])
        result = Store._case_rows(connection.execute(sql, args), run_id)
        for item in result:
            Store.validate_case_binding(item, Store.job(connection, item["job_id"]))
        return result

    def checkpoint_case(self, connection, job_id, owner, item):
        import time
        job = self.job(connection, job_id)
        run = self.current(connection, job["run_id"])
        require(job["status"] == "running" and job.get("owner") == owner and run["status"] == "active" and run["active_job"] == job_id
                and run["generation"] == job["generation"] and time.time() < job["deadline_at"], "STALE_CHECKPOINT", "Worker no longer owns this observation")
        self.validate_case_binding(item, job)
        self._case_rows([(canonical_bytes(item).decode(),)], job["run_id"])
        prior = connection.execute("SELECT data FROM case_observations WHERE observation_id=?", (item["observation_id"],)).fetchone()
        if prior:
            require(decode(prior[0]) == item, "CASE_OBSERVATION_CONFLICT", "Case observation already has another result")
            return
        connection.execute("INSERT INTO case_observations VALUES(?,?,?,?)", (item["observation_id"], item["run_id"], job_id, canonical_bytes(item).decode()))
        journal.case_observation(connection, item)
        job["observed_cases"] = job.get("observed_cases", 0) + 1
        job["completed_cases"] = job.get("completed_cases", 0) + int(item["case"]["finished"])
        self.save_job(connection, job)

    @staticmethod
    def _measurement_rows(rows, run_id):
        result = []
        for row in rows:
            item = decode(row[0])
            require(item.get("record_hash") == canonical_hash({k: v for k, v in item.items() if k != "record_hash"}),
                    "OBSERVATION_CORRUPT", "Stored measurement digest mismatch")
            observation = item["report"]["observation"]
            require(item["run_id"] == run_id and observation["runId"] == run_id and observation["taskId"] == item["job_id"]
                    and observation["observationId"] == item["observation_id"] and observation["subject"] == item["subject"]
                    and observation["checkRef"] == item["check_ref"], "OBSERVATION_BINDING", "Stored measurement identity mismatch")
            result.append(item)
        return result

    def checkpoint(self, connection, job_id, owner, item):
        import time
        job = self.job(connection, job_id)
        run = self.current(connection, job["run_id"])
        require(job["status"] == "running" and job.get("owner") == owner and run["status"] == "active"
                and run["active_job"] == job_id and run["generation"] == job["generation"] and time.time() < job["deadline_at"],
                "STALE_CHECKPOINT", "Worker no longer owns this measurement")
        role = item.get("subject_role", "candidate")
        subject = job.get("baseline") if role == "baseline" else job["candidate"] if role == "candidate" else None
        require(subject is not None and item["run_id"] == run["run_id"] and item["job_id"] == job_id and item["subject"] == subject["subject"]
                and item["candidate_hash"] == subject["candidate_hash"]
                and any(check["ref"] == item["check_ref"] for check in job["checks"]), "CHECKPOINT_BINDING", "Measurement/check/subject binding mismatch")
        require(item.get("check_set_hash") == job.get("check_set_hash"), "CHECKPOINT_BINDING", "Measurement check set mismatch")
        observation = item["report"]["observation"]
        require(observation["runId"] == run["run_id"] and observation["taskId"] == job_id and observation["subject"] == item["subject"]
                and observation["checkRef"] == item["check_ref"] and observation["observationId"] == item["observation_id"],
                "CHECKPOINT_BINDING", "Verifier response identity mismatch")
        for ref in item.get("case_observation_refs", []):
            cases = self._case_rows(connection.execute("SELECT data FROM case_observations WHERE observation_id=? AND run_id=? AND job_id=?",
                                    (ref["observation_id"], run["run_id"], job_id)), run["run_id"])
            require(len(cases) == 1 and cases[0]["record_hash"] == ref["record_hash"]
                    and cases[0]["execution_observation_id"] == item["observation_id"]
                    and cases[0]["check_ref"] == item["check_ref"] and cases[0]["subject"] == item["subject"],
                    "CASE_OBSERVATION_BINDING", "Case does not belong to the finalized Check observation")
        connection.execute("INSERT INTO measurements VALUES(?,?,?,?)", (item["observation_id"], item["run_id"], job_id, canonical_bytes(item).decode()))
        journal.measurement(connection, item)
        job["completed_checks"] = job.get("completed_checks", 0) + 1
        self.save_job(connection, job)
