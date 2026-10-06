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
The helper sends one `invoke --request` envelope to the existing CLI, not a second service. Optional `policy_root`
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
are references only, not a second Run ledger. Request construction is local; actual
execution checks workspace, Domain, intent and policy inside Core, before replay
or mutation. The helper never picks a Run,
generates an assessment, or changes an initial policy for you.

With `request --binding`, omit `run_id` from `--arguments`: the binding supplies
it. Read requests also omit `--action-id`; an unsuccessful request builder has not
created a request file. `--config` selects the client configuration, not a binding.
For example, inspect measurements without creating another action:

```sh
python3 <skill>/scripts/harness_client.py request --binding /absolute/task-binding.json \
  --operation records --arguments 'json:{"kind":"measurements","job_id":"JOB_ID","limit":20}' \
  --output /absolute/measurements-request.json
python3 <skill>/scripts/harness_client.py --config /absolute/client.json call \
  --request /absolute/measurements-request.json
```

## Lossless context and lower-cost follow-up

```sh
python3 <skill>/scripts/harness_client.py context --binding /absolute/task-binding.json
python3 <skill>/scripts/harness_client.py context --binding /absolute/task-binding.json --page PAGE_TOKEN
python3 <skill>/scripts/harness_client.py context --binding /absolute/task-binding.json --after CURSOR
```

Omit the cursor on a new Host session or after context loss. Follow `next_page`
until it is null; only the last page issues `next_cursor`. Pages share one immutable
`snapshot` even if new work arrives. `critical` always describes current blocking,
measurement and uncertainty status; `snapshot_critical` identifies an older page's
state when relevant. Afterwards, `--after` returns changed records, not another
copy of the original goal and history. An invalid/foreign cursor requests a full
transfer of the explicitly selected Run with `reset_reason`; corruption is an error.

Each item has a stable `key`, record `ref`, restored-body `digest`, `data` and
`text_refs`. Long strings are sent once per page in `texts`, indexed by their
digest. Each `text_refs` entry gives a list-valued path into `data` (empty means
the whole value) and its text digest; replace the null at that path with that
string. This is lossless substitution, not a natural-language summary. Apply full
page zero to an empty document and subsequent pages/deltas by replacing each
item's key. The Python API exports `harness_external.context.apply_page` as a
reference decoder. Caller decisions remain claims; only measurements are facts.

Cursors are explicit read positions, not approval, authority or proof the Host
retained the text. The helper does not persist them in bindings. Legacy Run JSON
is returned as `legacy_full_only`, without converting old records to the new
storage or issuing a delta cursor. `resume`, `status --view full` and `records`
remain available. Do not count response bytes as billed tokens or model quality.

For configured pytest checks, read `records` with `kind: "cases"` and the Job ID.
Job results include `requirement_observations`. Check whether declared cases were
discovered, selected and executed; preserve missing, skipped, errored and incomplete
states. A collection failure does not establish that a case is absent. Only
explicit required scenarios affect gates. Case observation IDs can be cited in
`assess`; original/baseline/older Candidate facts remain contextual when appropriate.
For the policy format and optional runtime setup, use the Harness checkout's
`POLICIES.md`. Prepare does not execute collection; measurement follows submission.

