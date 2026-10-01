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

Read [the caller protocol](references/protocol.md) before the first call; it includes
the helper request envelope, precise fields and recovery examples. Run the bundled
`scripts/harness_client.py` with the persistent config selected during installation.
If configuration/runtime is absent, report the missing setup. Do not guess another
checkout, create a temporary production state store, or silently bypass Harness.

- Preserve the user's original request and constraints. Prepare checks only obvious
  missing information, contradictions and scope misunderstandings; interpretation
  is revisable, not the definitive solution. Ask only when ambiguity changes scope,
  authority or the intended result.
- For a resumed task, find and inspect the existing Run's goal, workspace, Domain,
  lifecycle and active job. Explicitly use that Run ID. Never select a Run merely
  because it is newest, or hide missing state by starting another one.
- For a new task, select a supported Domain and state the initial policy/provenance.
  Use any explicitly supplied operational policy and user requirements; otherwise
  record the proposed policy as model-authored. Do not
  label model choices as authenticated user approval. Do not weaken a pinned gate.
  Domain modules own contract/check semantics; propose criteria through their API.
- Reuse a request ID only to retransmit the exact same operation and payload. A new
  intentional action needs a new ID. Save request IDs/payloads in Host task context;
  do not invent a second authoritative Run database.

## Explore, execute and measure

Plan, decompose, delegate when appropriate and try alternatives using the Host's
existing capabilities and permissions. Do not add a model loop to Harness or make
the helper decide the solution. Skill instructions do not expand authorization.

Revise the working interpretation/check proposals when observations warrant it.
Declare the complete input scope needed by the chosen checks, including tests and
fixtures. Use task/Domain reasoning to consider the intended runtime environment:
a Linux measurement is not evidence of Windows behavior. Missing execution support
is an explicit limitation, not permission to bypass the strict Sandbox.

At meaningful checkpoints, submit the current files and verify the submitted
Candidate. Poll the returned job with bounded `status`/`records` reads; a queued job
is not a pass. A Host turn ending does not mean a job should be reissued or cancelled.
After a change, submit again and obtain measurements for that new Candidate.

Read actual observations, their scope and limitations. Verifier facts do not order
a retry: you decide whether to revise, try another method, stop partially, or ask
the user. Keep caller observations and direct Host tests distinct from Harness
measurements. Do not convert a test skip or unavailable runtime into a pass.

## Close out or hand off

Before reporting success, compare current submitted files with the verified
Candidate, inspect gates and unresolved work, and assess the observations that
actually concern the current Candidate/check set. A past measurement can inform
reasoning but is not current proof. Report scope changes rather than rewriting the
original goal. Do not erase failed attempts or reset budgets by quietly creating Runs.

Use `finish` with the disposition supported by the facts and current policy.
On a budget/deadline boundary, explain the remaining work and preserve the Run for
inspection; do not continue by silently resetting the policy. On interruption,
record a concise decision/next step when possible and use `resume` next time.

The final user report needs the deliverable, actual measurements and skips, remaining
uncertainty and the Run ID (or a linked report containing it). Distinguish measured
results, model assessment and lifecycle closure. All current results are unsigned
`local-advisory`, `ready=false`, not protected certification. Skill adherence is not
a security boundary, and a separate directory does not defeat same-user tampering.
