"""Model-invoked API. Only verifier workers can record verification observations."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import time
import uuid
import copy
import tempfile
from contextlib import contextmanager

from harness.common import canonical_bytes, canonical_hash, redact
from . import API_VERSION, ASSURANCE
from .errors import HarnessError, fields, integer, limits, require
from .snapshots import capture, no_links, read_file, validate_candidate
from .store import Store, StopRun, identifier
from . import semantics
from .registry import builtin_registry
from .adapter_registry import builtin_adapter_registry
from .check_preparation import AdapterCheckPreparation
from . import queries, maintenance
from . import acceptance, evidence, observation_links
from .identity import verifier_identity


LEASE_SECONDS = 20


def response(**data) -> dict:
    return {"api_version": API_VERSION, "assurance": ASSURANCE, "ready": False, **data}


class Harness:
    def __init__(self, state_dir: str | Path | None = None, *, domains=None, policy_root=None, adapters=None):
        self.store = Store(state_dir)
        self.adapters = adapters if adapters is not None else builtin_adapter_registry()
        self.domains = domains or builtin_registry(check_preparation=AdapterCheckPreparation(self.adapters))
        self.policies = acceptance.PolicyRegistry(policy_root)

    @staticmethod
    def _domain_preparation_view(preparation):
        result = {"status": preparation["status"], "available_operations": preparation["available_operations"]}
        if "coverage_inventory_summary" in preparation:
            # Forward-only metadata. It never grants an operation or changes a gate.
            result["coverage_inventory"] = copy.deepcopy(preparation["coverage_inventory_summary"])
        if "validation_perspectives" in preparation:
            result["validation_perspectives"] = copy.deepcopy(preparation["validation_perspectives"])
        return result

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

    def _admit(self, connection, run, *, verification=False, request=None):
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
            message = "Harness limit reached; hand off to the user. External model execution is not controlled."
            run.update(status="blocked", phase="handoff", termination_reason=reason)
            if run.get("active_job"):
                job = self.store.job(connection, run["active_job"])
                job.update(status="cancelled", cleanup="complete" if job["status"] == "queued" else "pending")
                self.store.save_job(connection, job)
                run["active_job"] = None
            self.store.save_run(connection, run)
            if request:
                scope, request_id, fingerprint = request
                self.store.remember_error(connection, scope, request_id, fingerprint, reason, message, terminal=True)
            raise StopRun(reason, message)
        run["actions"] += 1

    def _refresh(self, connection, run):
        self._reconcile(connection, run)
        if run["status"] == "active" and time.time() >= run["deadline_at"]:
            run.update(status="blocked", phase="handoff", termination_reason="DEADLINE_EXCEEDED")
            self.store.save_run(connection, run)

    def _mutation_snapshot(self, run_id, request_id, fingerprint, *, verification=False):
        """Admit briefly, as submit() does; preparation must not hold a writer."""
        with self.store.transaction() as connection:
            prior = self.store.replay(connection, run_id, request_id, fingerprint)
            if prior:
                return prior, None
            run = self.store.run(connection, run_id)
            self._admit(connection, run, verification=verification, request=(run_id, request_id, fingerprint))
            # Successful admission changes only this detached copy. The action
            # is charged at publication; terminal limit errors are durable now.
            return None, run

    def _publish_prepared_run(self, run, expected_revision, request_id, fingerprint, result):
        run_id = run["run_id"]
        with self.store.transaction() as connection:
            prior = self.store.replay(connection, run_id, request_id, fingerprint)
            if prior:
                return prior
            current = self.store.run(connection, run_id)
            self._admit(connection, current, request=(run_id, request_id, fingerprint))
            require(current["revision"] == expected_revision and not current["active_job"],
                    "RUN_CONFLICT", "Run changed during preparation; inspect its current revision before retrying")
            run["actions"] = current["actions"]
            self.store.save_run(connection, run)
            self.store.remember(connection, run_id, request_id, fingerprint, result)
            return result

    def _refresh_for_read(self, run_id):
        with self.store.transaction(write=False) as connection:
            run = self.store.run(connection, run_id)
            now = time.time()
            expired = run["status"] == "active" and now >= run["deadline_at"]
            if run.get("active_job"):
                job = self.store.job(connection, run["active_job"])
                expired = expired or (job["status"] in {"queued", "running"} and
                                      (now > job["deadline_at"] or now - job["heartbeat_at"] > LEASE_SECONDS))
        if expired:
            with self.store.transaction() as connection:
                self._refresh(connection, self.store.run(connection, run_id))

    def _validate_checks(self, run, contract):
        module = self._module(run)
        observation_links.validate(contract.get("observation_links"), [item["check_id"] for item in semantics.current_checks(run)])
        for item in semantics.current_checks(run):
            parameters = copy.deepcopy(item["spec"]["parameters"])
            if parameters["kind"] == "command":
                parameters["timeout_seconds"] = item["spec"]["timeoutMs"] // 1000
            try:
                normalized = module.normalize_check(parameters, copy.deepcopy(contract))
                require(semantics.check_equivalent(item, normalized), "CHECK_SCOPE_CONFLICT", "Check normalization changed")
            except HarnessError as error:
                raise HarnessError("CHECK_SCOPE_CONFLICT", "Check " + item["check_id"] +
                                   " is incompatible with this interpretation; revise or retire the advisory check first (" + error.code + ")") from error

    @contextmanager
    def _read_run(self, run_id):
        with self.store.transaction(write=False) as connection:
            run = self.store.current(connection, run_id)
            now = time.time()
            expired = run["status"] == "active" and now >= run["deadline_at"]
            if run.get("active_job"):
                job = self.store.job(connection, run["active_job"])
                expired = expired or (job["status"] in {"queued", "running"} and
                                      (now > job["deadline_at"] or now - job["heartbeat_at"] > LEASE_SECONDS))
            if not expired:
                yield connection, run
                return
        with self.store.transaction() as connection:
            self._refresh(connection, self.store.run(connection, run_id))
        with self.store.transaction(write=False) as connection:
            yield connection, self.store.current(connection, run_id)

    def start(self, *, domain_id: str, goal: str, workspace: str, parameters: dict, request_id: str, budget: dict | None = None,
              mode: str | None = None, required_checks: list[str] | None = None, deferred_checks: list[str] | None = None,
              constraints: list[str] | None = None, provenance: dict | None = None, policy_id: str | None = None,
              predecessor_run_id: str | None = None, policy_change_reason: str | None = None, capture_baseline: bool = False,
              continuation_reason: str | None = None):
        # Request identity does not depend on mutable registry contents. A replay
        # returns the originally selected policy, even after operator revision.
        requested_mode = mode
        require(not (continuation_reason is not None and policy_change_reason is not None), "TRANSITION_REASON", "Choose one transition reason")
        transition_kind = "runtime_transition" if continuation_reason is not None else "policy_change"
        if continuation_reason is not None:
            policy_change_reason = continuation_reason
        require(type(capture_baseline) is bool, "INVALID_PARAMETERS", "capture_baseline must be boolean")
        require(mode is None or isinstance(mode, str) and mode in {"strict", "exploratory", "acceptance"},
                "MODE_UNSUPPORTED", "Mode is strict, exploratory or configured acceptance")
        require(required_checks is None or isinstance(required_checks, list), "INVALID_PARAMETERS", "required_checks must be a list")
        require(deferred_checks is None or isinstance(deferred_checks, list), "INVALID_PARAMETERS", "deferred_checks must be a list")
        require(len(required_checks or []) + len(deferred_checks or []) <= 256, "INVALID_PARAMETERS", "Too many gate IDs")
        require(mode != "strict" or not (required_checks or deferred_checks), "INVALID_PARAMETERS", "Strict mode already gates all checks")
        semantics.provenance(provenance)
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
        if mode not in (None, "strict") or required_checks:
            request.update(mode=mode, required_checks=required_checks or [])
        if constraints:
            request["constraints"] = constraints
        if deferred_checks:
            request["deferred_checks"] = deferred_checks
        if provenance is not None:
            request["provenance"] = provenance
        if policy_id is not None:
            request["policy_id"] = policy_id
        if capture_baseline:
            request["capture_baseline"] = True
        if predecessor_run_id is not None or policy_change_reason is not None:
            require(predecessor_run_id is not None and isinstance(policy_change_reason, str) and 0 < len(policy_change_reason) <= 2000,
                    "POLICY_CHANGE_LINK", "A policy-change Run needs its predecessor ID and a bounded reason")
            request.update(predecessor_run_id=predecessor_run_id, policy_change_reason=policy_change_reason)
            if continuation_reason is not None:
                request["continuation_reason"] = continuation_reason
        fingerprint = canonical_hash(request)
        with self.store.transaction(write=False) as connection:
            prior = self.store.replay(connection, "start", request_id, fingerprint)
            if prior:
                return prior
            selected = self.policies.select(domain_id, policy_id)
            mode = requested_mode or ("acceptance" if selected else "strict")
            require(mode != "strict" or not (required_checks or deferred_checks), "INVALID_PARAMETERS", "Strict mode already gates all checks")
            require((selected is not None) == (mode == "acceptance"), "POLICY_MODE_CONFLICT",
                    "Configured policies use acceptance mode; caller strict/exploratory mode cannot override them")
            require(not selected or not (required_checks or deferred_checks), "POLICY_MODE_CONFLICT",
                    "Configured policies, not the caller, define mandatory check IDs")
            if selected:
                require(not self.policies.root.is_relative_to(source) and not source.is_relative_to(self.policies.root),
                        "POLICY_WORKSPACE_OVERLAP", "Policy configuration must be outside the task workspace")
            predecessor = None
            if predecessor_run_id:
                predecessor = self.store.run(connection, predecessor_run_id)
                require(predecessor["workspace"] == str(source) and predecessor["goal"] == goal
                        and predecessor["intent"]["domain_id"] == domain_id and predecessor["status"] != "active"
                        and predecessor["intent"]["constraints"] == (constraints or []),
                        "POLICY_CHANGE_LINK", "Policy changes require the same task and a terminal predecessor Run")
            run_id = "run_" + uuid.uuid4().hex
            original_intent = semantics.record("intent:" + run_id, 1, {"original_goal": goal, "domain_id": domain_id, "constraints": constraints or []})
            if selected:
                require(callable(getattr(module, "prepare_acceptance", None)), "DOMAIN_POLICY_UNSUPPORTED", "Domain has no acceptance policy compiler")
                preparation = module.prepare_acceptance(goal, copy.deepcopy(parameters), verifier_identity(),
                                                        policy=copy.deepcopy(selected["definition"]), intent=copy.deepcopy(original_intent))
                acceptance.validate_preparation(preparation, selected["definition"])
                required_checks = selected["definition"]["required_check_ids"]
            else:
                preparation = module.prepare(goal, copy.deepcopy(parameters), verifier_identity(), exploratory=mode == "exploratory", intent=copy.deepcopy(original_intent))
            contract = preparation["contract"]
            if mode != "strict":
                contract["rules"]["all_checks_required"] = False
                contract["contract_hash"] = canonical_hash({k: v for k, v in contract.items() if k != "contract_hash"})
            require(contract["original_goal"] == goal and contract["domain_id"] == domain_id, "DOMAIN_RESULT_BINDING", "Domain changed intent identity")
            observation_links.validate(contract.get("observation_links"), [check.get("check_id") for check in contract["checks"]])
            adapter_implementations = self.adapters.pin_implementations(contract["checks"])
            bound = limits(budget)
        require(not capture_baseline or bool(contract["inputs"]), "NEEDS_INPUT", "Declare baseline input files before capturing the original")
        # File I/O must not block checkpoints in unrelated Runs. Only publish the
        # captured files while committing the idempotent start request.
        with tempfile.TemporaryDirectory(prefix=".start_", dir=self.store.root) as temporary, maintenance.capture_lease(temporary):
            staging = Path(temporary)
            binding = acceptance.pin(selected, staging) if selected else None
            baseline = None
            if capture_baseline:
                overlays = {path: acceptance.bundle_payload(binding, staging) for path in binding["definition"]["bundle"]["files"]} if binding else {}
                baseline = capture(source, contract["inputs"], staging, overlays=overlays)
            now = time.time()
            run = {"schema_version": API_VERSION, "storage_schema": "run-store-v2", "run_id": run_id, "workspace": str(source), "goal": goal,
                   "adapter_implementations": adapter_implementations,
                   "contract": contract, "revision": 0, "status": "active", "phase": "awaiting_submission",
                   "created_at": now, "deadline_at": now + bound["timeout_seconds"], "limits": bound,
                   "actions": 0, "verification_attempts": 0, "generation": 0, "candidate": None,
                   "observations": [], "verification": {"status": "not_run"}, "active_job": None, "record": None}
            if binding:
                run["acceptance"] = binding
            if capture_baseline:
                run["baseline"] = baseline
            if predecessor:
                run["predecessor"] = {"run_id": predecessor_run_id, "policy_ref": predecessor["policy"]["ref"],
                                      "kind": transition_kind,
                                      "reason": policy_change_reason, "previous_usage": {"actions": predecessor["actions"],
                                      "verification_attempts": predecessor["verification_attempts"]},
                                      "approval": binding["authority"] if binding else {"status": "not_established"}}
            semantics.ensure(run, mode=mode, required_checks=required_checks, parameters=parameters,
                             deferred_checks=deferred_checks, domain_identity=self.domains.identity(domain_id), policy_provenance=provenance,
                             questions=preparation["questions"], domain_revision=module.revision, initial_intent=original_intent,
                             acceptance_binding=binding)
            run["domain_preparation"] = self._domain_preparation_view(preparation)
            if preparation["status"] == "needs_input":
                run["phase"] = "waiting_input"
            result = response(run_id=run_id, contract=contract, phase=run["phase"], limits=bound,
                              interpretation=run["interpretations"][-1], policy=run["policy"], questions=run["domain_questions"],
                              domain_preparation=run["domain_preparation"],
                              acceptance=acceptance.view(binding),
                              baseline={key: run["baseline"][key] for key in ("candidate_id", "candidate_hash", "subject")} if capture_baseline else None)
            published = []
            try:
                with self.store.transaction() as connection:
                    prior = self.store.replay(connection, "start", request_id, fingerprint)
                    if prior:
                        return prior
                    require(contract["verifier"] == verifier_identity(), "VERIFIER_CHANGED", "Verifier changed while starting the Run")
                    require(run["domain_module"] == self.domains.identity(domain_id), "DOMAIN_MODULE_CHANGED", "Domain changed while starting the Run")
                    directory = self.store.directory(run_id)
                    directory.mkdir(parents=True, mode=0o700)
                    for snapshot in ([binding["bundle"]] if binding else []) + ([baseline] if baseline else []):
                        target = directory / snapshot["candidate_id"]
                        os.rename(staging / snapshot["candidate_id"], target)
                        published.append(target)
                    self.store.save_run(connection, run)
                    self.store.remember(connection, "start", request_id, fingerprint, result)
            except BaseException:
                for target in published:
                    os.rename(target, staging / target.name)
                raise
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
            self._admit(connection, run, request=(run_id, request_id, fingerprint))
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
        require(isinstance(ids, list) and len(ids) <= 128 and all(isinstance(i, str) for i in ids)
                and len(set(ids)) == len(ids), "OBSERVATION_REFERENCE", "Invalid observation references")
        if not ids:
            return []
        rows = connection.execute("SELECT data FROM measurements WHERE run_id=? AND observation_id IN (" +
                                  ",".join("?" for _ in ids) + ")", [run_id, *ids])
        known = {item["observation_id"]: item for item in self.store._measurement_rows(rows, run_id)}
        rows = connection.execute("SELECT data FROM case_observations WHERE run_id=? AND observation_id IN (" +
                                  ",".join("?" for _ in ids) + ")", [run_id, *ids])
        for item in self.store._case_rows(rows, run_id):
            self.store.validate_case_binding(item, self.store.job(connection, item["job_id"]))
            known[item["observation_id"]] = item
        require(set(ids) <= set(known), "OBSERVATION_REFERENCE", "Observation is not a verifier measurement owned by this Run")
        return [known[item] for item in ids]

    def _module(self, run):
        module = self.domains.resolve(run["domain_module"]["id"])
        require(module.revision == run["domain_module"]["revision"], "DOMAIN_MODULE_CHANGED", "Pinned Domain module revision changed")
        require("identity_hash" in run["domain_module"], "DOMAIN_IDENTITY_REQUIRED", "Historical Run has no Domain implementation identity; start a new Run to mutate or verify")
        require(self.domains.identity(module.domain_id) == run["domain_module"], "DOMAIN_MODULE_CHANGED", "Domain implementation or configuration changed")
        return module

    def _activities_exist(self, run, ids):
        require(isinstance(ids, list) and len(ids) <= 100 and all(isinstance(i, str) for i in ids)
                and set(ids) <= {a["ref"]["id"] for a in run["activity"]}, "ACTIVITY_REFERENCE", "Activity does not belong to this Run")

    def revise(self, run_id: str, proposal: dict, request_id: str):
        fields(proposal, {"expected_revision", "goal_summary", "parameters", "assumptions", "open_questions", "observation_ids", "activity_ids"}, {"expected_revision"})
        fingerprint = canonical_hash({"operation": "revise", "proposal": proposal})
        prior, run = self._mutation_snapshot(run_id, request_id, fingerprint)
        if prior:
            return prior
        expected_revision = run["revision"]
        with self.store.transaction(write=False) as connection:
            require(not run["active_job"], "VERIFICATION_ACTIVE", "Wait for the owned verification before revising")
            previous = run["interpretations"][-1]
            require(len(run["interpretations"]) < 100, "INTERPRETATION_LIMIT", "Interpretation history limit reached")
            require(type(proposal["expected_revision"]) is int and proposal["expected_revision"] == previous["ref"]["revision"],
                    "STALE_INTERPRETATION", "Interpretation was revised by another request")
            patch = proposal.get("parameters", {})
            require(isinstance(patch, dict), "INVALID_PARAMETERS", "Parameter revision must be an object")
            parameters = {**copy.deepcopy(previous["parameters"]), **copy.deepcopy(patch)}
            module = self._module(run)
            if run.get("acceptance"):
                acceptance.validate(run["acceptance"], self.store.directory(run_id))
                prepared = module.prepare_acceptance(run["goal"], parameters, run["contract"]["verifier"],
                                                     policy=copy.deepcopy(run["acceptance"]["definition"]), intent=copy.deepcopy(run["intent"]))
                acceptance.validate_preparation(prepared, run["acceptance"]["definition"])
            else:
                prepared = module.prepare(run["goal"], parameters, run["contract"]["verifier"], exploratory=run["policy"]["mode"] == "exploratory", intent=copy.deepcopy(run["intent"]))
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
            # Early v0.2 records duplicated structured Domain questions here.
            # Those are regenerated in domain_questions; caller questions persist.
            questions = proposal.get("open_questions", [q for q in previous["open_questions"] if isinstance(q, str)])
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
            self._validate_checks(run, prepared["contract"])
            run["adapter_implementations"] = self.adapters.pin_implementations(
                [item["spec"]["parameters"] for item in semantics.current_checks(run)], run.get("adapter_implementations"))
            old_inputs = run["contract"]["inputs"]
            run["interpretations"].append(interpretation)
            run["contract"] = prepared["contract"]
            if run["policy"]["mode"] != "strict":
                run["contract"]["rules"]["all_checks_required"] = False
                run["contract"]["contract_hash"] = canonical_hash({k: v for k, v in run["contract"].items() if k != "contract_hash"})
            if old_inputs != run["contract"]["inputs"]:
                run["candidate"] = None
            run.update(domain_questions=prepared["questions"], phase="waiting_input" if prepared["status"] == "needs_input" else "awaiting_submission",
                       verification={"status": "not_run"})
            run["domain_preparation"] = self._domain_preparation_view(prepared)
            result = response(run_id=run_id, interpretation=interpretation, phase=run["phase"],
                              questions=run["domain_questions"], domain_preparation=run["domain_preparation"])
        return self._publish_prepared_run(run, expected_revision, request_id, fingerprint, result)

    def register_check(self, run_id: str, proposal: dict, request_id: str):
        fields(proposal, {"check_id", "parameters", "expected_revision", "interpretation_revision", "provenance"}, {"check_id", "parameters", "interpretation_revision"})
        fingerprint = canonical_hash({"operation": "check", "proposal": proposal})
        prior, run = self._mutation_snapshot(run_id, request_id, fingerprint)
        if prior:
            return prior
        expected_revision = run["revision"]
        with self.store.transaction(write=False):
            require(not run["active_job"], "VERIFICATION_ACTIVE", "Cannot change checks while verification owns the Run")
            latest = run["interpretations"][-1]["ref"]
            require(type(proposal["interpretation_revision"]) is int and proposal["interpretation_revision"] == latest["revision"], "STALE_INTERPRETATION", "Check proposal refers to an older interpretation")
            key = semantics.named(proposal["check_id"])
            require(not key.startswith(("file-", "command-", "domain.")), "CHECK_ID_RESERVED", "Domain-generated check IDs are reserved")
            old = semantics.latest_checks(run).get(key)
            require(type(proposal.get("expected_revision", 0)) is int and proposal.get("expected_revision", 0) == (old["ref"]["revision"] if old else 0), "STALE_CHECK", "Check revision changed")
            require(key not in run["gate_bindings"], "GATE_POLICY_CHANGED", "Pinned gate checks cannot be revised")
            require(len(run["check_records"]) < 256, "CHECK_LIMIT", "Check revision limit reached")
            parameters = self._module(run).normalize_check(copy.deepcopy(proposal["parameters"]), run["contract"])
            source_provenance = semantics.provenance(proposal.get("provenance"))
            created = semantics.check_record(run_id, key, semantics.next_check_revision(run, key), parameters, latest,
                                             role="mandatory" if run["policy"]["rule"] == "all_registered_checks" or key in run["policy"]["required_check_ids"] else "exploratory",
                                             author=source_provenance["declared_author"], approval_claim=source_provenance["approval"],
                                             generator={"kind": "domain_normalization", "module": run["domain_module"]})
            run["adapter_implementations"] = self.adapters.pin_implementations([parameters], run.get("adapter_implementations"))
            run["check_records"].append(created)
            if run["policy"]["rule"] == "all_registered_checks" or key in run["policy"]["required_check_ids"]:
                run["gate_bindings"][key] = created["ref"]
            run["verification"] = {"status": "not_run"}
            result = response(run_id=run_id, check=created, gated=key in run["gate_bindings"])
        return self._publish_prepared_run(run, expected_revision, request_id, fingerprint, result)

    def retire_check(self, run_id: str, proposal: dict, request_id: str):
        fields(proposal, {"check_id", "expected_revision", "interpretation_revision", "reason"},
               {"check_id", "expected_revision", "interpretation_revision", "reason"})
        require(isinstance(proposal["reason"], str) and 0 < len(proposal["reason"]) <= 2000, "INVALID_PARAMETERS", "A bounded retirement reason is required")
        fingerprint = canonical_hash({"operation": "retire-check", "proposal": proposal})
        with self.store.transaction() as connection:
            prior = self.store.replay(connection, run_id, request_id, fingerprint)
            if prior:
                return prior
            run = self.store.run(connection, run_id)
            self._admit(connection, run, request=(run_id, request_id, fingerprint))
            require(not run["active_job"], "VERIFICATION_ACTIVE", "Wait or cancel verification before retiring a check")
            latest = run["interpretations"][-1]["ref"]
            require(type(proposal["interpretation_revision"]) is int and proposal["interpretation_revision"] == latest["revision"], "STALE_INTERPRETATION", "Interpretation changed")
            key = semantics.named(proposal["check_id"])
            require(not key.startswith(("file-", "command-", "domain.")), "CHECK_ID_RESERVED", "Revise Domain parameters to remove a Domain-generated check")
            previous = semantics.latest_checks(run).get(key)
            require(previous is not None and previous["active"], "CHECK_NOT_ACTIVE", "Check is not active")
            require(type(proposal["expected_revision"]) is int and proposal["expected_revision"] == previous["ref"]["revision"], "STALE_CHECK", "Check revision changed")
            retired = semantics.retire_check(run, previous, latest, redact(proposal["reason"]))
            run["verification"] = {"status": "not_run"}
            self.store.save_run(connection, run)
            result = response(run_id=run_id, check=retired)
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
        cited = self._observations_exist(connection, run["run_id"], assessment["cited_observation_ids"])
        require(len(run["assessments"]) < 100, "ASSESSMENT_LIMIT", "Assessment limit reached")
        subject = run["candidate"]["subject"] if run["candidate"] else None
        check_set = semantics.check_set_hash(semantics.current_checks(run))
        bindings = []
        for item in cited:
            current_subject = item["subject"] == subject
            current_interpretation = item["interpretation_ref"] == latest
            current_check_set = item.get("check_set_hash") == check_set
            bindings.append({"observation_id": item["observation_id"], "subject": item["subject"],
                             "interpretation_ref": item["interpretation_ref"], "check_set_hash": item.get("check_set_hash"),
                             "current_subject": current_subject, "current_interpretation": current_interpretation,
                             "current_check_set": current_check_set,
                             "relevance": "current" if current_subject and current_interpretation and current_check_set else "contextual"})
        summary = {"current": sum(item["relevance"] == "current" for item in bindings),
                   "contextual": sum(item["relevance"] == "contextual" for item in bindings)}
        created = semantics.record("assessment:" + run["run_id"] + ":" + uuid.uuid4().hex, 1,
                  {**redact(assessment), "interpretation_ref": latest, "subject": subject,
                   "citation_bindings": bindings, "citation_summary": summary, "origin": "caller", "trust": "untrusted"})
        run["assessments"].append(created)
        return created

    def assess(self, run_id: str, assessment: dict, request_id: str):
        fingerprint = canonical_hash({"operation": "assess", "assessment": assessment})
        with self.store.transaction() as connection:
            prior = self.store.replay(connection, run_id, request_id, fingerprint)
            if prior:
                return prior
            run = self.store.run(connection, run_id)
            self._admit(connection, run, request=(run_id, request_id, fingerprint))
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
            self._admit(connection, run, request=(run_id, request_id, fingerprint))
            require("submit" in run["domain_preparation"]["available_operations"], "NEEDS_INPUT", "Domain preparation still needs an input scope; revise this Run")
            require(not run["active_job"], "VERIFICATION_ACTIVE", "Wait for verification or finish as abandoned")
            expected_revision = run["revision"]
            directory = self.store.directory(run_id)
        # Slow file reads/copies must not hold the store's global writer lock.
        # Staging is owned by this call and cannot be observed by a verifier.
        with tempfile.TemporaryDirectory(prefix=".submit_", dir=directory) as staging, maintenance.capture_lease(staging):
            overlays = {}
            if run.get("acceptance"):
                acceptance.validate(run["acceptance"], directory)
                overlays = {path: acceptance.bundle_payload(run["acceptance"], directory)
                            for path in run["acceptance"]["definition"]["bundle"]["files"]}
            candidate = capture(Path(run["workspace"]), run["contract"]["inputs"], Path(staging), overlays=overlays)
            published = None
            try:
                with self.store.transaction() as connection:
                    prior = self.store.replay(connection, run_id, request_id, fingerprint)
                    if prior:
                        return prior
                    current = self.store.run(connection, run_id)
                    self._admit(connection, current, request=(run_id, request_id, fingerprint))
                    require(current["revision"] == expected_revision and not current["active_job"],
                            "SUBMISSION_CONFLICT", "Run changed during capture; inspect the Run and submit again")
                    target = directory / identifier(candidate["candidate_id"], "candidate")
                    os.rename(Path(staging) / candidate["candidate_id"], target)
                    published = target
                    current.update(candidate=candidate, generation=current["generation"] + 1,
                                   phase="submitted", verification={"status": "not_run"})
                    self.store.save_run(connection, current)
                    result = response(run_id=run_id, candidate=candidate)
                    self.store.remember(connection, run_id, request_id, fingerprint, result)
            except BaseException:
                if published is not None:
                    os.rename(published, Path(staging) / candidate["candidate_id"])
                raise
            return result

    def verify(self, run_id: str, request_id: str, *, compare_baseline: bool = False, expected_candidate_hash: str | None = None):
        require(type(compare_baseline) is bool, "INVALID_PARAMETERS", "compare_baseline must be boolean")
        request = {"operation": "verify"}
        if compare_baseline:
            request["compare_baseline"] = True
        if expected_candidate_hash is not None:
            require(isinstance(expected_candidate_hash, str) and len(expected_candidate_hash) == 64
                    and all(c in "0123456789abcdef" for c in expected_candidate_hash), "INVALID_PARAMETERS", "Invalid expected Candidate hash")
            request["expected_candidate_hash"] = expected_candidate_hash
        fingerprint = canonical_hash(request)
        # Optional runtime packaging can perform substantial file I/O. Keep it
        # outside the writer transaction so other Runs can checkpoint/heartbeat.
        prior, initial = self._mutation_snapshot(run_id, request_id, fingerprint, verification=True)
        if prior:
            return prior
        require(not initial["active_job"], "VERIFICATION_ACTIVE", "A verifier already owns this Run")
        require(initial["candidate"] is not None, "SUBMIT_REQUIRED", "Submit a snapshot before verification")
        require("verify" in initial["domain_preparation"]["available_operations"], "NEEDS_INPUT", "Domain preparation has not admitted measurements")
        initial_checks = copy.deepcopy(semantics.current_checks(initial))
        initial_check_hash = semantics.check_set_hash(initial_checks)
        require(initial["contract"]["verifier"] == verifier_identity(), "VERIFIER_CHANGED", "Verifier code changed; start a new Run with the new version")
        self._validate_checks(initial, initial["contract"])
        if initial.get("acceptance"):
            acceptance.validate(initial["acceptance"], self.store.directory(run_id))
            acceptance.validate_subject(initial["acceptance"], initial["candidate"])
        from .adapter_execution import bind as bind_adapters
        prepared_implementations = self.adapters.pin_implementations(
            [item["spec"]["parameters"] for item in initial_checks], initial.get("adapter_implementations"))
        prepared_bindings = bind_adapters(initial_checks, self.store.root, initial.get("adapter_bindings"), self.adapters)
        prepared_registrations = self.adapters.export(prepared_bindings)
        self.adapters.validate_bindings(prepared_bindings)
        self.adapters.pin_implementations([], prepared_implementations)
        with self.store.transaction() as connection:
            prior = self.store.replay(connection, run_id, request_id, fingerprint)
            if prior:
                return prior
            run = self.store.run(connection, run_id)
            self._admit(connection, run, verification=True, request=(run_id, request_id, fingerprint))
            require(not run["active_job"], "VERIFICATION_ACTIVE", "A verifier already owns this Run")
            require(run["candidate"] is not None, "SUBMIT_REQUIRED", "Submit a snapshot before verification")
            require(expected_candidate_hash is None or expected_candidate_hash == run["candidate"]["candidate_hash"],
                    "CANDIDATE_SUPERSEDED", "Another submission replaced the expected Candidate; no verification was started")
            require(not compare_baseline or run.get("baseline"), "BASELINE_REQUIRED", "This Run has no pre-edit baseline; do not reconstruct one from edited files")
            require("verify" in run["domain_preparation"]["available_operations"], "NEEDS_INPUT", "Domain preparation has not admitted measurements")
            require(run["revision"] == initial["revision"], "VERIFICATION_CONFLICT", "Run changed while preparing verification")
            job_id = "job_" + uuid.uuid4().hex
            now = time.time()
            checks = copy.deepcopy(semantics.current_checks(run))
            require(semantics.check_set_hash(checks) == initial_check_hash, "VERIFICATION_CONFLICT", "Checks changed while preparing adapter runtimes")
            require(all(old.get("status") == "unavailable" or prepared_bindings.get(key) == old
                        for key, old in run.get("adapter_bindings", {}).items()), "ADAPTER_RUNTIME_CHANGED", "Another request pinned a different runtime")
            require(all(prepared_implementations.get(key) == value for key, value in run.get("adapter_implementations", {}).items()),
                    "ADAPTER_IMPLEMENTATION_CHANGED", "Another request pinned a different adapter")
            require(all(binding["implementation"] == prepared_implementations[binding["id"]] for binding in prepared_bindings.values()),
                    "ADAPTER_IMPLEMENTATION_CHANGED", "Runtime binding differs from admitted adapter")
            run["adapter_implementations"] = prepared_implementations
            run["adapter_bindings"] = prepared_bindings
            timeout = 45 + (2 if compare_baseline else 1) * sum(check["spec"]["timeoutMs"] / 1000 + 10 for check in checks)
            job = {"job_id": job_id, "run_id": run_id, "status": "queued", "heartbeat_at": now,
                   "attempt": run["verification_attempts"] + 1,
                   "deadline_at": min(run["deadline_at"], now + timeout), "generation": run["generation"],
                   "contract_hash": run["contract"]["contract_hash"], "candidate_hash": run["candidate"]["candidate_hash"],
                   "cleanup": "not_started", "result": None}
            job.update(checks=checks, candidate=copy.deepcopy(run["candidate"]),
                       adapter_bindings=copy.deepcopy(run["adapter_bindings"]),
                       adapter_registrations=prepared_registrations,
                       interpretation_ref=run["interpretations"][-1]["ref"], policy_ref=run["policy"]["ref"],
                       completed_checks=0, total_checks=len(checks) * (2 if compare_baseline else 1))
            if compare_baseline:
                job["baseline"] = copy.deepcopy(run["baseline"])
            job.update(check_set_hash=semantics.check_set_hash(checks), domain_identity=copy.deepcopy(run["domain_module"]))
            if run.get("acceptance"):
                job["acceptance_binding_hash"] = run["acceptance"]["binding_hash"]
            require(job["total_checks"] > 0, "CHECKS_REQUIRED", "Register a measurement before verification")
            run.update(active_job=job_id, phase="verifying", verification={"status": "queued", "job_id": job_id},
                       verification_attempts=run["verification_attempts"] + 1)
            self.store.save_job(connection, job)
            self.store.save_run(connection, run)
            result = response(run_id=run_id, job_id=job_id, status="queued", check_set_hash=job["check_set_hash"])
            self.store.remember(connection, run_id, request_id, fingerprint, result)
        # Durable queue entry exists before spawning. Replayed requests never spawn twice.
        from .worker import spawn_worker
        try:
            spawn_worker(self.store.root, job_id)
        except OSError:
            with self.store.transaction() as connection:
                job = self.store.job(connection, job_id)
                run = self.store.run(connection, run_id)
                if job["status"] == "queued" and run["status"] == "active" and run["active_job"] == job_id and run["generation"] == job["generation"]:
                    job.update(status="error", cleanup="complete", error={"code": "WORKER_START_FAILED", "message": "Could not start verifier worker"})
                    self.store.save_job(connection, job)
                    run.update(active_job=None, phase="review", verification={"status": "error", "job_id": job_id})
                    self.store.save_run(connection, run)
        return result

    def cancel(self, run_id: str, job_id: str, request_id: str):
        fingerprint = canonical_hash({"operation": "cancel", "job_id": job_id})
        with self.store.transaction() as connection:
            prior = self.store.replay(connection, run_id, request_id, fingerprint)
            if prior:
                return prior
            run = self.store.run(connection, run_id)
            job = self.store.job(connection, job_id)
            require(job["run_id"] == run_id, "JOB_RUN_MISMATCH", "Job belongs to another Run")
            self._refresh(connection, run)
            job = self.store.job(connection, job_id)
            if job["status"] in {"queued", "running"}:
                job.update(status="cancelled", cleanup="complete" if job["status"] == "queued" else "pending")
                self.store.save_job(connection, job)
                if run["active_job"] == job_id:
                    run.update(active_job=None, verification={"status": "cancelled", "job_id": job_id})
                    if run["status"] == "active":
                        run["phase"] = "review"
                    self.store.save_run(connection, run)
            result = response(run_id=run_id, job=queries.job_view(job))
            self.store.remember(connection, run_id, request_id, fingerprint, result)
            return result

    def list_runs(self, *, offset=0, limit=20):
        return response(**queries.list_runs(self, offset=offset, limit=limit))

    def resume(self, run_id):
        return response(**queries.resume(self, run_id))

    def context(self, run_id, *, after=None, page=None, limit=20):
        from .context import read
        return response(**read(self, run_id, after=after, page=page, limit=limit))

    def records(self, run_id, kind, *, offset=0, limit=20, job_id=None):
        return response(**queries.records(self, run_id, kind, offset=offset, limit=limit, job_id=job_id))

    def cleanup(self, run_id, *, apply=False, min_age_seconds=3600):
        return response(**maintenance.cleanup(self.store, run_id, apply=apply, min_age_seconds=min_age_seconds))

    def status(self, run_id: str, job_id: str | None = None, *, check_workspace: bool = False, view="full"):
        require(view in {"summary", "full"}, "INVALID_VIEW", "Status view is summary or full")
        if view == "summary":
            result = self.resume(run_id) if not job_id else response(**queries.job_summary(self, run_id, job_id))
            if check_workspace:
                expected = (result.get("candidate") or result.get("job") or {}).get("candidate_hash")
                result["current_workspace_matches_submitted_files"] = self._workspace_matches(run_id, expected)
                with self.store.transaction(write=False) as connection:
                    run = self.store.run(connection, run_id)
                    result["workspace_comparison_excludes_pinned_bundle"] = (run.get("acceptance") or {}).get("definition", {}).get("bundle", {}).get("files", [])
            return result
        self._refresh_for_read(run_id)
        with self.store.transaction(write=False) as connection:
            run = self.store.run(connection, run_id)
            jobs = [decode_job[0] for decode_job in connection.execute("SELECT job_id FROM jobs WHERE run_id=?", (run_id,))]
            if job_id:
                job = self.store.job(connection, job_id)
                require(job["run_id"] == run_id, "JOB_RUN_MISMATCH", "Job belongs to another Run")
                self.store.validate_result(connection, job)
                return response(run_id=run_id, view="full", job=job, measurements=self.store.measurements(connection, run_id, job_id))
            if run["record"]:
                require(run["record"].get("record_hash") == canonical_hash({k: v for k, v in run["record"].items() if k != "record_hash"}),
                        "RECORD_CORRUPT", "Local completion record digest mismatch")
            measured = self.store.measurements(connection, run_id)
            job_records = []
            for item in jobs:
                job = self.store.job(connection, item)
                self.store.validate_result(connection, job)
                job_records.append(job)
            result = response(run=run, jobs=job_records, measurements=measured,
                              view="full", resolution=semantics.resolution(run, semantics.gates(run, measured)),
                              closeout={"measurement": run["verification"], "assessment": semantics.assessment_view(run),
                                        "gates": semantics.gates(run, measured), "lifecycle": semantics.lifecycle(run),
                                        "termination_reason": run.get("termination_reason")})
        if check_workspace:
            result["current_workspace_matches_submitted_files"] = self._workspace_matches(run_id, (run["candidate"] or {}).get("candidate_hash"))
            result["workspace_comparison_excludes_pinned_bundle"] = (run.get("acceptance") or {}).get("definition", {}).get("bundle", {}).get("files", [])
        return result

    def _workspace_matches(self, run_id, expected_candidate_hash):
        with self.store.transaction(write=False) as connection:
            run = self.store.run(connection, run_id)
        if not run["candidate"] or run["candidate"]["candidate_hash"] != expected_candidate_hash:
            return False
        try:
            def same(entry):
                if entry["path"] in (run.get("acceptance") or {}).get("definition", {}).get("bundle", {}).get("files", []):
                    # These bytes come from the pinned operator bundle, never
                    # an identically named workspace file.
                    return True
                data, mode = read_file(Path(run["workspace"]), entry["path"])
                return hashlib.sha256(data).hexdigest() == entry["sha256"] and mode == run["candidate"]["executable"][entry["path"]]
            return all(same(entry) for entry in run["candidate"]["manifest"]["files"])
        except (OSError, HarnessError):
            return False

    def _validate_completion(self, connection, run, gate_results, *, validate_result=False):
        require(run["status"] == "active" and time.time() < run["deadline_at"], "RUN_CLOSED", "Expired/blocked Runs can only finish as partial or abandoned")
        require(not run["active_job"] and "finish_completed" in run["domain_preparation"]["available_operations"] and gate_results["status"] in {"passed", "not_required"},
                "VERIFICATION_REQUIRED", "Pinned completion gates have not been satisfied")
        if run["policy"]["mode"] == "strict":
            require(run["verification"]["status"] == "passed", "VERIFICATION_REQUIRED", "Strict policy requires all measurements to pass")
        require(not run["contract"]["rules"].get("snapshot_required", False) or run["candidate"] is not None,
                "SUBMIT_REQUIRED", "The contract requires a submitted snapshot before completion")
        job = self.store.job(connection, run["verification"]["job_id"]) if run["verification"].get("job_id") else None
        observed = (job.get("result") or {}) if job else {}
        if run["policy"]["mode"] == "strict":
            require(job is not None and job["status"] == "completed", "VERIFICATION_REQUIRED", "Strict policy requires a completed verifier job")
        if observed:
            if validate_result:
                self.store.validate_result(connection, job)
            require(observed.get("result_hash") == canonical_hash({k: v for k, v in observed.items() if k != "result_hash"})
                    and observed.get("result_hash") == run["verification"].get("result_hash"),
                    "RESULT_CORRUPT", "Verification result digest/status mismatch")
            require(job["candidate_hash"] == run["candidate"]["candidate_hash"] and job["contract_hash"] == run["contract"]["contract_hash"],
                    "RESULT_BINDING", "Verification does not cover the current candidate/contract")
            require(job.get("check_set_hash") == semantics.check_set_hash(semantics.current_checks(run)),
                    "RESULT_BINDING", "Verification does not cover the current check set")

    def _completion_snapshot(self, run_id, request_id, fingerprint):
        with self.store.transaction(write=False) as connection:
            prior = self.store.replay(connection, run_id, request_id, fingerprint)
            if prior:
                return prior, None
            run = self.store.run(connection, run_id)
            self._validate_completion(connection, run, semantics.gates(run, self.store.gate_measurements(connection, run)), validate_result=True)
        if run["candidate"]:
            validate_candidate(run["candidate"], self.store.directory(run_id) / identifier(run["candidate"]["candidate_id"], "candidate") / "payload")
            if run.get("acceptance"):
                acceptance.validate(run["acceptance"], self.store.directory(run_id))
                acceptance.validate_subject(run["acceptance"], run["candidate"])
        require(run["contract"]["verifier"] == verifier_identity(), "VERIFIER_CHANGED", "Verifier code changed after measurement")
        self.adapters.validate_bindings(run.get("adapter_bindings", {}))
        self.adapters.pin_implementations([], run.get("adapter_implementations"))
        self._module(run)
        return None, run

    def finish(self, run_id: str, request_id: str, *, outcome: str, summary: str = "", assessment: dict | None = None):
        require(outcome in {"completed", "partial", "abandoned"} and isinstance(summary, str) and len(summary) <= 16000,
                "INVALID_FINISH", "Outcome is completed, partial or abandoned")
        request = {"operation": "finish", "outcome": outcome, "summary": summary}
        if assessment is not None:
            request["assessment"] = assessment
        fingerprint = canonical_hash(request)
        prepared = None
        if outcome == "completed":
            prior, prepared = self._completion_snapshot(run_id, request_id, fingerprint)
            if prior:
                return prior
        with self.store.transaction() as connection:
            prior = self.store.replay(connection, run_id, request_id, fingerprint)
            if prior:
                return prior
            run = self.store.run(connection, run_id)
            self._refresh(connection, run)
            require(run["status"] != "finished", "RUN_CLOSED", "Run is already finished")
            if assessment is not None:
                self._assessment_record(connection, run, assessment)
            measured = self.store.gate_measurements(connection, run)
            gate_results = semantics.gates(run, measured)
            if outcome == "completed":
                self._validate_completion(connection, run, gate_results)
                require(run["revision"] == prepared["revision"], "RUN_CONFLICT", "Run changed while checking completion; inspect it before retrying")
            if run["active_job"]:
                job = self.store.job(connection, run["active_job"])
                job.update(status="cancelled", cleanup="complete" if job["status"] == "queued" else "pending")
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
            record.update(check_set_hash=semantics.check_set_hash(semantics.current_checks(run)), domain_identity=run["domain_module"],
                          adapter_bindings=copy.deepcopy(run.get("adapter_bindings", {})),
                          acceptance=acceptance.view(run.get("acceptance")), predecessor=run.get("predecessor"),
                          baseline_comparison=run["verification"].get("baseline_comparison"),
                          outcome_meaning="caller_requested_disposition_not_verification_success")
            record["unresolved_verification_jobs"] = [row[0] for row in connection.execute("SELECT job_id FROM jobs WHERE run_id=?", (run_id,))
                                                      if self.store.job(connection, row[0]).get("cleanup") not in {"complete", "not_started"}]
            run.update(status="finished", phase="finished", record=record, termination_reason=reason)
            record["resolution"] = semantics.resolution(run, gate_results)
            record["record_hash"] = canonical_hash(record)
            self.store.save_run(connection, run)
            result = response(run_id=run_id, record=record, resolution=record["resolution"])
            self.store.remember(connection, run_id, request_id, fingerprint, result)
            return result
