# Task storage and reusable notes

Use this reference for lost task bindings, explicit continuations, or publishing
and retrieving reusable knowledge. Normal new starts need no extra setup call.

## One task, one store

A new helper-built `start` adds an opaque `task_id` derived from its stable start
request ID, absolute workspace and Domain. The saved request pins that selector.
The application creates `tasks/<task_id>/runs.sqlite3` and its snapshots under
the configured durable `state_dir`, outside the workspace. This is the task's
only Run database; task.json stores connection metadata, not lifecycle state.

The DB retains delivered original goal/spec text separately from interpretations,
caller notes (including opaque fields/extensions), checks and measured facts.
JSON value preservation is not preservation of envelope whitespace or automatic
capture of every Host message. Submitted file bytes live in snapshots linked by
identity; compact responses are read projections, not replacements for records.
Optional checkpoint payloads use these same note records and request replay keys.
No routine note is required and no payload means no note. Exploration cache files
are disposable derived syntax data, outside the task DB and verification authority.

Same action + identical request replays the same task and Run. Changed content
for that action conflicts. An unrelated new request needs a new start action,
even in the same workspace. Do not manufacture a new task to evade an existing
policy, limit, failure or unknown response.

Bindings from a new start use `harness-caller-binding-v2` and include task ID.
Use that same binding for submit/checkpoint/wait/context/finish and fresh-session
recovery. No configuration or database path edits are required.

Existing v1 bindings and saved envelopes without task_id continue to address the
legacy root DB. Replaying them does not migrate records or create a task DB.
Raw Core CLI/API calls without task_id retain legacy behavior. The CLI also
accepts a global `--task-id` selector; prefer the helper to construct new starts.

## Discover an explicitly selected task

Use `call --operation list-tasks --arguments
'json:{"workspace":"/absolute/project","domain":"develop","limit":20}'`.
Follow next_offset if needed; this lists only connection metadata. Choose the
intended task, then use `find --task-id TASK_ID --workspace ... --domain ...` to
inspect its Runs. Do not choose the latest or only match without checking intent.
Bind with `bind --task-id TASK_ID --run-id RUN_ID --workspace ... --domain ...
--output BINDING_FILE`.

A missing/corrupt task DB is an error, not permission to initialize another.
Explicit runtime/policy continuations may start through the predecessor binding:
include predecessor_run_id and the existing continuation/policy-change reason
and original task parameters. The selected task DB is retained; existing Core
terminal-predecessor and policy checks still apply. The new Run does not inherit
passing evidence. Omit the old binding only for an intentionally different task.

## Select a reusable note, not a transcript

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
