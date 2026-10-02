# Acceptance, evidence and caller-cost work

Scope: the complete implementation tree agreed on 2026-10-01. This is an external,
model-free Harness; the Host still reasons, edits, chooses another approach and
decides when to request closeout. No new agent runtime or second Run database.

## Completed — 2026-10-02

All **19 agreed requirements** are implemented and matched to passing tests or
retained execution evidence in [completion-audit.json](evaluation/completion-audit.json).
There are no remaining required items in this implementation tree. The dated
progress notes below are historical, not the current completion status.

- Final Python regression: **187 passed, 5 opt-in skips**. The separate live
  selection passed **6 tests**, including all five actual Sandbox/namespace tests
  skipped by default and one repeated-worker-delivery regression.
- Common conformance: **99/99**, with identical before/after source digest
  `313287685af2e781c2f43da5cb8c4074a5780ba3af8794a352fc2287801d8cb7`.
  Bun Sandbox tests: **3 passed, 3 Windows-only skips**. Skill format and diff
  validation passed. Windows-native command execution is not claimed.
- Luna medium and AGY Gemini high: **18/18 initial conditions**, plus six recovery
  sessions. All fixed observations passed. All six updated Harness Runs retained
  configured checks/bundles and measured baseline fail→pass; existing Run IDs
  survived the fresh-Host recovery sessions.
- Updated-versus-previous total session time fell **15.6% for Luna** and **8.4% for
  AGY**, but both remained slower than ordinary workflow. Input-token changes were
  mixed. There is no demonstrated net cost saving or quality advantage on these
  three tasks. See [methodology and results](evaluation/PAIRED_STUDY.md).
- Current-code custom-Domain reference guards were added after the model study
  freeze and verified separately. The evidence does not pretend the frozen models
  exercised that later guard. The production Harness remains model-free.
- JUnit files, mechanical validation logs, scoped patch and change manifest are
  retained under the sibling
  `harness-acceptance-evaluation-20261001/completion-20261002-dhkexe` directory.
  No commit or push was performed.

Remaining nonclaims are deliberate scope limits: no protected Ready certificate,
authenticated human approval, semantic completeness proof, automatic Skill
discovery, native Windows validation or statistically established cost benefit.

## Completion checklist

- [x] Domain: reusable acceptance profiles; requirement-to-check mappings; mandatory
  acceptance versus exploratory checks; explicit limited assurance for weak scope.
- [x] Core: separately configured policy input; authorship/approval/change authority;
  pinned acceptance code/fixtures/version; immutable mandatory scope; linked new
  Runs for policy changes; exploratory history preserved; no command-to-file downgrade.
- [x] Verification: accurate file/command/test scope and not-run/error/timeout facts;
  optional same-check/same-environment baseline comparison; positive/negative controls;
  lifecycle, measurement, assessment and uncertainty remain distinct.
- [x] Caller/Skill: reusable explicit Run binding; idempotent request serialization
  and retransmission; mechanical submit/verify/wait; compact evidence references;
  capability preflight; fewer protocol instructions without prescribing reasoning.
- [x] Evaluation: identical supplied policy across models; weak/stale/modified checks;
  total cost including rework/intervention; multi-file/regression/recovery tasks;
  matched before/after evaluation with retained failures and honest limitations.
- [x] Final audit: regression and live Sandbox tests; real caller/Skill usage;
  requirement-by-requirement evidence and remaining limitations recorded.

## Design decisions

- An operator-configured policy registry is separate from model check proposals.
  Selecting a registered policy is not permission to replace its contents.
  A configured approval is not authenticated human identity or OS tamper protection.
- Domain compiles policy requirements and checks. Core pins identities and enforces
  immutable references, source scopes, lifecycle and budgets, not goal semantics.
- Acceptance test files are captured from the configured bundle, not from editable
  workspace copies. Existing Runs continue with their captured bundle; policy edits
  apply only to explicitly started new Runs with predecessor/reason links.
- Baseline evidence means the same check changed from fail to pass under the recorded
  environment. It does not prove full goal coverage or independent authorship.
- Existing caller-defined policies remain usable but are explicitly identified as
  caller-defined, not silently promoted to independently approved acceptance.

## Chronological implementation notes

2026-10-01: inspected clean `8dad67a` checkout and current Core/Domain/Skill paths;
baseline regression suite: 142 passed, 2 skipped.

Implemented configured policy loading and defaults, Domain compilation/mappings,
pinned test bundles, acceptance/exploration roles, separate criteria/test authorship
and configured approval, linked replacement Runs, baseline pairs, runtime identity,
honest measurement scope, caller binding/request/checkpoint/wait/preflight helpers.
No model runtime, protected approval/certification service or new Run database.

Implementation-stage evidence:

- Full Python suite: 166 passed, 4 skipped before runtime-identity/request-fsync
  follow-up; the subsequent conformance run covered those changes.
- Common conformance: 82/82 scenarios, source unchanged during the run;
  `evaluation/acceptance-conformance.json` identifies its exact tested tree.
- Opt-in real Sandbox selection: 5 passed; Bun tests 3 passed, 3 Windows-only
  skipped; Skill structure validation passed.
- Separate same-Host Skill forward task: `run_aa9d7c306f724fe59514ba9fbc3cc3e9`,
  two Jobs / 12 retained measurements, configured acceptance command fail→pass,
  11 reported unittest cases passed in captured output. This is not an independent
  third-party audit or a comparative model benchmark. See
  `evaluation/skill-forward-acceptance.json`.
