# Base Harness External

A model-free Develop Harness called by an existing agent through a JSON CLI.
It owns independent Run state, pinned contracts, snapshots and verification jobs.
It does not include a model gateway, agent Host, UI or MCP server.

## Installation

Python 3.11+; no Python runtime dependencies. From the repository root:

```sh
python3 -m pip install -e ".[dev]"
base-harness-external --help
```

Without installation, use `PYTHONPATH=src python3 -m harness_external` on Linux,
or set `PYTHONPATH` to this checkout's `src` in your platform's shell. Command
checks reuse the existing Bun/WSL-or-Linux-namespace Sandbox through a small
adapter; file-only structural checks do not need Bun. In this first release,
command checks require this source checkout and Bun on PATH (or `BUN`). A missing
adapter/Sandbox returns incomplete verification, never an unsandboxed fallback.

The default independent state directory is `$XDG_STATE_HOME/base-harness-external`
(falling back to `~/.local/state/base-harness-external`) on Linux, or
`%LOCALAPPDATA%/base-harness-external` on Windows. Override with `--state-dir`.
State and workspace must be disjoint. `runs.sqlite3` stores Runs, jobs, contracts,
untrusted notes and verification records; `runs/<run_id>/<candidate_id>/payload`
stores snapshots. No Host session database or authentication is used.

### First actual run

The repository includes a small addition implementation and real unittest:

```sh
base-harness-external start --domain develop --goal "Implement integer addition and test it" --workspace examples/external-harness/project --parameters examples/external-harness/parameters.json --request-id demo-start-001
base-harness-external submit --run-id RUN_ID_FROM_START --request-id submit-001
base-harness-external verify --run-id RUN_ID_FROM_START --request-id verify-001
base-harness-external status --run-id RUN_ID_FROM_START --job-id JOB_ID_FROM_VERIFY
base-harness-external finish --run-id RUN_ID_FROM_START --outcome completed --request-id finish-001
```

`verify` returns a durable `job_id` immediately. Poll `status` until the job is
terminal. The default strict policy requires every registered check to pass for
`finish completed`; exploratory policy evaluates only its declared gates.
Use new request IDs for intentional new operations. Retrying the same operation
with the same ID returns the original response; reusing an ID for different input
is an error. A `start` ID identifies that specific Run, not a reusable template.

The agent may edit the workspace with its existing tools between attempts. Use a
new `submit` and `verify` request ID after changes. A new submission invalidates
the prior verification. Harness has no file-editing/apply API and never publishes
changes into the user's workspace.

### API and contract

- `start`: pin original goal, Develop policy revision, parameters and verifier
  implementation digest. `--budget` accepts a JSON file containing `max_actions`,
  `max_verifications`, `timeout_seconds` (defaults 50, 3, 3600).
- `observe --data note.json`: append `{ "note": "...", "references": [] }` as
  **untrusted caller data**. References are not fetched. This cannot create a
  verifier result or change the contract.
- `revise --data revision.json`: revise the working interpretation and Domain
  parameters in the same Run using `expected_revision`. Original goal, constraints,
  initial policy, elapsed time and cumulative budgets remain fixed.
- `check --data check.json`: propose or revise an advisory check, bound to an
  `interpretation_revision`. A pinned gate's check cannot be weakened or changed.
- `assess --data assessment.json`: append a caller assessment with status,
  uncertainty and exact verifier observation references. It never overwrites facts.
- `submit`: capture only the contract's explicit input files; reject links,
  traversal, special files and inputs exceeding the v5 10 MiB combined bound.
- `verify`: run existing Python v5 measurements against that snapshot; test
  commands run only in the existing disposable Sandbox. Submitted-file changes
  detected in the test sandbox invalidate that command result.
- `status`: retrieve durable state; `--check-workspace` additionally compares the
  current listed workspace files/modes against the submitted snapshot.
