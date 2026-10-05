"""Presentation only. No admission, evidence validation or state writes."""
from . import semantics


def job_view(job):
    return {**{key: job.get(key) for key in ("job_id", "run_id", "status", "cleanup", "heartbeat_at",
            "deadline_at", "completed_checks", "total_checks", "observed_cases", "completed_cases", "error", "check_set_hash", "candidate_hash")},
            "result": {key: job["result"].get(key) for key in ("status", "result_hash", "check_set_hash", "measurement_scope", "baseline_comparison", "requirement_observations")} if job.get("result") else None}


def run_view(run):
    return {"run_id": run["run_id"], "domain_id": run["contract"]["domain_id"], "goal": run["goal"],
            "workspace": run["workspace"], "status": run["status"], "phase": run["phase"],
            "lifecycle": semantics.lifecycle(run), "created_at": run["created_at"], "deadline_at": run["deadline_at"],
            "interpretation_ref": run["interpretations"][-1]["ref"], "active_job": run["active_job"],
            "termination_reason": run.get("termination_reason")}
