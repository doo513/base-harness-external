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
The helper sends one `invoke --request` envelope to the existing CLI. Optional
`server_socket` sends that same envelope to an operator-started local Harness
outside the Host's process restrictions. It creates no second Run store and
never falls back to local execution. Preflight and task requests share the
endpoint; state/policy configuration must match the server. Optional `policy_root`
selects an operator-configured acceptance registry. It is configuration, not a
field the caller can replace in a task request. Configured Domain defaults cannot
be overridden by selecting a weaker policy.

## Low-overhead workflow

`describe --domain develop [--adapter process-observations-v1]` queries the
configured Harness for Domain parameters and registered selector schemas. It
creates no Run and does not probe runtime availability. Local `describe
--operation NAME` remains available without configuration.

On direct `call --operation start`, `--goal-file /absolute/request.txt` supplies
`goal` verbatim; repeated `--source spec=/absolute/SPEC.md` supplies original
documents as `sources`. Do not also supply the same argument in `--arguments`.
The saved request contains the text, so replay with `--request` never re-reads a
changed source file. Core accepts at most 16 documents, 64 KB each / 128 KB total;
the enclosing request still has its usual 256 KiB bound. Source hashes identify
caller-supplied bytes, not authenticated user approval or complete interpretation.

Requirements can contain `source_refs: [{"source_id":"spec","start_line":1,
"end_line":3}]`. The reserved source ID `goal` addresses the original goal.
`verification_plan` checks explicit links, literal R-numbered labels and declared
observation kinds. Gaps are advisory and bounded; an omitted diagnostic count
means the first page is incomplete. `verification_scope` is present on compact
reads and finish responses. Job-only polling deliberately does not re-evaluate
all gates; `gate_status: not_evaluated` requests a Run read when a gate decision
is needed. No failure entries does not mean that undeclared requirements passed.

For optional cost measurement put `--trace-file /outside/workspace/cost.jsonl`
before the helper command. It appends timing/size rows without prompts, source
text or command output. `client_cost` distinguishes transport time and time
outside transport; transport includes any requested wait. Trace write failures
are reported without changing the outcome or retrying a committed operation.
`python3 <harness-repo>/scripts/report-caller-cost.py /absolute/cost.jsonl --run-id RUN_ID`
summarizes calls, repeats, unknown outcomes and bytes. Optional `--model-usage`
accepts a normalized Host report with `source`, `run_id`, `model`, optional
`input_tokens`, `output_tokens`, `cache_read_tokens` and `elapsed_seconds`.
Those remain Host-reported counters; absent tokens/billing are null, not inferred
from response bytes. Job `timings.sandbox` reports setup/execution/observation/
cleanup within worker time; do not add nested timing categories together.

`describe --operation NAME` returns actual accepted fields and a CLI template
without loading configuration or invoking Core. A missing installation reports
`CONFIG_MISSING`; select its config or set `BASE_HARNESS_CLIENT_CONFIG` once.
Core validation errors can include bounded `diagnostics` with JSON-pointer
`path`, `code`, and `message`, plus `diagnostic_count` and
`diagnostics_truncated`. Fix independent input errors together. This does not
authorize automatic retries/new IDs after an unknown mutation outcome.

For a fresh Run, `call --operation start ... --output REQUEST_FILE
--binding-output BINDING_FILE --format compact` saves the returned context
references without another resume call. The exact start request is still saved
before dispatch, and identical replay can finish saving the same binding. A
conflicting binding is never overwritten. `compact` start/finish output keeps
criteria, policy, uncertainty and recovery references but omits implementation
manifests; it is not a complete context transfer.

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

For other operations, one helper invocation can construct and send a request:

```sh
python3 <skill>/scripts/harness_client.py --config /absolute/client.json call \
  --binding /absolute/task-binding.json --operation observe \
  --arguments 'json:{"data":{"note":"Decision and next step"}}' \
  --action-id note-1 --output /absolute/note-1.json
```

Mutations require `--output`: the normalized exact request is saved before dispatch.
An existing identical file is reused; different content is rejected without sending.
Responses include `caller_request` so the same file can be replayed with `call
--request`, including after a timeout. A killed foreground process can also leave
that known output file; do not invent a new action ID. Read operations need neither
an action ID nor an output file. This removes a helper process, not necessarily a
model turn: a Host could already batch shell commands. The helper does not retry
automatically or store another authoritative Run ledger.

