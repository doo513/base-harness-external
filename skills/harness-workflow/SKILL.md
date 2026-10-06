---
name: harness-workflow
description: Use Base Harness External to track and verify agent work when the user requests Harness-backed work or the workspace has opted into this workflow. Preserve the original goal, submit snapshots, inspect measured facts, and resume the correct Run. Does not provide an agent runtime or replace task-specific Domain reasoning.
---

# Harness workflow

You solve the user's task with the Host's existing tools. Harness owns Run state,
contracts, submitted snapshots and measured facts; it does not call a model.
Use this workflow for an explicitly requested Harness task or an opted-in workspace.
Installation alone does not opt every unrelated task into Harness.

## Connect and prepare

Read [the caller protocol](references/protocol.md) for setup and first use. Run the
bundled `scripts/harness_client.py` with the persistent installation config. Use
`preflight` when execution capability is unknown, `bind` once for an explicitly
selected Run, and `checkpoint` for submit/verify/wait. These helpers make no task,
assessment or closeout decisions. Use saved `request` envelopes for other actions.
On a new session, use `context --binding ...` without a cursor and read every
`next_page`. Afterwards, use the returned `next_cursor` with `--after` for changes.
If the Host loses context, request a full transfer again; a saved binding is not
evidence that the model remembers earlier pages. Inspect `critical` even when the
delta is empty. The protocol explains lossless text references and snapshot pages.
If configuration/runtime is absent, report the missing setup. Do not guess another
checkout, create a temporary production state store, or silently bypass Harness.

- Preserve the user's original request and constraints. Prepare checks only obvious
  missing information, contradictions and scope misunderstandings; interpretation
  is revisable, not the definitive solution. Ask only when ambiguity changes scope,
  authority or the intended result.
- For a resumed task, find and inspect the existing Run's goal, workspace, Domain,
  lifecycle and active job. Explicitly use that Run ID. Never select a Run merely
  because it is newest, or hide missing state by starting another one.
- Use the operator-configured Domain acceptance profile when one is installed;
  `mode: acceptance` keeps mandatory checks/test bundles separate from your
  exploratory probes. Do not create or edit operator approvals as part of solving
  the task. Without a configured policy, label caller-defined criteria and coverage
  limitations honestly; do not invent independent approval.
- A binding file only caches immutable references. Core remains authoritative.
  Give each intentional action an ID; repeat that ID only to retransmit the same
  request. Keep the exact saved request after response loss. A new checkpoint ID
  means a new submission, not a retry of a lost response.

## Explore, execute and measure

When the Domain exposes `analysis_guidance`, derive the questions needed for the
next useful action: what must be known, observed, decided or measured? Its Knowledge
Map is an index, not a reading list. Select relevant entries after forming a Need;
use Host tools to inspect only the useful code/docs or run a bounded observation.
Unknowns outside the map are allowed. Do not require every category or settle all
questions before acting. Ask the user when a missing decision changes authority,
scope or the intended result, rather than silently choosing it.

Record material analysis changes with `observe kind=need`; see the
[Need record format](references/protocol.md#need-analysis-and-focused-discovery).
State what would be sufficient to proceed and connect conclusions to source,
decision, Check or measurement references. Reading a document alone does not address
a Need. Conflicts or missing support remain open/deferred; addressed is your judgment,
not verification. Reopen it when contrary evidence appears. Prefer brief conclusions
and locators over copied documents or a record per file read. Stop searching once
the relevant question is sufficiently answered, or preserve uncertainty at a limit.
Avoid mirroring the same question into both a Need and `open_questions`/task notes.
Only revise the contract/checks when their actual meaning or scope changes; routine
Need updates must not invalidate a verified Candidate. On recovery, inspect the Need
summary and context-change hints without rereading unchanged source material blindly.
When recording an answer, reuse the saved question/details and the protocol's
update example. Reword a question only when the actual uncertainty changes.

Plan, decompose, delegate when appropriate and try alternatives using the Host's
existing capabilities and permissions. Do not add a model loop to Harness or make
the helper decide the solution. Skill instructions do not expand authorization.

Revise the working interpretation/check proposals when observations warrant it.
Declare the complete input scope needed by the chosen checks, including tests and
fixtures. Use task/Domain reasoning to consider the intended runtime environment:
a Linux measurement is not evidence of Windows behavior. Missing execution support
is an explicit limitation, not permission to bypass the strict Sandbox.

At meaningful checkpoints, use `checkpoint --binding ... --action-id ...`.
`wait_status: pending` means the observation window ended, not that the job failed;
use `wait` for the same job. After an intentional edit, use a new checkpoint ID.
For change/regression evidence, capture the baseline before editing and request
`--compare-baseline`. An improved check does not prove the whole goal was met.

Read actual observations, `measurement_scope`, requirement links and limitations.
A process exit is not a count of tests or independent goal validation. A mandatory
command that cannot run stays unavailable, not replaced with a substring check.
Verifier facts do not order
a retry: you decide whether to revise, try another method, stop partially, or ask
the user. Keep caller observations and direct Host tests distinct from Harness
measurements. Do not convert a test skip or unavailable runtime into a pass.

## Close out or hand off

Before reporting success, compare current submitted files with the verified
Candidate, inspect gates and unresolved work, and assess the observations that
actually concern the current Candidate/check set. A past measurement can inform
reasoning but is not current proof. Report scope changes rather than rewriting the
original goal. Do not erase failed attempts or reset budgets by quietly creating Runs.

Use `finish` with the disposition supported by the facts and current policy. A
mandatory-policy change requires explicit operator configuration and a new Run
linked to its terminal predecessor and reason; do not quietly reset the task.
On a budget/deadline boundary, explain the remaining work and preserve the Run for
inspection; do not continue by silently resetting the policy. On interruption,
record a concise decision/next step when possible and restore `context` next time.
Legacy `resume` remains available. A runtime-version change needs the old runtime
or an explicitly linked new Run; never transfer old passing evidence as new proof.

The final user report needs the deliverable, actual measurements and skips, remaining
uncertainty and the Run ID (or a linked report containing it). Distinguish measured
results, model assessment and lifecycle closure. All current results are unsigned
`local-advisory`, `ready=false`, not protected certification. Skill adherence is not
a security boundary, and a separate directory does not defeat same-user tampering.