- `finish --outcome completed|partial|abandoned`: close the Run. Partial/abandoned
  can stop a pending job; the job's `cleanup` field distinguishes a request from
  acknowledged cleanup. A stale worker cannot publish into a closed/newer Run.

Develop parameters are `inputs`, `artifacts`, `expectations`, `test_commands`, and
optional `profile`. Every artifact requires a concrete expectation (`equals`,
`contains`, or `sha256`). The default `execution` profile requires at least one
test command and always expects exit code zero. Explicit `structural` profile
requires file expectations and excludes commands; it says nothing about runtime
behavior. Missing criteria return `NEEDS_INPUT`, not invented success conditions.
Strict mode retains the original preparation requirements. Exploratory mode can
start before these parameters are known and returns a durable `waiting_input`
Run with Domain questions. Supply the missing parameters through `revise`.
Once input scope is known, Domain preparation can admit snapshots and exploratory
measurements even while final artifact/test criteria are incomplete. Its
`available_operations` distinguishes measurement readiness from completion
readiness; pending Domain questions still prevent `finish completed`.
Interpretation and advisory checks can change within that Run; pinned gate checks
and the initial policy cannot be weakened. A new policy requires a new Run.

### Common semantic records

The additive `closeout-semantics-v1` data contract separates immutable Intent and
initial Policy from revisioned Interpretation, Check definitions, measurements,
caller Assessment and Completion. References contain ID, revision and digest.
The storage `revision` field is a write counter, not an interpretation revision.

`start --mode exploratory` selects explicit-gate completion. Use repeated
`--required-check file-0` arguments to name required check IDs at initialization;
IDs for Domain-generated checks are `file-0`, `file-1`, `command-0`, etc. If a
named check is initially unknown, its first definition is pinned when prepared.
Other proposed checks remain advisory. `--constraints constraints.json` records
the original constraint string array and passes it to the Domain preparation port.
The Core stores constraints without guessing semantic tests from their wording.
Gate records bind their initial policy, exact check revision, submitted subject
and `finish_completed` action; they do not authorize the caller's file edits.

The default `strict` mode is an explicit all-checks policy. Newly registered checks
are also gated under that policy. Policy origin is `initial_configuration`; it
does not authenticate a human or turn caller-authored criteria into independent
evidence. Both modes remain `local-advisory`, unsigned, `ready=false`.

Start an exploratory Run with just the original goal and workspace:

```sh
base-harness-external start --mode exploratory --domain develop --goal "Investigate the requested change" --workspace /path/to/project --request-id start-001
base-harness-external status --run-id RUN_ID_FROM_START
base-harness-external revise --run-id RUN_ID_FROM_START --data revision.json --request-id revision-001
```

For example, `revision.json` can contain:

```json
{
  "expected_revision": 1,
  "goal_summary": "Current working interpretation of the original goal",
  "parameters": {
    "profile": "structural",
    "inputs": ["result.txt"],
    "artifacts": ["result.txt"],
    "expectations": [{"path": "result.txt", "operator": "contains", "expected": "expected text"}]
  },
  "assumptions": [],
  "open_questions": []
}
```

Optional structured `observe` records use `kind: hypothesis|decision|task|note`,
`observation_ids` and `interpretation_revision`. Logical task records can name
`task_id`, an existing `parent_task_id`, prior `depends_on` IDs and a reported
`pending|settled|blocked` state. These are caller reports, not executed-agent
identities or a process scheduler. Free notes remain available.
Task state updates supply the current `task_revision`; the original reported
objective and topology stay pinned. Activity records can use
`related_activity_id` plus `supports|refutes|informs|supersedes`, and a `revise`
proposal can cite `activity_ids` as its basis. These relationships remain caller
claims and do not authenticate reasoning or turn a claim into a verifier fact.

An Assessment JSON has `interpretation_revision`, `status` (`satisfied`, `partial`,
`unsolved`, `not_assessed`), `summary`, `uncertainties`, and
`cited_observation_ids`. References must name verifier observations owned by the
same Run; free notes cannot masquerade as measured evidence. `finish --assessment`
can attach a final assessment atomically, including during a budget handoff.