`checkpoint` now executes its submit/verify sequence in one CLI process and keeps
their independent replay IDs. Bounded `wait` polls inside that same process;
ending the wait never cancels, resubmits or assesses the job. Unknown process
outcomes still require the identical request/action ID, not an automatic retry.

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
`execution_checks` (inside Develop parameters), etc. JSON options (`parameters`, `data`,
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
        "execution_checks": [{"kind": "command", "id": "behavior", "argv": ["python3", "-m", "unittest", "discover", "-s", "tests", "-v"], "timeout_seconds": 60}]
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
The `execution` profile also needs at least one command or case entry in `execution_checks`. An explicitly
chosen `structural` profile excludes commands and establishes no runtime behavior.
`assumptions` requires `{id, statement, observation_ids?}` objects, not string arrays.
`expected_revision` is the interpretation revision, not a storage write counter.

## Need analysis and focused discovery

Develop returns `domain_preparation.analysis_guidance`, a versioned advisory
Knowledge Map with question types, applicability and source hints. It neither reads
project files nor generates questions. The Host derives the actual Needs and uses
its own tools. A map entry is not a mandated step, source truth or completion gate.

Use the existing `observe` envelope with the following `data`:

```json
{
  "kind": "need",
  "note": "Identify the compatibility decision before changing the transformation",
  "interpretation_revision": 1,
  "need": {
    "id": "collision-policy",
    "expected_revision": 0,
    "state": "open",
    "details": {
      "kind": "decision",
      "question": "Which value should survive a legacy/nested field collision?",
      "reason": "Avoid silently discarding existing configuration",
      "resolution_criterion": "Locate an applicable policy or get the user's decision",
      "requirement_ids": [],
      "knowledge_refs": ["compatibility"]
    }
  }
}
```

`details.kind` is knowledge/observation/decision/verification. Requirement IDs must
refer to declared requirements; leave them empty during initial discovery. Knowledge
refs select map IDs, not required reading. They may be empty for questions outside
the map. Core does not interpret either Domain vocabulary.

Updates send the same Need ID, its returned `ref.revision` as `expected_revision`,
and the complete current details. States are open/addressed/deferred; addressed and
deferred require a nonblank `conclusion`. Reopening defaults the conclusion to empty
so an old answer is not silently retained. A distinct request ID records a new
update; replay the same envelope/ID after response loss. Concurrent stale updates
are rejected without replacing the newer version.

When answering the same question, reuse its returned `details`; do not replace the
question with a generic closeout instruction. Given a saved observe response, this
constructs the `data` object for a new update (the request still needs its own ID):

```python
current = saved_response["need"]  # Or the selected item from records kind=needs.
data = {
    "kind": "need", "note": "Record the source-grounded conclusion",
    "need": {
        "id": current["need_id"], "expected_revision": current["ref"]["revision"],
        "details": current["details"], "state": "addressed",
        "conclusion": "The supplied schema specifies which value survives."
    },
    "references": ["schema.txt#collision-policy"]
}
```

Keep these fields at their distinct levels:

| Field in `data` | Value |
|---|---|
| `need.details` | Domain question, reason, criterion and Domain references |
| `need.conclusion` | Answer or deferral explanation, not inside `details` |
| `references` | Locator strings, not path/symbol objects or a field inside `need` |
| `need.activity_ids` | Optional complete returned `activity.ref.id` values; do not invent a UUID or use a Need ID |
| `observation_ids` | Returned Verifier observation IDs, not caller notes |

Top-level `references` are bounded source locators (file/symbol/section or URL),
never fetched by Harness and never authenticated facts. Top-level `observation_ids`
must be real Verifier observations from this Run; older Candidates remain historical
context, not current proof. Inside `need`, optional `activity_ids` cite earlier
decisions/activities and `check_ids` cite active Checks. Their exact revisions are
recorded. Cite sources and state why the conclusion is sufficient, not just what
was read. Do not use caller note IDs as measurement IDs.

`records kind=needs` returns paginated current Need records; `activity` retains
update references and context pages retain immutable snapshot versions. `resume`
and `context.critical` include counts, open/deferred IDs and `context_changed_ids`.
The latter only means the recorded Interpretation or Candidate differs. It does
not inspect files for freshness or decide whether an answer is still applicable.
Use a full context transfer after lost Host context, then deltas as usual.

Need records are caller claims, not GoalContract parameters. They do not change
the contract, Candidate, Measurement, Assessment, gates or completion policy.
Use `revise`/`check` only when reasoning actually changes those declarations, and
preserve existing approval boundaries. Unresolved Needs are visible advice, not a
new mandatory finish rule. Hosts without Need support and Domains without this
optional analysis port retain the existing workflow.

## Operation arguments

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
