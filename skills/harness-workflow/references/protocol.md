# Caller protocol (external-harness-v2)

Run `python3 <skill>/scripts/harness_client.py --config /absolute/client.json call
--request /absolute/request.json`. `--request -` reads UTF-8 stdin; `json:{...}` is
also accepted. Paths are in the environment running the helper and Harness. From
a Windows Host using WSL, invoke the helper through `wsl.exe --exec python3 ...`
with WSL paths; do not pass Windows drive paths to the Linux helper.

The helper config pins `harness_repo`, `state_dir`, `state_lifetime` and schema version.
Production uses a durable state directory outside every workspace. `--ephemeral`
is an explicit disposable-test setting, not a recovery strategy. Setup is documented
in the repository's `INSTALL.md`; the helper's `configure --help` is self-contained.
The helper calls the existing CLI, not a second service. Optional `policy_root`
selects an operator-configured acceptance registry. It is configuration, not a
field the caller can replace in a task request. Configured Domain defaults cannot
be overridden by selecting a weaker policy.

## Low-overhead workflow

`preflight` runs the real strict Sandbox doctor without creating a task Run. If
unavailable, preserve the limitation; never fall back to unsandboxed verification.
After starting or explicitly selecting the correct Run, save its binding once:

```sh
python3 <skill>/scripts/harness_client.py --config /absolute/client.json bind \
  --run-id RUN_ID --workspace /absolute/project --domain develop \
  --output /absolute/task-binding.json
python3 <skill>/scripts/harness_client.py --config /absolute/client.json checkpoint \
  --binding /absolute/task-binding.json --action-id implementation-1 --wait-seconds 30
```

`checkpoint` performs exactly one submit and one verify and returns a compact job
summary. It does not assess, repair, finish, choose another method, or retry on an
unknown response. Repeating the same action ID replays the original submission
and Job. Use a new action ID for a new intentional checkpoint. Concurrent helper
mutations return `CALLER_BUSY`; Core still enforces concurrency for all clients.

`wait_status: pending` does not cancel the job. Use `wait --binding ... --job-id ...`
to observe that same job. `terminal` means the job ended; inspect its result, scope
and error. Failed checks and unavailable execution are not passing evidence.

For other operations, `request --binding ... --operation observe --arguments
'json:{"data":{"note":"Decision and next step"}}' --action-id note-1 --output
/absolute/note-1.json` saves the exact envelope. Send it with `call --request` and
reuse that file after response loss. Existing files are never overwritten. Bindings
are references only, not a second Run ledger; every use checks the authoritative
workspace, Domain, intent and policy in Harness. The helper never picks a Run,
generates an assessment, or changes an initial policy for you.

## Envelope and initial request

```json
{
  "operation": "start",
  "request_id": "project-task-001-start",
  "arguments": {
    "domain": "develop",
    "goal": "The user's original request, not an invented test goal",
    "workspace": "/absolute/project",
    "mode": "exploratory",
    "provenance": {"declared_author": "model"},
    "constraints": []
  }
}
```

With a configured acceptance policy, use `mode: "acceptance"`; `parameters` may
be omitted and initial `provenance` describes caller proposals, not the policy
author. The Domain compiles mandatory checks from the registry and merges any
additional exploratory parameters. `policy_id` can select a registered profile
only where the operator has not pinned a Domain default. A missing profile or
incompatible Domain is an error, not permission to invent replacement criteria.

Without configured acceptance, choose policy explicitly: `exploratory` permits a clarification state and gates
only declared checks; `strict` needs complete Domain parameters and gates all checks.
Do not pass `required_check` or `deferred_check` with `strict`; those fields belong
to exploratory policy and combining them with strict is rejected.
Use a task's known `required_check` IDs, or explicitly declared `deferred_check` IDs
for required definitions supplied later. Those fields are string arrays in this
envelope. Never assume a gate-free exploratory closure means tests passed. The
example omits gates rather than inventing one for every Domain/task.
`provenance` is a caller declaration, not authenticated approval. An application
default is not authenticated user approval. Registered policies are separately
configured, but are still same-user, local-advisory records. The current checkout
supports the Develop Domain.

Set `capture_baseline: true` at start to pin declared input files before editing.
Files must already exist; Harness never reconstructs an original from later edits.
Use `checkpoint --compare-baseline` or verify argument `compare_baseline: true` to
measure both subjects with identical checks. `baseline_comparison.changes` reports
improved/already_passing/regressed/still_failing/inconclusive. Command comparisons
without pinned acceptance tests are inconclusive, not proof of improvement.

Arguments use the CLI's option names with underscores: `run_id`, `check_workspace`,
`test_commands` (inside Develop parameters), etc. JSON options (`parameters`, `data`,
`budget`, `constraints`, `provenance`, `assessment`) take real objects/arrays here,
not paths or pre-encoded JSON strings. The helper handles encoding without a shell.

