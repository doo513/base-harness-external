---
name: harness-workflow
description: Use Base Harness External to track and verify agent work when the user requests Harness-backed work or the workspace has opted into this workflow. Preserve the original goal, submit snapshots, inspect measured facts, and resume the correct Run. Does not provide an agent runtime or replace task-specific Domain reasoning.
---

# Harness workflow

Use the Host's model and ordinary development tools. Domain supplies criteria and
reasoning guidance; Core owns intent, state and references; Worker/Verifier execute
and observe. Model claims, measurements, gates and task closure remain separate.
Installation alone does not opt unrelated work into this workflow.

## Connect once

Use bundled `scripts/harness_client.py` with the operator's persistent config
(`--config` or `BASE_HARNESS_CLIENT_CONFIG`). New local configs pin Python; a
configured socket uses the operator-started external Harness. Run `preflight`
when the execution path is unknown. Missing setup, a changed interpreter or an
unavailable Sandbox is a limitation, not permission for an unsandboxed fallback.

Start with the full original request (`goal` or `--goal-file`) and attach supplied
specifications with `--source spec=/absolute/SPEC.md`. Use configured acceptance
when present; otherwise explicitly select policy and label model-authored criteria
with `provenance: {"declared_author":"model"}`, not user approval. Do not weaken
mandatory criteria or overwrite the original intent.

Discover syntax only when needed: `describe --operation NAME` exposes input
fields and supported response formats; `describe --domain develop --adapter ID`
exposes Domain/adapter selectors. Start using `call --operation start` with
`--action-id`, `--binding-output` and `--format compact`. The helper saves the
exact request and binds the returned task/Run without another resume call.
Keep that binding; never silently choose the newest Run or another state store.

## Develop autonomously

Use relevant `analysis_guidance` to identify a question that changes a decision,
inspect the relevant code or observation, and choose the next action. No Need
quota, routine note, `observe`, `revise` or `assess` sequence is required.
Revise only when interpretation/check scope actually changes. Use
`analysis_selection` for focused guidance; it does not change criteria.

Optional deterministic discovery is available through:

```sh
python3 <skill>/scripts/harness_client.py explore context /absolute/project \
  --changed src/example.py --limit 8 --format json
```

It runs locally, not through Core, and emits no verification evidence. Optional
`--cache-dir /outside/project/cache` reuses syntax; `--refresh` rereads content.
Retain cache freshness, omissions and unresolved imports. Suggested tests are
impact candidates, not permission to omit mandatory tests.

## One verification checkpoint

Declare code, tests and fixtures needed for the snapshot. Mandatory case gates
require an external observer such as `process-observations-v1`; in-process
pytest is auxiliary. A passing self-authored test does not prove goal coverage.

```sh
python3 <skill>/scripts/harness_client.py checkpoint \
  --binding /absolute/binding.json --action-id implementation-1 --wait-seconds 30
```

The action ID is an intent label, not a hand-built request UUID. The helper saves
the exact request, submits, verifies and waits. Default `brief` output keeps
scope, gates, exceptions and detail references; use `--view attention` or
`--view summary` when more data is needed. It never assesses, repairs or finishes.

`ok` means the call was handled. Inspect `selected_job`, `gates`, gaps and
omission counts. `pending` means the Job is still running: use `wait` on that
Job, not another checkpoint. After response loss, replay `caller_request.request_file`
with `call --request`; do not reread a changed payload or invent a new action ID.
`checkpoint_progress` describes acknowledged stages; null is not proof that a
side effect did not happen. A new intentional submission needs a new action label.

When a consequential decision/exception is worth retaining, optionally add
`--payload FILE` containing `fields`, `extensions`, and optional `parents`.
Omission creates no note. Extra/null/empty values are preserved in this task's DB
as untrusted caller data, never policy or measured success. Do not include secrets.
Record delivery and exact field reuse are described in [the protocol](references/protocol.md).

## Resume and close explicitly

After context loss, confirm goal/workspace/Domain and use the existing binding
with `context --complete` without a cursor. Later use deltas or compact reads.
For discovery, continuations, or shared advice, see
[task storage](references/task-storage.md); for details, baseline comparisons,
scope revisions or recovery, read the relevant [protocol section](references/protocol.md).

Before declaring completion, compare current submitted files with the measured
Candidate, inspect mandatory gates and report gaps. Measurements concern that
snapshot, not later edits. Decide whether to fix, continue, finish partially or
finish completed; an assessment is optional and remains a model claim.
Use `call --operation finish` with an explicit outcome. Budget/runtime/policy
limits cannot be bypassed by silently creating a replacement Run.

A selected reusable lesson may be published with optional `--knowledge` on
finish. It creates advisory MD outside the task DB, not a transcript or a new
task's evidence. Publication failure is separate from committed closeout.
Report the deliverable, measured results/skips, uncertainty and Run ID.
All results remain unsigned `local-advisory`, `ready=false`.
