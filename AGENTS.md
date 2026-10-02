# Base Harness External

- This repository is an external verification tool, not an agent runtime.
- Preserve the start/observe/submit/verify/status/finish operations. API v2 defaults CLI status to summary; explicit full views and historical v1 records remain available.
- Optional revise/check/assess operations preserve interpretation/check history and caller assessment. Original intent, constraints and initial completion policy stay pinned within a Run.
- Keep Measurement, Assessment, Gate results and Completion independent. Strict mode is an explicit compatibility policy; exploratory checks do not automatically become completion gates.
- Core owns references, provenance and lifecycle. Domain modules own preparation and check normalization; do not add Develop success criteria to Core.
- Persist each completed measurement before the next check. Partial checkpoints survive worker failure without becoming final success for another subject/revision.
- Policy provenance is initial configuration, not authenticated human identity. Logical task roles/states and assessments remain caller claims.
- Model notes and test expectations are untrusted inputs. Do not present them as independent evidence.
- All results are local-advisory, unsigned, and ready=false. Do not add Ready claims without a protected verifier and independent consumer.
- Keep Run state outside and independent of the caller's workspace/session.
- Do not edit or publish workspace files; verify pinned snapshots.
- Test commands may execute only through the existing strict Sandbox adapter. Never add an unsandboxed fallback.
- Preserve request idempotency, deadlines, budgets, ownership and late-result rejection.
- Revalidate active checks through the Domain port when interpretation scope changes. Retired checks retain history; reactivation must advance their revision.
- Required gates cannot be retired. Late worker startup failures cannot rewrite terminal Runs or replace a newer Job owner.
- Prefer resume and paginated records for agent-facing reads. Job results store observation references; new measurements have one canonical body.
- Domain check keys are identity, not list positions. Keep approval claims unverified without an authenticated approval mechanism.
- Pin Domain implementation/configuration identity and the executed check-set hash. Pure queries use read-only transactions.
- Agent-facing summaries and closed Run recovery validate Job observation references. Assessments classify current versus contextual citations.
- Persist and replay terminal limit errors for the same request ID; retrying a state-changing request must not change its outcome.
- Capture recovery must acquire an OS lock and retain data in quarantine; never remove workspaces or published Candidates.
- The Python package has no runtime dependencies. Bun is currently required only for sandboxed command checks from the source checkout.
- Do not restore the old Host, UI, provider gateway or legacy Ready engine.
- Configured acceptance policies are a separate operator input channel. Domain
  compiles their criteria; Core pins policy and test/fixture content. Never turn
  a caller proposal into configured approval or infer authorship from its loader.
- Acceptance approval is configured, not authenticated. Same-user file access is
  still outside the protection of these local-advisory records.
- Baseline observations cannot satisfy current Candidate gates. Pair outcomes
  distinguish check improvement from goal proof and retain explicit incomparable
  reasons. Command exit is not an independently measured test-case count.
- Caller bindings and transport locks are references/mechanics, not another Run
  database. Checkpoint must not assess, repair, finish or silently retry a mutation.
- Run Python tests and an opt-in real Sandbox test when changing execution boundaries.
