"""Small durable Run/job store, unrelated to Host session databases."""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import re
import sqlite3

from harness.measurement_v5 import decode
from harness.common import canonical_bytes, canonical_hash
from .errors import HarnessError, require, validate_contract
from . import semantics
from .snapshots import no_links


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

    def _initialize(self):
        if self._initialized:
            return
        no_links(self.root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        require(self.root.is_dir(), "STATE_DIRECTORY", "State root must be a directory")
        db = self.database
        no_links(db)
        with sqlite3.connect(db) as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS runs(run_id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs(job_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS jobs_run ON jobs(run_id);
                CREATE TABLE IF NOT EXISTS requests(scope TEXT NOT NULL, request_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, response TEXT NOT NULL, PRIMARY KEY(scope, request_id));
                CREATE TABLE IF NOT EXISTS measurements(observation_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL, job_id TEXT NOT NULL, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS measurements_run ON measurements(run_id);
            """)
        db.chmod(0o600)
        self._initialized = True

    @contextmanager
    def transaction(self):
        self._initialize()
        no_links(self.database)
        connection = sqlite3.connect(self.database, timeout=10, isolation_level=None)
        try:
            connection.execute("PRAGMA busy_timeout=10000")
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except StopRun:
            connection.commit()
            raise
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def directory(self, run_id: str) -> Path:
        path = self.root / "runs" / identifier(run_id, "run")
        no_links(path)
        return path

    @staticmethod
    def run(connection, run_id: str) -> dict:
        identifier(run_id, "run")
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
        return value

    @staticmethod
    def save_run(connection, run: dict):
        run["revision"] += 1
        connection.execute("INSERT OR REPLACE INTO runs VALUES(?,?)", (run["run_id"], canonical_bytes(run).decode()))

    @staticmethod
    def save_job(connection, job: dict):
        connection.execute("INSERT OR REPLACE INTO jobs VALUES(?,?,?)", (job["job_id"], job["run_id"], canonical_bytes(job).decode()))

    @staticmethod
    def replay(connection, scope: str, request_id: str, fingerprint: str):
        require(isinstance(request_id, str) and 1 <= len(request_id) <= 128, "REQUEST_ID_REQUIRED", "Use a stable request ID for safe retries")
        row = connection.execute("SELECT fingerprint,response FROM requests WHERE scope=? AND request_id=?", (scope, request_id)).fetchone()
        if row is None:
            return None
        require(row[0] == fingerprint, "REQUEST_CONFLICT", "This request ID was already used for different input")
        return decode(row[1])

    @staticmethod
    def remember(connection, scope: str, request_id: str, fingerprint: str, response: dict):
        connection.execute("INSERT INTO requests VALUES(?,?,?,?)", (scope, request_id, fingerprint, canonical_bytes(response).decode()))

    @staticmethod
    def measurements(connection, run_id: str, job_id: str | None = None):
        query = "SELECT data FROM measurements WHERE run_id=?"
        args = [run_id]
        if job_id:
            query += " AND job_id=?"
            args.append(job_id)
        rows = connection.execute(query + " ORDER BY rowid", args)
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
        run = self.run(connection, job["run_id"])
        require(job["status"] == "running" and job.get("owner") == owner and run["status"] == "active"
                and run["active_job"] == job_id and run["generation"] == job["generation"] and time.time() < job["deadline_at"],
                "STALE_CHECKPOINT", "Worker no longer owns this measurement")
        require(item["run_id"] == run["run_id"] and item["job_id"] == job_id and item["subject"] == job["candidate"]["subject"]
                and any(check["ref"] == item["check_ref"] for check in job["checks"]), "CHECKPOINT_BINDING", "Measurement/check/subject binding mismatch")
        observation = item["report"]["observation"]
        require(observation["runId"] == run["run_id"] and observation["taskId"] == job_id and observation["subject"] == item["subject"]
                and observation["checkRef"] == item["check_ref"] and observation["observationId"] == item["observation_id"],
                "CHECKPOINT_BINDING", "Verifier response identity mismatch")
        connection.execute("INSERT INTO measurements VALUES(?,?,?,?)", (item["observation_id"], item["run_id"], job_id, canonical_bytes(item).decode()))
        job["completed_checks"] = job.get("completed_checks", 0) + 1
        self.save_job(connection, job)