- The forward task exposed misleading `test_bundle_not_pinned` wording when the
  actual blocker was a changed input scope. The comparison now records distinct
  `incomparable_reasons`; it does not hide or relax the comparison preconditions.
- Follow-up fixes avoid attributing unknown policy authorship to the application,
  preserve caller proposal provenance separately, and cache executable digests
  while rechecking inode/ctime/mtime. After these fixes: relevant regression
  53 passed / 2 opt-in skips, real Sandbox positive/negative/timeout checks 2 passed.

## Original completion gates

1. Finish matched before/after/ordinary-workflow evaluation on harder multi-file,
   regression and recovery tasks across the previously exercised CLI/model pairs.
   Freeze common requirements, tests, model settings, source snapshots and time
   budgets before scored runs. Retain failures, raw cost counters and intervention
   records. Report setup and repair/review cost separately; do not infer savings
   from helper convenience or conformance tests.
2. Re-run final full/common/live verification against the final tested tree and
   produce the requirement-by-requirement completion audit. Historical reports
   are evidence for their recorded digest, not automatic proof of later changes.

Both gates were required before declaring completion; the final audit above now
records their results. No commits or pushes have been made for this work.

### Preliminary comparative evaluation (historical)

- Added opt-in `scripts/agent_study.py`, `study_scope.py`, a Sandbox-only subject
  driver and three multi-file task definitions/positive controls. The production
  package does not import or invoke this evaluator. Fixed holdout coverage is
  23 settings + 20 journal + 22 planner observations.
- Grader preflight found and corrected exact-exception-name handling:
  JSONDecodeError is a valid ValueError subtype. The failed preflight is retained;
  requirements and expected values did not change. Correct controls pass 65/65;
  unfinished sources pass 2/65. Both real CLIs qualified inside the evaluation scope.
- Current full suite: 176 passed, 4 skipped; common conformance 82/82 with unchanged
  source during execution (`evaluation/acceptance-final-conformance.json`). Later
  evaluator-only termination fixes are additionally covered by 8 study tests,
  including actual namespace/process-group teardown.
- Initial model trials exposed an evaluator termination error: reaping the outer
  wrapper did not prove descendant termination; some timeout logs and closeouts
  arrived later. This is not a Harness state rollback. The supervisor now records
  owned namespace init identities, terminates them, and emits an exit confirmation.
  Monotonic deadlines are recorded; wall-clock guards are not used for scored runs.
- All initial records remain under
  `external-local/harness-acceptance-evaluation-20261001/comparison-v2`.
  They are diagnostic/excluded except the three normally completed AGY settings
  conditions, whose source/policy/input identity, final claims, usage, artifact
  hashes and absence of remaining log holders were checked before explicit reuse.
  Two temporary guard-affected stages ended before their original monotonic budgets
  and are explicitly excluded, not presented as fair performance measurements.
- The final 18-condition set is running at sibling `comparison-final`: 3 valid
  reused conditions + 15 new initial invocations. Frozen source runtimes, task
  requirements, tests and model choices remain identical. Journal also includes
  a fresh-Host recovery session; a failed artifact has at most one bounded repair.
  Do not relaunch a stage merely because its caller observation timed out.

At this stage final trial collection/cost and policy consistency analysis, the
methodology/amendment report and completion audit were still pending. Intermediate
green tests alone were not treated as completion.

### 2026-10-02 model restriction and final boundary audit

- User restricted subsequent model tests to **Luna and AGY only**. No new Astra
  calls are authorized. Its old results are retained separately and excluded from
  the current comparison. Astra had already stopped before this instruction;
  its later pre-execution failures were usage-limit rejections, not evidence that
  unchanged task stubs were produced by a working model.
- Confirmed `gpt-6-luna` can be invoked through the existing CLI; selected its
  default `medium` effort. `scripts/luna_study.py` runs only that model and uses
  exactly the frozen before/after runtimes, policies and tasks from the AGY study.
  Luna receipts: sibling `luna-final`; AGY receipts: `comparison-final`. Both
  controllers were still running at that point. The report generator rejects
  non-Luna Codex data.
- The Luna wrapper stops on a provider rejection before task execution instead of
  scoring unchanged stubs or sending further repair attempts. Do not automatically
  retry a missing/incomplete stage. A CLI usage limit is not a Harness defect.
- Final code audit reproduced a real extension boundary gap: a custom Domain
  compiler could return fewer mandatory check IDs than its configured policy.
  Core now checks those references at start, revision and persisted Run validation.
  It does not decide check semantics. Regression tests cover initial omission and
  revision drift. Normal Develop behavior in the frozen model studies is unchanged;
  this post-freeze defensive guard is verified separately, not retroactively claimed
  as part of their frozen runtime hash.
- Latest complete regression run: 180 passed, 5 opt-in skips. Subsequent reporter/
  Luna-wrapper tests: 4 passed; actual Sandbox acceptance/timeout tests: 2 passed.
- These remaining gates have now been closed: 9 Luna and 9 AGY conditions collected,
  limitations/cost report published, future runner defaults changed only after
  frozen controllers stopped, and the requirement-by-requirement audit passed.
  `evaluation/luna-agy-comparison.json` now records all 18 conditions as observed.