## Existing Run, revisions and measurements

Every Run operation includes context to reject cross-workspace/Domain mixups:

```json
{
  "operation": "revise",
  "request_id": "project-task-001-revise-1",
  "context": {"workspace": "/absolute/project", "domain": "develop"},
  "arguments": {
    "run_id": "RUN_ID_FROM_START",
    "data": {
      "expected_revision": 1,
      "goal_summary": "Current interpretation, still open to revision",
      "assumptions": [{"id": "runtime", "statement": "Python CLI is appropriate"}],
      "open_questions": [],
      "parameters": {
        "profile": "execution",
        "inputs": ["main.py", "tests/test_main.py"],
        "artifacts": ["main.py"],
        "expectations": [{"id": "entrypoint", "path": "main.py", "operator": "contains", "expected": "def main("}],
        "test_commands": [{"id": "behavior", "argv": ["python3", "-m", "unittest", "discover", "-s", "tests", "-v"], "timeout_seconds": 60}]
      }
    }
  }
}
```

This is Develop-specific syntax, not a universal success criterion. Named checks
above become `domain.entrypoint` and `domain.behavior`. A substring check only
measures that substring, not behavioral sufficiency. Adapt tests to the task.
Develop requires a nonempty `expectations` list, and every declared `artifact` must
also be an `input` with at least one file expectation. Tests/fixtures may be inputs
without being artifacts; do not label every input an artifact unnecessarily.
The `execution` profile also needs at least one `test_commands` entry. An explicitly
chosen `structural` profile excludes commands and establishes no runtime behavior.
`assumptions` requires `{id, statement, observation_ids?}` objects, not string arrays.
`expected_revision` is the interpretation revision, not a storage write counter.

Use the same context with these operations/arguments:

| Operation | Arguments in addition to `run_id` | Request ID |
|---|---|---|
| `submit` | none | required |
| `verify` | optional `compare_baseline`, `expected_candidate_hash`; returns `job_id` | required |
| `status` | optional `job_id`, `check_workspace: true`, `view: "summary"` | omit |
| `resume` | none; query only, does not restart a worker | omit |
| `records` | `kind`, optional `job_id` (measurements only), `offset`, `limit` | omit |
| `observe` | `data: {"note": "Decision and next step"}` | required |
| `assess` | `data` below | required |
| `finish` | `outcome: "completed" / "partial" / "abandoned"`, optional `summary`, `assessment` | required |
| `cancel` | `job_id`; cancels that job, not the Run | required |
| `check`, `retire-check` | Domain check proposal/retirement in `data` | required |
| `cleanup` | preview by default; `apply: true` is an explicit authorized action | omit |

Assessment data:

```json
{
  "interpretation_revision": 2,
  "status": "satisfied",
  "summary": "Conclusion supported by the actual observations",
  "uncertainties": ["Any untested platform or unresolved requirement"],
  "cited_observation_ids": ["OBSERVATION_ID_FROM_RECORDS"]
}
```

Other assessment statuses are `partial`, `unsolved`, `not_assessed`. Inspect returned
`citation_bindings`: only `current` references match the current Candidate, check
set and interpretation. `contextual` references remain historical evidence.

Global requests need no context: `doctor` with `{ "sandbox": true }`, or `list-runs`
with `{ "offset": 0, "limit": 20 }`. They need no request ID. `healthy` and
`command_execution_verified` are environment checks, not task results.

## Recovery and bounded reads

`find --workspace /absolute/project --domain develop` lists matching Runs without
choosing one. Check the original goal and lifecycle; even a sole match is not proof
it is the requested task. Discovery is paginated and reports `truncated`; use direct
`list-runs` pages when needed. Persisted Harness records, not Host memory, own state.

Missing config/state, identity mismatch, incompatible API or verifier changes must
be reported explicitly. Do not quietly create another store/Run. A changed verifier
may require a consciously chosen new Run; link the previous Run in a caller note.

Use bounded records pages. `ok: true` describes API handling, not verification
success. Read `resolution` fields and measurements separately. A failed check may
still be a successfully measured fact. Wait for terminal job state; never interpret
`queued`, `not_run`, skips or partial checkpoints as a passing result.

The helper has no default wall-clock deadline beyond Harness's own budgets.
Optional `--call-timeout SECONDS` limits only the foreground CLI response. If it
expires, the outcome is unknown and a durable worker may still run. Query state or
replay the identical request ID/payload before deciding on another action. Never
generate a new ID merely because a response was lost. No fallback executes checks
outside the strict Sandbox. Host-side tests are separate caller observations.