The original two-step form remains available: `request --binding ... --operation observe --arguments
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

For a single validated transfer, add `--complete` (no `--page`): the helper follows
all pages, validates restored entry/text hashes and coalesces long strings across
pages. The result is `harness-client-context-v1`: `data`, `text_refs` and `texts`
use short local aliases (`t0`, `t1`, ...), which are not Core IDs. `data_hash` covers
the exact restored dictionary. Read the referenced texts; machine consumers may
use the bundled `expand_context()` decoder. Goals, constraints, checks and history
are preserved. `critical` remains the latest returned Core status; an older
`snapshot_critical` remains distinct. `--complete --after CURSOR` contains only
changes, not a complete historical document. The helper stores no cursor or data
cache in the binding and returns no complete cursor on corruption, byte/page limit
or changed snapshot. `--max-pages` bounds the sequence; legacy full-only transfers
retain a null cursor. Plain page mode remains available.

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

After restoring context, use `resume` arguments `{"view":"compact"}` for repeated
progress checks. `summary` remains the default and includes fuller working
context. Compact `details.snapshot` identifies the source; it is not a cursor
proving that the Host has received the omitted context.
`operation_constraints` lists known blockers for submit, verify and completed
closeout. It is a read projection, not authorization: invocation still checks
the current revision, files, runtime and execution ownership.

Case observations distinguish `external_capture` from `subject_process`.
In-process pytest reports are auxiliary and cannot be mandatory gates. Use an
external observer for mandatory case facts. A reported pass remains available
for diagnosis/citation, but `reported_cases_passed` does not mean
`linked_cases_passed`. Historical reports without a boundary do not become
independent evidence merely by being read with a newer client.

For large records, `records` with `view: "references"` returns
`record-projection-v1` items. Each has `source` (`run_id`, `kind`, `record_id`,
`record_digest`), `data` and `detail_refs`. Null placeholders in `data` are
replaced by details identified by a JSON `path` and `sha256`. Request only the
details needed for the next decision, instead of automatically downloading all
records. Normal `view: "full"` reads remain available.

To read a detail, keep the same Run and kind, combine `record_id`/`record_digest`
from `source` with `path`/`sha256` from that detail reference, and send them as a
JSON object in `detail`. For example, create a binding-aware request with:

```sh
python3 <skill>/scripts/harness_client.py request --binding /absolute/task-binding.json \
  --operation records --arguments 'json:{"kind":"measurements","view":"references","limit":5}' \
  --output /absolute/measurement-refs.json
```

After calling that request, the next request's arguments can be:

```json
{
  "kind": "measurements",
  "detail": {
    "record_id": "OBSERVATION_ID_FROM_SOURCE",
    "record_digest": "DIGEST_FROM_SOURCE",
    "path": ["report", "observation", "result", "findings", 0, "observed"],
    "sha256": "HASH_FROM_DETAIL_REFERENCE",
    "offset": 0,
    "limit": 4096
  }
}
```

Use the exact returned path, not the example path. A detail request omits the
outer `view`, `job_id`, `offset` and `limit`; its byte range belongs inside
`detail`. Responses use `record-detail-chunk-v1` with `base64-canonical-json`
encoding. Decode chunks to bytes and append them at their returned offsets;
follow `next_offset` only if the complete detail is needed. UTF-8/JSON decoding
and SHA-256 verification apply to the assembled bytes, which can split a
multibyte character or JSON token. Restore the value at its exact path. Full
record reconstruction must also match `record_digest` (the Python reference
decoder is `harness_external.detail_projection.restore`). If Core returns
`DETAIL_CHANGED`, discard partial bytes and inspect a fresh projection; never
combine chunks from different versions. Read requests use no action/request ID.

The helper can perform that selected-detail assembly directly:

```sh
python3 <skill>/scripts/harness_client.py call --binding /absolute/task-binding.json \
  --operation records --arguments /absolute/selected-detail-arguments.json --resolve-detail