`status` returns a `closeout` view with independent measurement, assessment,
gates, lifecycle and termination reason. A failed advisory comparison can coexist
with passed required gates, a partial model assessment and a closed Run. A failed
required gate cannot become passed because the caller declares satisfaction.
Completion also records unresolved logical work and verifier cleanup requests.

Every completed check is saved in the `measurements` table before the next check
starts, with Run/job/attempt/check/subject references. Partial measurements survive
errors, cancellation and worker interruption. They do not become a final batch
success or automatically pass gates for a new snapshot/check revision. Historical
facts remain available after resubmission. Old strict records are projected for
reading with `semantic_projection_origin: legacy_strict_projection`; no worker is
resumed and legacy aggregate observations are not silently reissued as checkpoints.

Domain injection uses `DomainRegistry` and its `prepare`/`normalize_check` port.
Core owns state, provenance, references and execution. Develop's existing artifact,
file-comparison and command-exit criteria remain in the Domain implementation.
No new Develop success rules are inferred in Core.

Run/job state survives CLI exit. Workers use durable ownership and heartbeat
leases; killed workers become `interrupted` when observed, and are **not silently
restarted**. A retry is a new explicit verification attempt against the pinned
snapshot. Budgets cover only Harness work, not external model tokens or actions.
Exhaustion returns a handoff state; it cannot stop the agent outside the Harness.

### Honest assurance boundary

All responses say `assurance: local-advisory`, `ready: false`. A finished record
binds the Run, candidate hash, contract hash, verifier implementation and result
hash, but has **no signature**. Digests detect accidental mismatches; they do not
protect against a same-user model rewriting state, verifier code and hashes.
This is not the legacy GoalContract-v2/Ready protocol and does not invoke its
Ready issuer. A passing check does not prove goal coverage or test independence.

Results describe the submitted snapshot, not later workspace edits or unlisted
files. Command observations retain v5's environment/input-closure limitations.
For tamper-resistant certification, use a separately protected verifier/state/key
and an independent consumer (for example CI); that protected mode is not included.

Tests: `python3 -m pytest -q tests/test_external_harness.py`. On a machine allowing
namespace sandboxing, run the actual execution check with
`BASE_HARNESS_EXTERNAL_LIVE=1 python3 -m pytest -q -s tests/test_external_harness.py -k live_sandboxed`.
Windows-native operation and packaged standalone command-sandbox distribution
have not been verified. Python-only structural execution works without the old Host.

## Validation

This extracted repository was checked independently on WSL/Linux with Python
3.12 and Bun 1.4.0: 50 Python tests passed (one opt-in test skipped), the real
namespace Sandbox test passed separately, and three Sandbox unit tests passed
(three Windows-only tests skipped). No commercial-model quality claim is made.

Run the complete local suite with `python3 -m pytest -q tests`, and the Sandbox
unit suite with `bun test tests/sandbox.test.ts`. See `NOTICE` for extraction
provenance. No local Run databases, credentials, or execution logs are included.

The original extraction counts above are historical. Common semantic restoration
is evaluated separately by `evaluation/closeout-scenarios.json` and
`tests/test_closeout_semantics.py`.

For 0.2.0, the complete Python suite passed 69 tests (one opt-in skipped), all
19 common semantic scenarios passed, and the real namespace command check
passed separately. See `evaluation/validation-0.2.json` for the recorded scope.

Run:

```sh
python3 scripts/evaluate.py --output evaluation/latest.json
```

The report binds the source digest and lists each scenario's result, including
advisory failure vs required gates, two interpretation revisions, lost Host
context, real worker interruption, stale observations and v1 record compatibility.
It is scripted common-track conformance, not a commercial-model quality or
speed comparison. The worker-kill case uses a controlled blocking second-stage
adapter; the namespace command execution check remains a separate opt-in test.
