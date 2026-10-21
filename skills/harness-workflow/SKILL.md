---
name: harness-workflow
description: Use Base Harness External to track and verify agent work when the user requests Harness-backed work or the workspace has opted into this workflow. Preserve the original goal, submit snapshots, inspect measured facts, and resume the correct Run. Does not provide an agent runtime or replace task-specific Domain reasoning.
---

# Harness workflow

Use the Host's existing model, editing and discovery tools. Harness owns the
original intent, policy, Run state, Candidate snapshots and verification records.
Installation alone does not opt unrelated work into this workflow.

## Connect once

Run bundled `scripts/harness_client.py` with the operator's persistent config,
selected by `--config PATH` or `BASE_HARNESS_CLIENT_CONFIG`. Use `preflight`
when the actual execution path is unknown. A configured `server_socket` uses an
operator-started external Harness; the same endpoint handles preflight and work.
Missing setup or an unavailable Sandbox is a limitation, not permission to run
verification directly on the Host. Do not guess another checkout or state store.

Use `describe --operation NAME` for the bundled Core input schema, including
nested assessment fields; add `--runtime` to query the configured Core. For
helper-only arguments use `describe --helper find` (or bind/context/checkpoint/wait).
Read [the protocol](references/protocol.md) for installation/recovery,
a missing schema detail, advanced records, or runtime transitions; ordinary calls
do not require loading the entire reference.

For Domain parameters, use `describe --domain develop --adapter ADAPTER_ID`.
It queries the configured registry once and returns the Domain and selector
schemas. Reuse those fields instead of reading helper implementation code.

Start with the original goal and constraints. Use the configured acceptance
policy when present. Otherwise explicitly choose the task's policy and declare
model-authored criteria as `provenance: {"declared_author":"model"}`; this is not
user approval. Unknown scope can remain exploratory and revisable.
Pass the full original request with `--goal-file /absolute/request.txt` or `goal`;
attach explicitly supplied specifications with `--source spec=/absolute/SPEC.md`.
Do not replace those originals with a summary. Requirement `source_refs` address
1-based inclusive lines in `goal` or a named source. Inspect `verification_plan`
for unmapped source IDs, missing scenarios and unsuitable observation kinds.
Prepare checks obvious omissions/contradictions; it does not fix the solution.
Use `call --operation start --arguments json:{...} --action-id ...
--output REQUEST_FILE --binding-output BINDING_FILE --format compact` to start
and save a binding in one call. The arguments are operation fields, not another
request envelope. `describe` includes the exact CLI template.
New starts prepared by the helper automatically select a separate task DB.
Keep the returned binding: it pins both `task_id` and `run_id`. A new session
continues that binding; an unrelated request uses a new start action.

For an existing Run, inspect its goal, workspace, Domain and lifecycle before
binding it. Never choose the newest Run automatically. Save one explicit binding
with `bind --task-id ... --run-id ... --workspace ... --domain ... --output ...`.
Omit task ID only for legacy Runs. For lost bindings or task discovery, see
[task storage and reusable notes](references/task-storage.md).
Bindings cache references only; Core remains authoritative.

## Investigate and implement

Use relevant Domain `analysis_guidance`: identify an unresolved fact, the design
decision it affects, and an observation that distinguishes plausible answers.
Selected perspectives connect inspection, decision and counterexample checks.
Use Host tools to inspect the relevant symbol, caller and assertion; stop when
the next action is supported. No Need IDs, quotas or extra tracking calls are
required. Preserve consequential decisions and sources when useful for handoff.

Use `analysis_selection` on start or read-only resume for needed perspectives or
knowledge entries. Omission retains the full map; `{}` restores the stored map.
Advice does not alter criteria or grant an operation. Revise checks/interpretation
only when their actual meaning or input scope changes.

Declare all code/fixture inputs needed for verification. Mandatory case checks
require an observer outside the Candidate process, such as
`process-observations-v1`. In-process pytest is an auxiliary framework report:
its `passed` or `executed` fields cannot satisfy a mandatory case gate.
Do not downgrade a pinned policy to accommodate an unavailable/unsupported check.
Tests, their author and configured approval are distinct; passing checks do not
prove that their criteria cover the entire goal.
For CLI behavior, the external process observer accepts disposable `fixtures`,
an `expected_exit_code`, and stdout/stderr or file-state cases. File observations
come from outside the Candidate. An `unchanged` case must name the actual file
whose preservation matters; a different file does not exercise that requirement.

## Submit, inspect, decide

At an intentional checkpoint use `checkpoint --binding ... --action-id ...
--wait-seconds 30`. It performs submit/verify/wait, not assessment or repair.
`pending` ends the observation window, not the job; wait on the same job.
A new action ID means a new intentional action. After response loss, replay the
IDENTICAL saved request/ID or query its Run; never automatically invent another.
For other mutations, `call --operation ... --arguments json:{...} --binding ...
--action-id ...` saves the exact request beside the binding before dispatch;
`--output REQUEST_FILE` overrides that location. Preserve the returned request
path for identical replay. Rebinding identical references is safe; conflicting
references are never overwritten.

After context loss, use `context --binding ... --complete` without a cursor and
read its contents. Afterwards use its cursor for deltas, or `resume` with
`view: compact` for progress. `operation_constraints` explains known blockers,
not authorization or proof that the runtime/file checks will pass.

Use `--format attention` on a bound status/resume read to collect failures,
requirement links and evidence boundaries. Preserve skips, unavailability,
framework-only reports, known gaps and pagination. Fetch an exact raw detail
with `records` and `--resolve-detail` when needed.
Capture a baseline before edits if comparing original and revised behavior.
A baseline improvement concerns that check, not complete goal correctness.
For newly added tests, `compare` measures both subjects with the same explicit
shared inputs; discover the Domain's `comparison_schema`. It is a diagnostic,
not normal verification or permission to finish. Neither side passing proves
that the test reached the intended failure. See the protocol for this optional mode.

When the user explicitly forbids changing particular existing files, declare
`workspace_constraints.unchanged_files` at start with their original source
references. Core checks those actual workspace bytes at submit/completed finish,
separately from product tests. No Host edit hook or continuous protection exists.

Verifier facts do not order a retry. Decide whether to repair, explore, retain
uncertainty or stop. Direct Host tests remain caller observations, not Harness
measurements. A Linux result does not establish Windows behavior.

## Finish or hand off

Compare current submitted files with the verified Candidate; inspect current
gates, observation scope, unresolved work and uncertainty before choosing
`finish`. Keep measured facts, model assessment and lifecycle closure separate.
Check Domain clarification separately: a `NEEDS_INPUT` diagnostic requests a
Domain declaration, not new approval or a weaker verification policy.
Do not edit Harness state, rewrite original intent, weaken mandatory criteria,
or quietly reset budgets through a new Run. A terminal Run/policy/runtime change
needs the explicit linked continuation described in the protocol.
An empty failure list with no declared requirements is unknown coverage.
Include the reported evidence gaps even when all configured checks passed.

When a reusable lesson exists, pass a selected note with `call --operation finish
... --knowledge NOTE_JSON`; `describe --operation finish` exposes its schema.
The application publishes it after Core closeout, without another model call.
Do not invent a lesson to fill a template. Treat `knowledge_publication.status`
separately from closeout; a publication failure needs `knowledge-publish`, not
another verification. For later work use `knowledge-find` then `knowledge-read`
on selected entries only. These notes are advice, not policy or current evidence.

Report the deliverable, actual measurements and skips, remaining uncertainty,
and Run ID. Results are unsigned `local-advisory`, `ready=false`; neither Skill
adherence nor a separate directory establishes protected certification.
