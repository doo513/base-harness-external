# Task storage and reusable notes

Use this reference for lost task bindings, explicit continuations, or publishing
and retrieving reusable knowledge. Normal new starts need no extra setup call.

## One task, one store

A new helper-built `start` adds an opaque `task_id` derived from its stable start
request ID, absolute workspace and Domain. The saved request pins that selector.
New configurations use `task_storage: project`. The application creates
`<project>/.harness/tasks/<task_id>/runs.sqlite3` and its snapshots inside the project.
This is the task's only Run database; task.json stores connection metadata, not
lifecycle state. Shared `state_dir` retains selected knowledge and operational
locks, not a copy or an index of all project databases. Project discovery is explicit.

`.harness/` is reserved control state: it is rejected as a contract input/artifact,
excluded by Change Lens and ignored by Git through its own `.gitignore`. Do not
edit its DB or snapshots with source tools. This separation is logical and
same-user, not an OS-enforced protection from the Host model. Concurrent project
tasks keep separate DBs and still share the configured Worker capacity limit.

The DB retains delivered original goal/spec text separately from interpretations,
caller notes (including opaque fields/extensions), checks and measured facts.
JSON value preservation is not preservation of envelope whitespace or automatic
capture of every Host message. Submitted file bytes live in snapshots linked by
identity; compact responses are read projections, not replacements for records.
Optional checkpoint payloads use these same note records and request replay keys.
No routine note is required and no payload means no note. Exploration cache files
are disposable derived syntax data, outside the task DB and verification authority.

The project DB is also the authority for live execution state and all detailed
verification. The external Markdown ledger described below is a projection of
that DB, not a second state machine. Do not reconstruct state from edited MD.

Same action + identical request replays the same task and Run. Changed content
for that action conflicts. An unrelated new request needs a new start action,
even in the same workspace. Do not manufacture a new task to evade an existing
policy, limit, failure or unknown response.

Project-local bindings use `harness-caller-binding-v3`, with task ID and
`project_workspace`. The helper forwards this routing field; Domain parameters
never choose DB paths. All requests continue using Linux absolute project paths.
Use that same binding for submit/checkpoint/wait/context/finish and fresh-session
recovery. No configuration or database path edits are required.

Existing v1 bindings use the legacy root DB; v2 bindings and envelopes with only
task_id use the historical shared-root task directory. Replaying them never
migrates records. Configurations without task_storage retain their old layout;
`configure --task-storage external` makes that choice explicit. Raw Core calls
without project routing retain legacy behavior. Do not add a project field to a
saved external-store request and expect it to migrate or fall back.

## Discover an explicitly selected task

Use `call --operation list-tasks --arguments
'json:{"workspace":"/absolute/project","domain":"develop","limit":20}'`.
Follow next_offset if needed; this lists project-local and legacy matching metadata.
Without workspace, only the historical external catalog can be searched; Harness
does not scan the home directory for projects. Choose the intended task and use
`find --task-id TASK_ID --project-workspace PROJECT --workspace PROJECT --domain ...`
for a project-local result. Bind with the same `--project-workspace` and explicit
Run ID. Omit that flag for a legacy result. The saved binding handles subsequent
routing automatically. Never choose the newest or only match without checking intent.

A missing/corrupt task DB is an error, not permission to initialize another.
Explicit runtime/policy continuations may start through the predecessor binding:
include predecessor_run_id and the existing continuation/policy-change reason
and original task parameters. The selected task DB is retained; existing Core
terminal-predecessor and policy checks still apply. The new Run does not inherit
passing evidence. Omit the old binding only for an intentionally different task.

## Derived Run ledger

Finishing a project-local Run automatically generates
`state_dir/run-ledger/<run_id>.md` and `run-ledger/INDEX.md` from the committed
closed record. The document distinguishes measured scope/gates from caller
assessment, retains baseline-comparison uncertainty, and points to the project DB
and exact record/Candidate/contract hashes. Long text and lists are bounded with
explicit truncation/omission markers. Original requests, working notes and raw
measurement bodies remain in the DB. No shared SQLite copy is created.

Inspect `run_report.status` separately from the successful closeout. If publication
fails, use `call --operation report-publish --binding BINDING --action-id ID
--arguments json:{}`; do not restart or re-verify the Run to repair a document.
Publication is deterministic/replayable and does not hold a DB write transaction
while writing files. Existing documents that disagree with their stored receipt
are reported, not silently overwritten. MD cannot change a gate or lifecycle.

`report-list` is a global bounded read with optional workspace/domain/offset/limit.
`report-read` takes `entry_id` (the exact Run ID returned in the list), without a
Run binding. These are historical documents with `source_state_not_queried:true`,
not live status or protected certificates. Use the source DB's binding to inspect
current facts. Socket reads filter source workspaces. Legacy external Runs are
not automatically published; `report-publish` is an explicit optional operation.

## Selected reusable knowledge

The optional `--knowledge` finish input accepts JSON files, `json:{...}`, or stdin:

```json
{
  "title": "Preserve the previous result during a failed replacement",
  "when": "Replacing an existing output file",
  "lesson": "The source task used a temporary sibling and an atomic replacement.",
  "limitations": ["Not verified for power loss or another operating system."],
  "keywords": ["atomic", "replacement", "preservation"],
  "cited_observation_ids": ["EXACT_RESULT_ID_FROM_THIS_RUN"]
}
```

Use only genuinely useful task-specific lessons. Empty citations are allowed and
are labelled unverified. Source IDs must be measurements from the selected Run;
their existence does not prove the model's written lesson. Model advice,
measurement scope, terminal outcome, uncertainty and source identity remain
separate. Full logs, task files and entire discussions are not exported.

The shared library is `state_dir/knowledge/`, outside every task DB. Each entry
has an immutable content-derived ID, an MD document and bounded metadata.
INDEX.md is a rebuildable navigation view, not a verification authority.
Publication never issues Ready, changes a gate or rewrites a completed Run.

If publication fails after finish, Core's closeout remains committed. Use
`call --operation knowledge-publish --binding BINDING_FILE --action-id PUBLISH_ID
--arguments 'json:{"data":{...the selected note...}}'`.
Repeat an identical saved publication request after response loss; changed
content needs a new intentional action. Repeated identical notes do not create
duplicate entries. A corrupt published document is reported, not overwritten.

## Read only what the next task needs

Use `knowledge-find` with query, optional domain, offset and limit. It returns
titles, short descriptions, keywords, source outcomes and entry IDs, not full
documents. Read an exact entry with `knowledge-read` and entry_id. These
operations use `call --operation ...` without a Run binding or action ID.
An external socket filters source workspaces before returning document content.

Search is local literal-keyword matching, not embeddings or another model.
Different task lessons remain distinct; no automatic merging or latest-wins
replacement is performed. A historical note is neither a command nor approval.
Apply its method only where relevant and measure the current task independently.
