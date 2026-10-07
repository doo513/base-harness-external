# Base Harness External

A model-free Develop Harness called by an existing agent through a JSON CLI.
It owns independent Run state, pinned contracts, snapshots and verification jobs.
It does not include a model gateway, agent Host, UI or MCP server.

The repository also ships a portable [harness-workflow Skill](skills/harness-workflow/SKILL.md)
and a small caller helper. Follow [Skill + Harness installation](INSTALL.md) to
configure durable state and install the Skill into an existing Host. The Skill
guides the caller; Domain/Core remain the source of contract and lifecycle rules.
There is no second agent loop or Run database in the helper.

[Configured acceptance policies](POLICIES.md) separate operator-provided mandatory
checks and pinned test/fixture bundles from model exploratory probes. Optional
baseline comparison records check improvement or regression without claiming
goal correctness. The helper supports explicit `bind`, mechanical `checkpoint`
and bounded `wait`, without automatic assessment, repair or closeout.

For external callers and agent shell tools, see [caller usage](CALLER_USAGE.md).
Develop also supports [pytest case observations](POLICIES.md#pytest-case-observations):
exact scenario bindings, selected/executed/outcome facts and scoped requirement
reports. The optional pytest runtime is isolated in the existing Sandbox.
Domain compiles declarative links; Core joins their opaque IDs to scoped facts.
Worker does not import Develop to aggregate results, and the pytest adapter owns
its private execution command. See [responsibility boundaries](POLICIES.md#domain-core-and-execution-boundaries).
Applications can [register verifier adapters](POLICIES.md#adapter-interface-and-application-registration)
through the common port; detached Workers restore pinned registrations without
framework-specific dispatch code. Pytest is the only bundled production adapter.
`bash scripts/harness-tool` runs the checkout without an editable installation
and works from another working directory when given its absolute path.

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

Develop parameters are `inputs`, `artifacts`, `expectations`, `execution_checks`,
and optional `profile` / advisory `validation_profile`, `requirements` and `coverage`.
Every artifact requires a concrete expectation (`equals`,
`contains`, or `sha256`). The default `execution` profile requires at least one
execution check and always expects exit code zero. Each entry uses `kind: command`
with explicit `argv`, or `kind: cases` with a registered `adapter_id`, opaque
`selector`, and normalized case criteria. Explicit `structural` profile
requires file expectations and excludes commands; it says nothing about runtime
behavior. Missing criteria return `NEEDS_INPUT`, not invented success conditions.
Strict mode retains the original preparation requirements. Exploratory mode can
start before these parameters are known and returns a durable `waiting_input`
Run with Domain questions. Supply the missing parameters through `revise`.
The missing execution question is `develop:execution_checks`, with answer field
`execution_checks`; it does not require a particular test framework. Legacy
`test_commands` and the bundled adapter's `pytest_checks` alias remain accepted
through the application compatibility bridge. Do not mix nonempty legacy lists
with `execution_checks`. When switching an existing interpretation to the new
field, clear its old lists in the same `revise` request. See
[case-aware input and adapter schema ownership](POLICIES.md#pytest-case-observations).
Once input scope is known, Domain preparation can admit snapshots and exploratory
measurements even while final artifact/test criteria are incomplete. Its
`available_operations` distinguishes measurement readiness from completion
readiness; pending Domain questions still prevent `finish completed`.
Interpretation and advisory checks can change within that Run; pinned gate checks
and the initial policy cannot be weakened. A new policy requires a new Run.

Optional [requirement conditions](POLICIES.md#requirement-conditions) describe
input/output meaning, behavior, preservation, conflicts and failure handling.
Each declaration has a stable ID, a statement, optional applicability (`when`),
and explicit named Check references. The compiled GoalContract keeps these
conditions separate from revisable design assumptions in Interpretation.
`domain_preparation.requirements` and `resolution.requirements` expose unlinked
requirements, absent scenario mappings and declared gaps. These summaries do not
create checks, approval or additional completion gates. A passing Check still
does not prove the corresponding statement adequately describes the goal.

Develop returns `domain_preparation.analysis_guidance` (`develop-analysis-v2`):
quality cues for useful unknowns, perspectives on outcomes, existing behavior,
design, implementation impact and verification, and a bounded Knowledge Map.
The map connects questions to source hints and the design/code/check decisions
those sources can inform. The Host derives task-specific Needs in ordinary
reasoning and inspects only useful code, documents or observations. It does not
need to register, update or close Need records to use this advice.
See [focused discovery](skills/harness-workflow/references/protocol.md#need-analysis-and-focused-discovery).
Domain advice is pure data: no model/tool calls, keyword-based task router, Run
state machine, new success criteria or Core authority. Existing Core/Verifier
boundaries remain unchanged. The optional [legacy record API](skills/harness-workflow/references/need-records.md)
is retained for old clients, separate from the default discovery workflow.

### Common semantic records

The additive `closeout-semantics-v1` data contract separates immutable Intent and
initial Policy from revisioned Interpretation, Check definitions, measurements,
caller Assessment and Completion. References contain ID, revision and digest.
The storage `revision` field is a write counter, not an interpretation revision.

`start --mode exploratory` selects explicit-gate completion. Use repeated
`--required-check file-0` arguments to name required check IDs at initialization;
IDs for implicitly named Domain-generated checks are `file-0`, `file-1`, `command-0`, etc.
An initially unknown required check must be declared with `--deferred-check`;
its first definition is pinned when prepared.
Other proposed checks remain advisory. `--constraints constraints.json` records
the original constraint string array and passes it to the Domain preparation port.
The Core stores constraints without guessing semantic tests from their wording.
Gate records bind their initial policy, exact check revision, submitted subject
and `finish_completed` action; they do not authorize the caller's file edits.

The default `strict` mode is an explicit all-checks policy. Newly registered checks
are also gated under that policy. Policy origin is `initial_configuration`; it
does not authenticate a human or turn caller-authored criteria into independent
evidence. Both modes remain `local-advisory`, unsigned, `ready=false`.

### API v2 / 0.3 changes

New responses use `external-harness-v2`. New Job results use
`verification-result-v2`, with `observation_refs` instead of embedded observation
bodies. Retrieve bodies with `records --kind measurements`; their record hashes
are bound to the result. Historical aggregate Job results and request replays
remain readable in their original form. No bulk rewrite of old Run state occurs.

The CLI `status` default is now `--view summary`. `--view full` explicitly returns
the full Run/history. The Python `Harness.status()` default remains full for
embedded-call compatibility; pass `view="summary"` for bounded agent responses.
Use `resolution.display_status`, `measurement_status`, `assessment_status`, and
`gate_status` together. A requested completed disposition without measurement is
reported as `closed_unverified`; it is not a passing verification or certification.

Expectations and command execution checks may include an explicit `id` (for example
`{"kind":"command","id":"behavior","argv":["python3","-m","unittest"]}`), producing check ID
`domain.behavior`. Without it, Develop defines identity by file path/operator or
command argv/cwd. Case-aware checks require a logical `id`. IDs such as file-0 are assigned once, then retained by that
identity across reorder, insertion and reactivation. Multiple criteria with the
same implicit identity require explicit IDs. An ambiguous change is rejected
rather than guessed from a list position. Domain extensions can provide stable
`check_key` and optional `check_id` metadata, distinct from measurement parameters.

Unknown `--required-check` IDs are rejected. Use `--deferred-check domain.behavior`
when a required definition is intentionally supplied later. This pins the future
requirement without treating it as passed. The policy records the declaration,
and gate summaries distinguish a deferred definition from an old unspecified one.

`start --provenance` accepts declared_author (`user`, `model`, `application`, or
`unknown`) and an optional approval_reference. These remain caller claims:
approval is `not_provided` or `unverified`, with no authenticated approver. Neither
a role label nor an approval reference grants authority. Check proposals accept
provenance as well. Check records distinguish authored_by, generated_by and origin;
Domain preparation and caller proposals no longer both claim to be model-authored.

Every new Run pins Domain implementation and configuration identity. Method code,
source files, and JSON instance/class configuration are hashed. Modules with
non-JSON state must expose JSON `identity_config`; declare additional implementation
dependencies in `identity_files`. This is change detection, not isolation against
same-user tampering. Modules are responsible for declaring all relevant configuration
and dependencies. Historical Runs without this binding can be read and closed as
partial/abandoned; use a new Run for continued mutation/verification.

Domain identity v2 includes effective inherited JSON configuration in Python MRO
order, with instance overrides. Computed properties are not evaluated implicitly;
declare dynamic values in `identity_config`. The verifier execution manifest is
separate from the diagnostic deployment source hash: presentation edits alone do
not rebind measurements, while execution/invariant changes still require a new
Run. `doctor` reports transport/storage capabilities and deployment identity.

New Runs use `run_heads`, immutable `run_records` and ordered `run_events` in the
same SQLite database. Current reads do not hydrate historical collections, and
unchanged bodies are not rewritten. Measurements still have one canonical body.
Legacy `runs.data` rows remain readable without rewriting their contracts or
evidence. No protected certification or same-user tamper barrier is introduced.

Assessment records retain the caller's cited observation IDs and add kernel-derived
`citation_bindings`. Each binding reports whether the observation belongs to the
current subject, interpretation and check set. A citation is `current` only when
all three match; otherwise it is `contextual`. Contextual observations remain useful
history but are not presented as measurements of the current Candidate. Assessment
status and sufficiency remain untrusted caller judgments.

`check_set_hash` identifies the exact ordered frozen checks independently of
`contract_hash`. It is attached to Job, measurements, result and completion. The
per-check Measurement row is the canonical body; a Job result stores only bound
references. Read-only history queries use SQLite read transactions, and expiry
reconciliation takes a write transaction only when a transition is needed. The
store uses WAL to let readers proceed alongside writers.

All JSON options accept a file path, `-` for bounded UTF-8 stdin, or an explicit
`json:` inline prefix. Bare inline JSON gets an input-source error rather than a
file-open traceback. Only one option per call may consume stdin. Per-input bounds,
unique JSON keys and downstream schema checks remain enforced.

Default Job summaries, `resume`, full status and paginated Job history validate
v2 result references against their canonical Measurement records. Missing or
changed facts fail with `RESULT_BINDING`. If a request itself persists a deadline
or budget handoff, retrying that request ID replays the original terminal error
instead of returning a later generic `RUN_CLOSED` result.

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

### Core boundary fixes after 0.2.0

Job deadlines include every current check's timeout, including added and revised
checks, and remain capped by the original Run deadline. `closeout.lifecycle`
reports `closed`/`blocked` before clarification state and reports `verifying`
while a measurement job is active.

Omitting `open_questions` in a revision preserves the caller's previous questions;
an explicit empty list clears them. Domain preparation questions live separately
in `domain_questions` and are regenerated by preparation. Historical 0.2.0 records
that duplicated structured Domain questions into the interpretation remain readable.

`finish completed` enforces the contract's `snapshot_required` rule. Exploratory
completion with no required gates can still have `measurement: not_run` and
`assessment: not_assessed` after submission. Completion does not imply verification
or goal satisfaction; those fields and the configured gates must be read separately.

Snapshot copying runs outside the SQLite write transaction. Before attaching the
copy, Harness rechecks the Run revision, lifecycle and budget. A concurrent Run
change returns `SUBMISSION_CONFLICT`; an identical request already committed by
another caller replays that response without consuming a second action. Failed or
superseded captures are cleaned up. Abrupt process/OS termination can leave staging
files; the explicit `cleanup` command can quarantine eligible abandoned captures.

### Agent recovery tools

For lossless first-session recovery and small follow-ups, use `context --run-id ID`.
Read its `next_page` tokens to completion, then supply `--after NEXT_CURSOR` on
later queries. Full pages and deltas reconstruct the same document; text
substitution references remove repeated long strings without summarizing them.
The [caller protocol](skills/harness-workflow/references/protocol.md) describes
the decoder, pinned pagination and current critical-status banner. Omit the
cursor after Host context loss. Old `resume`/`status` response shapes remain valid.

After an execution-version change, keep using a pinned old checkout or explicitly
close the old Run as partial/abandoned and start a new one with
`--predecessor-run-id ID --continuation-reason TEXT`. The task identity and history
link remain visible; passing results and baselines are not silently imported.
Existing `--policy-change-reason` is preserved and is mutually exclusive with
the runtime continuation reason. Never upgrade an actively executing checkout.

`doctor` checks the selected state store and local prerequisites. `doctor --sandbox`
also executes a controlled command through the real strict adapter; it exits with
code 2 if that probe cannot pass. The JSON `ok` field still describes API handling,
and `healthy`/`command_execution_verified` describe readiness.

`list-runs --offset 0 --limit 20` finds Runs independently of the Host session.
`resume --run-id ID` returns the current intent, interpretation summary, questions,
budgets, candidate identity, assessment, gates and recent Job summaries. It does
not resume execution. Use `records --run-id ID --kind measurements --limit 5` for
history pages; `next_offset` identifies the next page. Other kinds are checks,
interpretations, activity, assessments, tasks, notes and jobs. Measurement pages
can be filtered by `--job-id`. Limits are 1..100. Use `status --view full` for
the full response. Recovery gate summaries load and validate the latest relevant
measurements without returning duplicate process output.

`retire-check --run-id ID --data proposal.json --request-id KEY` accepts
`check_id`, `expected_revision`, `interpretation_revision` and `reason`. It retires
only an advisory caller check; required gates cannot be retired. Reactivation uses
`check` with the retired check's current revision. Domain-generated checks are
removed through `revise` parameters. Check revisions advance across retirement
and reactivation. Scope/profile revisions revalidate active checks through the
Domain port and fail atomically with `CHECK_SCOPE_CONFLICT` if a retained check
no longer applies. Explicitly revise or retire that check first.

`cancel --run-id ID --job-id ID --request-id KEY` cancels only that verifier job.
Like finish, cancellation remains available without an extra action-budget slot.
It preserves the Run and cumulative budget; retrying verification consumes a new
attempt. Queued cancellation is immediately cleaned up, while a running worker
acknowledges cleanup later. Old cancellation/startup failures cannot clear a newer
Job owner or rewrite a closed Run.

`cleanup --run-id ID` previews capture recovery. `--apply` moves abandoned
`.submit_*` directories into recoverable `.quarantine_*` directories within that
Run. Minimum age defaults to 3600 seconds (`--min-age-seconds`, minimum 60).
An OS lock excludes live captures; unknown legacy ownership is reported and left
untouched. This POSIX-only recovery never deletes data or moves published Candidates.
There is no background janitor or automatic quarantine deletion.

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

Current reports also record `head_commit`, `dirty` at test start, and
`tested_tree_digest` over implementation, test and evaluator sources. Generated
reports are excluded from that digest. `base_commit` remains a compatibility alias
for the checked-out HEAD; with `dirty: true`, it is not the full tested source.
Source changes during evaluation, missing/failed required scenarios, and skipped
required scenarios fail the evaluation command. A scenario may explicitly declare
`required: false` to allow a skip; current scenarios are all required. Historical
validation files retain their original test-time source identity.