```

The arguments contain only `kind` and `detail`. Resolution starts at byte zero,
decodes UTF-8/JSON only after the full SHA-256 matches, and returns
`harness-client-detail-v1` with `value` plus source identity. It does not resolve
every reference automatically. A changed source returns an error, not mixed bytes.

For exception-focused progress reads, use `call --operation status --binding ...
--arguments json:{"job_id":...} --format attention`. The helper uses existing
summary data and a bounded case query with `outcomes` and `subject_role: candidate`.
`harness-client-attention-v1` includes observed nonpassing states, linked requirement
IDs, error excerpts and exact detail arguments. Excerpts are not root-cause claims;
arbitrary adapter phase data remains available by reference. `case_query.next_offset`
and `attention_page_complete` describe pagination, not verified completion.
Skipped/xfail/xpass outcomes are attention items, not necessarily failed policy
checks. `not_queried` explicitly identifies omitted Run gates/assessment/limits;
Job-only reads do not invent them. A selected historical Job is not automatically
current evidence. This projection is for follow-up after context restoration and
does not silently finish, repair, change criteria or hide known gaps.

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
`budget`, `constraints`, `provenance`, `assessment`, `analysis_selection`, `detail`) take real objects/arrays here,
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

`analysis_selection` is an optional top-level argument of `start` and `resume`,
separate from contract `parameters`. For example, a binding-aware read can use:

```sh
python3 <skill>/scripts/harness_client.py request --binding /absolute/task-binding.json \
  --operation resume --arguments 'json:{"view":"compact","analysis_selection":{"perspectives":["change"]}}' \
  --output /absolute/change-advice.json
```

Call that saved request normally. It returns the selected advice under
`domain_preparation.analysis_guidance`, including with `view: compact`.
Develop perspective IDs are `outcome`, `existing_behavior`, `design`, `change`,
`verification`; knowledge IDs are `compatibility`, `structure`, `runtime`,
`recovery`, `verification`. Select useful IDs from the task, not a fixed quota.
If perspectives are supplied and knowledge is omitted, their linked knowledge
is included. To request only knowledge, use e.g.
`{"perspectives":[],"knowledge":["runtime"]}`. Explicit lists are respected;
empty lists omit those entries, while `{}` or no selection returns the full map.
Unknown/duplicate IDs fail explicitly.

Selection changes only the response. Core keeps the canonical map for lossless
`context` restoration; subsequent reads can choose another subset without a
revision, verification attempt or action budget charge. `source_map_hash`
identifies that stored map and `map_hash` identifies the selected view. A resumed
selection uses the Run's recorded advice, even when the installed map has changed.
Legacy Domains without this optional capability retain their default responses
and return `DOMAIN_ANALYSIS_UNSUPPORTED` for a requested selection. A `start`
request still needs its normal action ID; changing its selection changes its
replay payload. Use read-only `resume` to choose a different view of that Run.

Need discovery uses the Host's ordinary reasoning, not a registration API.
Use the Domain's quality cues and analysis perspectives to identify an unresolved
question that affects a concrete decision, select relevant knowledge sources, and
apply the finding to design, implementation or verification. Do not read every
source hint or create a question merely to populate a category. No Need IDs,
revisions or state transitions are necessary for this workflow.

Record a consequential decision/source with the existing note/decision mechanism
only when useful for handoff. Core still owns state, references, authority and
verification boundaries; these Domain hints do not alter them.

The existing optional record API remains supported for compatibility. Read
[legacy Need records](need-records.md) only when that tracking is explicitly
requested, not as a prerequisite for analysis or completion.

## Operation arguments

Use the same context with these operations/arguments:

| Operation | Arguments in addition to `run_id` | Request ID |
|---|---|---|
| `submit` | none | required |
| `verify` | optional `compare_baseline`, `expected_candidate_hash`; returns `job_id` | required |
| `status` | optional `job_id`, `check_workspace: true`, `view: "summary"` | omit |
| `resume` | optional `view: "summary" / "compact"`, `analysis_selection`; query only | omit |
| `records` | `kind`; page: optional `job_id` (measurements/cases), `offset`, `limit`, `view: "full" / "references"`; cases only: `outcomes` string list, `subject_role: "candidate" / "baseline"`; detail: `detail` object | omit |
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
