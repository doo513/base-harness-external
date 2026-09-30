"""Model-invoked API. Only verifier workers can record verification observations."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import time
import uuid

from harness import measurement_v5, common
from harness.common import canonical_bytes, canonical_hash, redact
from . import API_VERSION, ASSURANCE
from .domain import HarnessError, build_contract, fields, limits, require
from .snapshots import capture, no_links, read_file, validate_candidate
from .store import Store, StopRun, identifier


LEASE_SECONDS = 20


def verifier_identity() -> dict:
    paths = [Path(measurement_v5.__file__), Path(common.__file__), Path(__file__).with_name("domain.py"), Path(__file__),
             Path(__file__).with_name("worker.py"), Path(__file__).with_name("snapshots.py"), Path(__file__).with_name("store.py"),
             Path(__file__).with_name("develop_manifest.json")]
    bridge = Path(__file__).resolve().parents[2] / "runtime" / "script" / "external-harness-sandbox.ts"
    sandbox = bridge.parent.parent / "packages" / "security" / "src" / "sandbox.ts"
    sources = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    for name, path in (("sandbox_bridge", bridge), ("sandbox", sandbox)):
        sources[name] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "unavailable"
    return {"id": "external-develop-measurement", "revision": "1", "measurement_protocol": 5,
            "implementation_hash": canonical_hash(sources), "sources": sources}


def response(**data) -> dict:
    return {"api_version": API_VERSION, "assurance": ASSURANCE, "ready": False, **data}


class Harness:
    def __init__(self, state_dir: str | Path | None = None):
        self.store = Store(state_dir)

    def _reconcile(self, connection, run):
        job_id = run.get("active_job")
        if not job_id:
            return
        job = self.store.job(connection, job_id)
        if job["status"] in {"queued", "running"} and (
            time.time() > job["deadline_at"] or time.time() - job["heartbeat_at"] > LEASE_SECONDS
        ):
            job.update(status="interrupted", error={"code": "WORKER_INTERRUPTED", "message": "Worker lease or verification deadline expired; no work was automatically resumed"})
            self.store.save_job(connection, job)
            run.update(active_job=None, phase="review", verification={"status": "interrupted", "job_id": job_id})
            self.store.save_run(connection, run)

    def _admit(self, connection, run, *, verification=False):
        self._reconcile(connection, run)
        require(run["status"] == "active", "RUN_CLOSED", "Start a new Run; a closed or blocked Run cannot be reopened")
        reason = None
        if time.time() >= run["deadline_at"]:
            reason = "DEADLINE_EXCEEDED"
        elif run["actions"] >= run["limits"]["max_actions"]:
            reason = "ACTION_BUDGET_EXHAUSTED"
        elif verification and run["verification_attempts"] >= run["limits"]["max_verifications"]:
            reason = "VERIFICATION_BUDGET_EXHAUSTED"
        if reason:
            run.update(status="blocked", phase="handoff", termination_reason=reason)
            if run.get("active_job"):
                job = self.store.job(connection, run["active_job"])
                job.update(status="cancelled", cleanup="pending")
                self.store.save_job(connection, job)
                run["active_job"] = None
            self.store.save_run(connection, run)
            raise StopRun(reason, "Harness limit reached; hand off to the user. External model execution is not controlled.")
        run["actions"] += 1

    def start(self, *, domain_id: str, goal: str, workspace: str, parameters: dict, request_id: str, budget: dict | None = None):
        source = Path(workspace).expanduser().absolute()
        no_links(source)
        require(source.is_dir(), "WORKSPACE_NOT_FOUND", "Workspace must exist")
        source = source.resolve()
        require(not self.store.root.is_relative_to(source) and not source.is_relative_to(self.store.root),
                "STATE_WORKSPACE_OVERLAP", "Run state and workspace must be disjoint directories")
        fingerprint = canonical_hash({"operation": "start", "domain_id": domain_id, "goal": goal,
                                      "workspace": str(source), "parameters": parameters, "budget": budget})
        with self.store.transaction() as connection:
            prior = self.store.replay(connection, "start", request_id, fingerprint)
            if prior:
                return prior
            contract = build_contract(domain_id, goal, parameters, verifier_identity())
            bound = limits(budget)
            run_id = "run_" + uuid.uuid4().hex
            now = time.time()
            run = {"schema_version": API_VERSION, "run_id": run_id, "workspace": str(source), "goal": goal,
                   "contract": contract, "revision": 0, "status": "active", "phase": "awaiting_submission",
                   "created_at": now, "deadline_at": now + bound["timeout_seconds"], "limits": bound,
                   "actions": 0, "verification_attempts": 0, "generation": 0, "candidate": None,
                   "observations": [], "verification": {"status": "not_run"}, "active_job": None, "record": None}
            self.store.directory(run_id).mkdir(parents=True, mode=0o700)
            self.store.save_run(connection, run)
            result = response(run_id=run_id, contract=contract, phase=run["phase"], limits=bound)
            self.store.remember(connection, "start", request_id, fingerprint, result)
            return result

    def observe(self, run_id: str, observation: dict, request_id: str):
        fields(observation, {"note", "references"}, {"note"})
        require(isinstance(observation["note"], str) and 0 < len(observation["note"]) <= 16000, "INVALID_OBSERVATION", "A bounded note is required")
        refs = observation.get("references", [])
        require(isinstance(refs, list) and len(refs) <= 32 and all(isinstance(ref, str) and len(ref) <= 512 for ref in refs),
                "INVALID_OBSERVATION", "References must be bounded strings; they are not automatically opened")
        fingerprint = canonical_hash({"operation": "observe", "observation": observation})
        with self.store.transaction() as connection:
            prior = self.store.replay(connection, run_id, request_id, fingerprint)
            if prior:
                return prior
            run = self.store.run(connection, run_id)
            self._admit(connection, run)
            require(len(run["observations"]) < 100, "OBSERVATION_LIMIT", "At most 100 caller observations per Run")
            item = {"observation_id": "note_" + uuid.uuid4().hex, "origin": "caller", "trust": "untrusted", **redact(observation)}
            run["observations"].append(item)
            self.store.save_run(connection, run)
            result = response(run_id=run_id, observation=item)
            self.store.remember(connection, run_id, request_id, fingerprint, result)
            return result

    def submit(self, run_id: str, request_id: str):
        fingerprint = canonical_hash({"operation": "submit"})
        with self.store.transaction() as connection:
            prior = self.store.replay(connection, run_id, request_id, fingerprint)
            if prior:
                return prior
            run = self.store.run(connection, run_id)
            self._admit(connection, run)
            require(not run["active_job"], "VERIFICATION_ACTIVE", "Wait for verification or finish as abandoned")
            candidate = capture(Path(run["workspace"]), run["contract"]["inputs"], self.store.directory(run_id))
            run.update(candidate=candidate, generation=run["generation"] + 1, phase="submitted", verification={"status": "not_run"})
            self.store.save_run(connection, run)
            result = response(run_id=run_id, candidate=candidate)
            self.store.remember(connection, run_id, request_id, fingerprint, result)
            return result

    def verify(self, run_id: str, request_id: str):
        fingerprint = canonical_hash({"operation": "verify"})
        with self.store.transaction() as connection:
            prior = self.store.replay(connection, run_id, request_id, fingerprint)
            if prior:
                return prior
            run = self.store.run(connection, run_id)
            self._admit(connection, run, verification=True)
            require(not run["active_job"], "VERIFICATION_ACTIVE", "A verifier already owns this Run")
            require(run["candidate"] is not None, "SUBMIT_REQUIRED", "Submit a snapshot before verification")
            require(run["contract"]["verifier"] == verifier_identity(), "VERIFIER_CHANGED", "Verifier code changed; start a new Run with the new version")
            job_id = "job_" + uuid.uuid4().hex
            now = time.time()
            timeout = 45 + sum(check.get("timeout_seconds", 1) + 10 for check in run["contract"]["checks"])
            job = {"job_id": job_id, "run_id": run_id, "status": "queued", "heartbeat_at": now,
                   "deadline_at": min(run["deadline_at"], now + timeout), "generation": run["generation"],
                   "contract_hash": run["contract"]["contract_hash"], "candidate_hash": run["candidate"]["candidate_hash"],
                   "cleanup": "not_started", "result": None}
            run.update(active_job=job_id, phase="verifying", verification={"status": "queued", "job_id": job_id},
                       verification_attempts=run["verification_attempts"] + 1)
            self.store.save_job(connection, job)
            self.store.save_run(connection, run)
            result = response(run_id=run_id, job_id=job_id, status="queued")
            self.store.remember(connection, run_id, request_id, fingerprint, result)
        # Durable queue entry exists before spawning. Replayed requests never spawn twice.
        from .worker import spawn_worker
        try:
            spawn_worker(self.store.root, job_id)
        except OSError:
            with self.store.transaction() as connection:
                job = self.store.job(connection, job_id)
                job.update(status="error", error={"code": "WORKER_START_FAILED", "message": "Could not start verifier worker"})
                self.store.save_job(connection, job)
                run = self.store.run(connection, run_id)
                run.update(active_job=None, phase="review", verification={"status": "error", "job_id": job_id})
                self.store.save_run(connection, run)
        return result

    def status(self, run_id: str, job_id: str | None = None, *, check_workspace: bool = False):
        with self.store.transaction() as connection:
            run = self.store.run(connection, run_id)
            self._reconcile(connection, run)
            # Merely inspecting an expired Run does not execute/resume anything.
            if run["status"] == "active" and time.time() >= run["deadline_at"]:
                run.update(status="blocked", phase="handoff", termination_reason="DEADLINE_EXCEEDED")
                self.store.save_run(connection, run)
            jobs = [decode_job[0] for decode_job in connection.execute("SELECT job_id FROM jobs WHERE run_id=?", (run_id,))]
            if job_id:
                job = self.store.job(connection, job_id)
                require(job["run_id"] == run_id, "JOB_RUN_MISMATCH", "Job belongs to another Run")
                return response(run_id=run_id, job=job)
            if run["record"]:
                require(run["record"].get("record_hash") == canonical_hash({k: v for k, v in run["record"].items() if k != "record_hash"}),
                        "RECORD_CORRUPT", "Local completion record digest mismatch")
            result = response(run=run, jobs=[self.store.job(connection, item) for item in jobs])
        if check_workspace:
            matches = False
            if run["candidate"]:
                try:
                    def same(entry):
                        data, mode = read_file(Path(run["workspace"]), entry["path"])
                        return hashlib.sha256(data).hexdigest() == entry["sha256"] and mode == run["candidate"]["executable"][entry["path"]]
                    matches = all(same(entry) for entry in run["candidate"]["manifest"]["files"])
                except (OSError, HarnessError):
                    matches = False
            result["current_workspace_matches_submitted_files"] = matches
        return result

    def finish(self, run_id: str, request_id: str, *, outcome: str, summary: str = ""):
        require(outcome in {"completed", "partial", "abandoned"} and isinstance(summary, str) and len(summary) <= 16000,
                "INVALID_FINISH", "Outcome is completed, partial or abandoned")
        fingerprint = canonical_hash({"operation": "finish", "outcome": outcome, "summary": summary})
        with self.store.transaction() as connection:
            prior = self.store.replay(connection, run_id, request_id, fingerprint)
            if prior:
                return prior
            run = self.store.run(connection, run_id)
            self._reconcile(connection, run)
            require(run["status"] != "finished", "RUN_CLOSED", "Run is already finished")
            if outcome == "completed":
                require(run["status"] == "active" and time.time() < run["deadline_at"], "RUN_CLOSED", "Expired/blocked Runs can only finish as partial or abandoned")
                require(not run["active_job"] and run["verification"]["status"] == "passed", "VERIFICATION_REQUIRED", "Latest snapshot must pass all mandatory checks")
                job = self.store.job(connection, run["verification"]["job_id"])
                observed = job.get("result") or {}
                require(job["status"] == "completed" and observed.get("status") == "passed" and
                        observed.get("result_hash") == canonical_hash({k: v for k, v in observed.items() if k != "result_hash"}) and
                        observed.get("result_hash") == run["verification"].get("result_hash"), "RESULT_CORRUPT", "Verification result digest/status mismatch")
                require(job["candidate_hash"] == run["candidate"]["candidate_hash"] and job["contract_hash"] == run["contract"]["contract_hash"],
                        "RESULT_BINDING", "Verification does not cover the current candidate/contract")
                validate_candidate(run["candidate"], self.store.directory(run_id) / identifier(run["candidate"]["candidate_id"], "candidate") / "payload")
                require(run["contract"]["verifier"] == verifier_identity(), "VERIFIER_CHANGED", "Verifier code changed after measurement")
            if run["active_job"]:
                job = self.store.job(connection, run["active_job"])
                job.update(status="cancelled", cleanup="pending")
                self.store.save_job(connection, job)
                run.update(active_job=None, verification={"status": "cancelled", "job_id": job["job_id"]})
            record = {"schema_version": "local-verification-record-v1", "run_id": run_id, "outcome": outcome,
                      "assurance": ASSURANCE, "ready": False, "signature": None,
                      "candidate_hash": run["candidate"]["candidate_hash"] if run["candidate"] else None,
                      "contract_hash": run["contract"]["contract_hash"], "verifier": run["contract"]["verifier"],
                      "verification": run["verification"], "model_summary": redact(summary), "finished_at": time.time(),
                      "limitations": run["contract"]["limitations"] + ["This record concerns the submitted snapshot, not later workspace edits."]}
            record["record_hash"] = canonical_hash(record)
            run.update(status="finished", phase="finished", record=record)
            self.store.save_run(connection, run)
            result = response(run_id=run_id, record=record)
            self.store.remember(connection, run_id, request_id, fingerprint, result)
            return result
