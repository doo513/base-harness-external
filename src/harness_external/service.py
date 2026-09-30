"""Model-invoked API. Only verifier workers can record verification observations."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import time
import uuid
import copy

from harness import measurement_v5, common
from harness.common import canonical_bytes, canonical_hash, redact
from . import API_VERSION, ASSURANCE
from .errors import HarnessError, fields, limits, require
from .snapshots import capture, no_links, read_file, validate_candidate
from .store import Store, StopRun, identifier
from . import semantics
from .registry import builtin_registry


LEASE_SECONDS = 20


def verifier_identity() -> dict:
    paths = [Path(measurement_v5.__file__), Path(common.__file__), Path(__file__).with_name("domain.py"), Path(__file__),
             Path(__file__).with_name("worker.py"), Path(__file__).with_name("snapshots.py"), Path(__file__).with_name("store.py"),
             Path(__file__).with_name("develop_manifest.json"), Path(__file__).with_name("errors.py"),
             Path(__file__).with_name("semantics.py"), Path(__file__).with_name("registry.py")]
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
    def __init__(self, state_dir: str | Path | None = None, *, domains=None):
        self.store = Store(state_dir)
        self.domains = domains or builtin_registry()

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

    def start(self, *, domain_id: str, goal: str, workspace: str, parameters: dict, request_id: str, budget: dict | None = None,
              mode: str = "strict", required_checks: list[str] | None = None, constraints: list[str] | None = None):
        require(isinstance(mode, str) and mode in {"strict", "exploratory"}, "MODE_UNSUPPORTED", "Mode is strict or exploratory")
        require(required_checks is None or isinstance(required_checks, list), "INVALID_PARAMETERS", "required_checks must be a list")
        require(mode != "strict" or not required_checks, "INVALID_PARAMETERS", "Strict mode already gates all checks")
        require(constraints is None or isinstance(constraints, list) and len(constraints) <= 100
                and all(isinstance(c, str) and len(c) <= 2000 for c in constraints), "INVALID_PARAMETERS", "Invalid original constraint list")
        module = self.domains.resolve(domain_id)
        source = Path(workspace).expanduser().absolute()
        no_links(source)
        require(source.is_dir(), "WORKSPACE_NOT_FOUND", "Workspace must exist")
        source = source.resolve()
        require(not self.store.root.is_relative_to(source) and not source.is_relative_to(self.store.root),
                "STATE_WORKSPACE_OVERLAP", "Run state and workspace must be disjoint directories")
        request = {"operation": "start", "domain_id": domain_id, "goal": goal,
                   "workspace": str(source), "parameters": parameters, "budget": budget}
        if mode != "strict" or required_checks:
            request.update(mode=mode, required_checks=required_checks or [])
        if constraints:
            request["constraints"] = constraints
        fingerprint = canonical_hash(request)
        with self.store.transaction() as connection:
            prior = self.store.replay(connection, "start", request_id, fingerprint)
            if prior:
                return prior
            run_id = "run_" + uuid.uuid4().hex
            original_intent = semantics.record("intent:" + run_id, 1, {"original_goal": goal, "domain_id": domain_id, "constraints": constraints or []})
            preparation = module.prepare(goal, copy.deepcopy(parameters), verifier_identity(), exploratory=mode == "exploratory", intent=copy.deepcopy(original_intent))
            contract = preparation["contract"]
            if mode == "exploratory":
                contract["rules"]["all_checks_required"] = False
                contract["contract_hash"] = canonical_hash({k: v for k, v in contract.items() if k != "contract_hash"})
            require(contract["original_goal"] == goal and contract["domain_id"] == domain_id, "DOMAIN_RESULT_BINDING", "Domain changed intent identity")
            bound = limits(budget)
            now = time.time()
            run = {"schema_version": API_VERSION, "run_id": run_id, "workspace": str(source), "goal": goal,
                   "contract": contract, "revision": 0, "status": "active", "phase": "awaiting_submission",
                   "created_at": now, "deadline_at": now + bound["timeout_seconds"], "limits": bound,
                   "actions": 0, "verification_attempts": 0, "generation": 0, "candidate": None,
                   "observations": [], "verification": {"status": "not_run"}, "active_job": None, "record": None}
            semantics.ensure(run, mode=mode, required_checks=required_checks, parameters=parameters,
                             questions=preparation["questions"], domain_revision=module.revision, initial_intent=original_intent)
            run["domain_preparation"] = {"status": preparation["status"], "available_operations": preparation["available_operations"]}
            if preparation["status"] == "needs_input":
                run["phase"] = "waiting_input"
            self.store.directory(run_id).mkdir(parents=True, mode=0o700)
            self.store.save_run(connection, run)
            result = response(run_id=run_id, contract=contract, phase=run["phase"], limits=bound,
                              interpretation=run["interpretations"][-1], policy=run["policy"], questions=run["domain_questions"])
            self.store.remember(connection, "start", request_id, fingerprint, result)
            return result

    def observe(self, run_id: str, observation: dict, request_id: str):
        fields(observation, {"note", "references", "kind", "record_id", "task_id", "parent_task_id", "depends_on", "state",
                             "observation_ids", "interpretation_revision", "related_activity_id", "relation", "task_revision"}, {"note"})
        require(not (set(observation) & {"related_activity_id", "relation", "task_revision"}) or bool(observation.get("kind")),
                "ACTIVITY_REQUIRED", "Structured relationships require an activity kind")
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
            if observation.get("kind"):
                require(isinstance(observation["kind"], str) and observation["kind"] in {"note", "hypothesis", "decision", "task"}, "ACTIVITY_KIND", "Unknown activity kind")
                latest = run["interpretations"][-1]["ref"]
                require(observation.get("interpretation_revision", latest["revision"]) == latest["revision"], "STALE_INTERPRETATION", "Activity refers to an older interpretation")
                cited = observation.get("observation_ids", [])
                self._observations_exist(connection, run_id, cited)
                related = observation.get("related_activity_id")
                relation = observation.get("relation")
                if related is not None or relation is not None:
                    require(isinstance(related, str) and any(a["ref"]["id"] == related for a in run["activity"])
                            and isinstance(relation, str) and relation in {"supports", "refutes", "informs", "supersedes"},
                            "ACTIVITY_REFERENCE", "Unknown activity or relationship")
                if observation["kind"] == "task":
                    task_id = semantics.named(observation.get("task_id", ""))
                    known = {t["task_id"] for t in run["logical_tasks"]}
                    previous_task = next((t for t in run["logical_tasks"] if t["task_id"] == task_id), None)
                    parent = observation.get("parent_task_id", previous_task["parent_task_id"] if previous_task else None)
                    deps = observation.get("depends_on", previous_task["depends_on"] if previous_task else [])
                    require(parent is None or isinstance(parent, str) and parent in known, "TASK_REFERENCE", "Unknown parent task")
                    require(isinstance(deps, list) and len(deps) <= 32 and all(isinstance(d, str) and d in known and d != task_id for d in deps), "TASK_REFERENCE", "Unknown/self dependency")
                    require(previous_task is not None or len(known) < 100, "TASK_REFERENCE", "Task limit reached")
                    require(isinstance(observation.get("state", "pending"), str) and observation.get("state", "pending") in {"pending", "settled", "blocked"}, "TASK_STATE", "Invalid reported task state")
                    expected = observation.get("task_revision", 0)
                    require(type(expected) is int and expected == (previous_task.get("revision", 1) if previous_task else 0),
                            "STALE_TASK", "Logical task revision changed")
                    if previous_task:
                        require(parent == previous_task["parent_task_id"] and deps == previous_task["depends_on"], "TASK_REFERENCE", "Task topology is pinned; add a new task for changed dependencies")
                        previous_task.update(revision=expected + 1, state=observation.get("state", "pending"))
                    else:
                        run["logical_tasks"].append({"task_id": task_id, "parent_task_id": parent, "depends_on": deps, "objective": observation["note"],
                                                      "revision": 1, "state": observation.get("state", "pending"), "origin": "caller", "trust": "untrusted"})
                activity_id = "activity:" + run_id + ":" + (semantics.named(observation["record_id"]) if observation.get("record_id") else uuid.uuid4().hex)
                require(not any(a["ref"]["id"] == activity_id for a in run["activity"]), "ACTIVITY_DUPLICATE", "Activity ID already exists")
                run["activity"].append(semantics.record(activity_id, 1, {"kind": observation["kind"], "note_id": item["observation_id"],
                                           "interpretation_ref": latest, "observation_ids": cited,
                                           "task_id": observation.get("task_id"), "task_revision": expected + 1 if observation["kind"] == "task" else None,
                                           "related_activity_id": related, "relation": relation,
                                           "origin": "caller", "trust": "untrusted"}))
            self.store.save_run(connection, run)
            result = response(run_id=run_id, observation=item)
            if observation.get("kind"):
                result["activity"] = run["activity"][-1]
            self.store.remember(connection, run_id, request_id, fingerprint, result)
            return result

    def _observations_exist(self, connection, run_id, ids):
        require(isinstance(ids, list) and len(ids) <= 128 and all(isinstance(i, str) for i in ids), "OBSERVATION_REFERENCE", "Invalid observation references")
        known = {item["observation_id"] for item in self.store.measurements(connection, run_id)}
        require(set(ids) <= known, "OBSERVATION_REFERENCE", "Observation is not a verifier measurement owned by this Run")

    def _module(self, run):
        module = self.domains.resolve(run["domain_module"]["id"])
        require(module.revision == run["domain_module"]["revision"], "DOMAIN_MODULE_CHANGED", "Pinned Domain module revision changed")
        return module

    def _activities_exist(self, run, ids):
        require(isinstance(ids, list) and len(ids) <= 100 and all(isinstance(i, str) for i in ids)
                and set(ids) <= {a["ref"]["id"] for a in run["activity"]}, "ACTIVITY_REFERENCE", "Activity does not belong to this Run")

    def revise(self, run_id: str, proposal: dict, request_id: str):
        fields(proposal, {"expected_revision", "goal_summary", "parameters", "assumptions", "open_questions", "observation_ids", "activity_ids"}, {"expected_revision"})
        fingerprint = canonical_hash({"operation": "revise", "proposal": proposal})
        with self.store.transaction() as connection:
            prior = self.store.replay(connection, run_id, request_id, fingerprint)
            if prior:
                return prior
            run = self.store.run(connection, run_id)
            self._admit(connection, run)
            require(not run["active_job"], "VERIFICATION_ACTIVE", "Wait for the owned verification before revising")
            previous = run["interpretations"][-1]
            require(len(run["interpretations"]) < 100, "INTERPRETATION_LIMIT", "Interpretation history limit reached")
            require(type(proposal["expected_revision"]) is int and proposal["expected_revision"] == previous["ref"]["revision"],
                    "STALE_INTERPRETATION", "Interpretation was revised by another request")
            patch = proposal.get("parameters", {})
            require(isinstance(patch, dict), "INVALID_PARAMETERS", "Parameter revision must be an object")
            parameters = {**copy.deepcopy(previous["parameters"]), **copy.deepcopy(patch)}
            prepared = self._module(run).prepare(run["goal"], parameters, run["contract"]["verifier"], exploratory=run["policy"]["mode"] == "exploratory", intent=copy.deepcopy(run["intent"]))
            require(prepared["contract"]["original_goal"] == run["goal"] and prepared["contract"]["domain_id"] == run["domain_module"]["id"],
                    "DOMAIN_RESULT_BINDING", "Domain revision changed the original intent identity")
            summary = proposal.get("goal_summary", previous["goal_summary"])
            require(isinstance(summary, str) and 0 < len(summary) <= 16000, "INVALID_INTERPRETATION", "Bounded goal summary required")
            assumptions = proposal.get("assumptions", previous["assumptions"])
            require(isinstance(assumptions, list) and len(assumptions) <= 100, "INVALID_INTERPRETATION", "Too many assumptions")
            for assumption in assumptions:
                fields(assumption, {"id", "statement", "observation_ids"}, {"id", "statement"})
                semantics.named(assumption["id"])
                require(isinstance(assumption["statement"], str) and len(assumption["statement"]) <= 16000, "INVALID_INTERPRETATION", "Invalid assumption")
                self._observations_exist(connection, run_id, assumption.get("observation_ids", []))
            require(len({item["id"] for item in assumptions}) == len(assumptions), "INVALID_INTERPRETATION", "Duplicate assumption IDs")
            questions = proposal.get("open_questions", [])
            require(isinstance(questions, list) and len(questions) <= 100 and all(isinstance(q, str) and len(q) <= 2000 for q in questions),
                    "INVALID_INTERPRETATION", "Invalid open questions")
            cited = proposal.get("observation_ids", [])
            self._observations_exist(connection, run_id, cited)
            activity_ids = proposal.get("activity_ids", [])
            self._activities_exist(run, activity_ids)
            interpretation = semantics.record(previous["ref"]["id"], previous["ref"]["revision"] + 1,
                            {"goal_summary": summary, "parameters": parameters, "assumptions": assumptions,
                             "open_questions": questions, "observation_ids": cited, "activity_ids": activity_ids,
                             "origin": "caller", "trust": "untrusted"})
            semantics.sync_checks(run, prepared["contract"]["checks"], interpretation["ref"])
            old_inputs = run["contract"]["inputs"]
            run["interpretations"].append(interpretation)
            run["contract"] = prepared["contract"]
            if run["policy"]["mode"] == "exploratory":
                run["contract"]["rules"]["all_checks_required"] = False
                run["contract"]["contract_hash"] = canonical_hash({k: v for k, v in run["contract"].items() if k != "contract_hash"})
            if old_inputs != run["contract"]["inputs"]:
                run["candidate"] = None
            run.update(domain_questions=prepared["questions"], phase="waiting_input" if prepared["status"] == "needs_input" else "awaiting_submission",
                       verification={"status": "not_run"})
            run["domain_preparation"] = {"status": prepared["status"], "available_operations": prepared["available_operations"]}
            self.store.save_run(connection, run)
            result = response(run_id=run_id, interpretation=interpretation, phase=run["phase"], questions=run["domain_questions"])
            self.store.remember(connection, run_id, request_id, fingerprint, result)
            return result

    def register_check(self, run_id: str, proposal: dict, request_id: str):
        fields(proposal, {"check_id", "parameters", "expected_revision", "interpretation_revision"}, {"check_id", "parameters", "interpretation_revision"})
        fingerprint = canonical_hash({"operation": "check", "proposal": proposal})
        with self.store.transaction() as connection:
            prior = self.store.replay(connection, run_id, request_id, fingerprint)
            if prior:
                return prior
            run = self.store.run(connection, run_id)
            self._admit(connection, run)
            require(not run["active_job"], "VERIFICATION_ACTIVE", "Cannot change checks while verification owns the Run")
            latest = run["interpretations"][-1]["ref"]
            require(type(proposal["interpretation_revision"]) is int and proposal["interpretation_revision"] == latest["revision"], "STALE_INTERPRETATION", "Check proposal refers to an older interpretation")
            key = semantics.named(proposal["check_id"])
            require(not key.startswith(("file-", "command-")), "CHECK_ID_RESERVED", "Domain-generated check IDs are reserved")
            old = next((item for item in semantics.current_checks(run) if item["check_id"] == key), None)
            require(type(proposal.get("expected_revision", 0)) is int and proposal.get("expected_revision", 0) == (old["ref"]["revision"] if old else 0), "STALE_CHECK", "Check revision changed")
            require(key not in run["gate_bindings"], "GATE_POLICY_CHANGED", "Pinned gate checks cannot be revised")
            require(len(run["check_records"]) < 256, "CHECK_LIMIT", "Check revision limit reached")
            parameters = self._module(run).normalize_check(copy.deepcopy(proposal["parameters"]), run["contract"])
            created = semantics.check_record(run_id, key, old["ref"]["revision"] + 1 if old else 1, parameters, latest)
            run["check_records"].append(created)
            if run["policy"]["rule"] == "all_registered_checks" or key in run["policy"]["required_check_ids"]:
                run["gate_bindings"][key] = created["ref"]
            run["verification"] = {"status": "not_run"}
            self.store.save_run(connection, run)
            result = response(run_id=run_id, check=created, gated=key in run["gate_bindings"])
            self.store.remember(connection, run_id, request_id, fingerprint, result)
            return result

    def _assessment_record(self, connection, run, assessment):
        fields(assessment, {"status", "summary", "uncertainties", "cited_observation_ids", "interpretation_revision"},
               {"status", "summary", "uncertainties", "cited_observation_ids", "interpretation_revision"})
        require(isinstance(assessment["status"], str) and assessment["status"] in {"satisfied", "partial", "unsolved", "not_assessed"} and isinstance(assessment["summary"], str)
                and len(assessment["summary"]) <= 16000, "ASSESSMENT_INVALID", "Invalid model assessment")
        require(isinstance(assessment["uncertainties"], list) and len(assessment["uncertainties"]) <= 100
                and all(isinstance(u, str) and len(u) <= 2000 for u in assessment["uncertainties"]), "ASSESSMENT_INVALID", "Invalid uncertainty list")
        latest = run["interpretations"][-1]["ref"]
        require(type(assessment["interpretation_revision"]) is int and assessment["interpretation_revision"] == latest["revision"], "STALE_INTERPRETATION", "Assessment refers to an older interpretation")
        self._observations_exist(connection, run["run_id"], assessment["cited_observation_ids"])
        require(len(run["assessments"]) < 100, "ASSESSMENT_LIMIT", "Assessment limit reached")
        created = semantics.record("assessment:" + run["run_id"] + ":" + uuid.uuid4().hex, 1,
                  {**redact(assessment), "interpretation_ref": latest, "subject": run["candidate"]["subject"] if run["candidate"] else None,
                   "origin": "caller", "trust": "untrusted"})
        run["assessments"].append(created)
        return created

    def assess(self, run_id: str, assessment: dict, request_id: str):
        fingerprint = canonical_hash({"operation": "assess", "assessment": assessment})
        with self.store.transaction() as connection:
            prior = self.store.replay(connection, run_id, request_id, fingerprint)
            if prior:
                return prior
            run = self.store.run(connection, run_id)
            self._admit(connection, run)
            created = self._assessment_record(connection, run, assessment)
            self.store.save_run(connection, run)
            result = response(run_id=run_id, assessment=created)
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
            require("submit" in run["domain_preparation"]["available_operations"], "NEEDS_INPUT", "Domain preparation still needs an input scope; revise this Run")
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
            require("verify" in run["domain_preparation"]["available_operations"], "NEEDS_INPUT", "Domain preparation has not admitted measurements")
            require(run["contract"]["verifier"] == verifier_identity(), "VERIFIER_CHANGED", "Verifier code changed; start a new Run with the new version")
            job_id = "job_" + uuid.uuid4().hex
            now = time.time()
            timeout = 45 + sum(check.get("timeout_seconds", 1) + 10 for check in run["contract"]["checks"])
            job = {"job_id": job_id, "run_id": run_id, "status": "queued", "heartbeat_at": now,
                   "deadline_at": min(run["deadline_at"], now + timeout), "generation": run["generation"],
                   "contract_hash": run["contract"]["contract_hash"], "candidate_hash": run["candidate"]["candidate_hash"],
                   "cleanup": "not_started", "result": None}
            job.update(checks=copy.deepcopy(semantics.current_checks(run)), candidate=copy.deepcopy(run["candidate"]),
                       interpretation_ref=run["interpretations"][-1]["ref"], policy_ref=run["policy"]["ref"],
                       completed_checks=0, total_checks=len(semantics.current_checks(run)))
            require(job["total_checks"] > 0, "CHECKS_REQUIRED", "Register a measurement before verification")
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
                return response(run_id=run_id, job=job, measurements=self.store.measurements(connection, run_id, job_id))
            if run["record"]:
                require(run["record"].get("record_hash") == canonical_hash({k: v for k, v in run["record"].items() if k != "record_hash"}),
                        "RECORD_CORRUPT", "Local completion record digest mismatch")
            measured = self.store.measurements(connection, run_id)
            result = response(run=run, jobs=[self.store.job(connection, item) for item in jobs], measurements=measured,
                              closeout={"measurement": run["verification"], "assessment": semantics.assessment_view(run),
                                        "gates": semantics.gates(run, measured), "lifecycle": "closed" if run["status"] == "finished" else "waiting_input" if run["domain_questions"] and not run["active_job"] else run["status"],
                                        "termination_reason": run.get("termination_reason")})
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

    def finish(self, run_id: str, request_id: str, *, outcome: str, summary: str = "", assessment: dict | None = None):
        require(outcome in {"completed", "partial", "abandoned"} and isinstance(summary, str) and len(summary) <= 16000,
                "INVALID_FINISH", "Outcome is completed, partial or abandoned")
        request = {"operation": "finish", "outcome": outcome, "summary": summary}
        if assessment is not None:
            request["assessment"] = assessment
        fingerprint = canonical_hash(request)
        with self.store.transaction() as connection:
            prior = self.store.replay(connection, run_id, request_id, fingerprint)
            if prior:
                return prior
            run = self.store.run(connection, run_id)
            self._reconcile(connection, run)
            require(run["status"] != "finished", "RUN_CLOSED", "Run is already finished")
            if assessment is not None:
                self._assessment_record(connection, run, assessment)
            measured = self.store.measurements(connection, run_id)
            gate_results = semantics.gates(run, measured)
            if outcome == "completed":
                require(run["status"] == "active" and time.time() < run["deadline_at"], "RUN_CLOSED", "Expired/blocked Runs can only finish as partial or abandoned")
                require(not run["active_job"] and "finish_completed" in run["domain_preparation"]["available_operations"] and gate_results["status"] in {"passed", "not_required"},
                        "VERIFICATION_REQUIRED", "Pinned completion gates have not been satisfied")
                if run["policy"]["mode"] == "strict":
                    require(run["verification"]["status"] == "passed", "VERIFICATION_REQUIRED", "Strict policy requires all measurements to pass")
                job = self.store.job(connection, run["verification"]["job_id"]) if run["verification"].get("job_id") else None
                observed = (job.get("result") or {}) if job else {}
                if run["policy"]["mode"] == "strict":
                    require(job is not None and job["status"] == "completed", "VERIFICATION_REQUIRED", "Strict policy requires a completed verifier job")
                if observed:
                    require(
                        observed.get("result_hash") == canonical_hash({k: v for k, v in observed.items() if k != "result_hash"}) and
                        observed.get("result_hash") == run["verification"].get("result_hash"), "RESULT_CORRUPT", "Verification result digest/status mismatch")
                    require(job["candidate_hash"] == run["candidate"]["candidate_hash"] and job["contract_hash"] == run["contract"]["contract_hash"],
                        "RESULT_BINDING", "Verification does not cover the current candidate/contract")
                if run["candidate"]:
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
            reason = run.get("termination_reason") or ("abandoned" if outcome == "abandoned" else "requested")
            record.update(semantic_schema_version=semantics.SEMANTIC_VERSION, lifecycle="closed", termination_reason=reason,
                          intent_ref=run["intent"]["ref"], interpretation_ref=run["interpretations"][-1]["ref"], policy_ref=run["policy"]["ref"],
                          measurement=copy.deepcopy(run["verification"]), assessment=semantics.assessment_view(run), gates=gate_results,
                          unresolved_work=[task["task_id"] for task in run["logical_tasks"] if task["state"] != "settled"])
            record["unresolved_verification_jobs"] = [row[0] for row in connection.execute("SELECT job_id FROM jobs WHERE run_id=?", (run_id,))
                                                      if self.store.job(connection, row[0]).get("cleanup") not in {"complete", "not_started"}]
            record["record_hash"] = canonical_hash(record)
            run.update(status="finished", phase="finished", record=record, termination_reason=reason)
            self.store.save_run(connection, run)
            result = response(run_id=run_id, record=record)
            self.store.remember(connection, run_id, request_id, fingerprint, result)
            return result
