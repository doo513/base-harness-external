# Paired workflow evaluation

This opt-in evaluation launches existing agent CLIs over three fixed, benign
multi-file Python tasks. It is outside the model-free Harness runtime; nothing
in `src/harness_external` imports or calls the study runner.

The task set covers settings migration/atomic output, durable journal recovery
and concurrency, and dependency planning with preserved input/CLI behavior.
Each task runs in ordinary, previous-Harness, and updated-Harness conditions on
Codex (`gpt-6-luna`, `medium`) and AGY (`gemini-3.8-flash-high`): 18 initial trials
and six fresh-Host recovery sessions. Only these two requested models are included
in the completed comparison. Earlier Astra experiments are retained but excluded.
These are requested model identifiers, not attestations of served weights.

## Fixed methodology

- Each trial starts with identical task files, requirements and supplied partial
  acceptance tests. Order is rotated across tasks and reversed for the second Host.
  There is at most one active trial per Host. This is one repetition per cell.
- The previous runtime is copied from `8dad67a`; the updated runtime is copied
  from the current working tree. Exact runtime, policy, evaluator and task hashes
  are saved before scored model execution. The original implementation checkout
  and hidden expected answers are not visible in the evaluation mount scope.
- Both Harness arms receive the same mandatory checks. The previous caller must
  pass the supplied parameters and gate IDs; the updated caller uses an operator
  default and pinned test bundle. Updated trials also request baseline comparison.
  This evaluates the whole workflow revision, not one isolated optimization.
  Supplied scope and actually executed scope are audited separately: one previous
  Luna settings Run changed the two supplied substring predicates into more specific
  function-signature predicates. Its behavioral command remained unchanged. This
  is a protocol/scope deviation, not an observed weak-check bypass; it also limits
  strict apples-to-apples interpretation of that cell.
- The initial budget is 600 seconds, enforced by the external evaluator. Native
  AGY print mode has no imposed CLI timeout. A failed artifact gets at most one
  fresh-Host repair session with failed-case names and a 300-second budget.
  Journal trials always have a fresh-Host recovery/inspection session, even after
  an initial pass. No human solving assistance or silent retries are allowed.
- Each stage's source files, stdout/stderr, native usage counters, claim, Harness
  records and grading result are retained before another stage is started. A
  completion claim, artifact correctness, required gates and command execution
  are different observations. Historical command passes are not current proof.
- Fixed expected answers stay in the parent evaluator. A bounded subject driver
  executes candidate behavior in the actual strict Sandbox and returns observed
  values/exceptions. The parent compares them with the fixed expected answers.
  Initial stubs and correct positive controls must both be measured before models
  run; a failed positive control blocks scoring rather than penalizing a model.

## Costs and interpretation

Report first-stage quality/time separately from final quality and total stage
time. Include repair/recovery sessions, raw tool-call and token counters, observed
human interventions, automatic setup and grading time. Task/test authoring,
human review time and provider monetary/subscription charges are not measured.
Do not manufacture them or claim complete financial savings from token totals.
AGY cache counters and Codex cached-input counters have different definitions.

The controls and acceptance policy are authored by the same internal evaluator,
not a third party. A fixed bundle does not establish semantic coverage or test
independence. The limited task count cannot prove general model superiority or
universal Harness savings. Native Windows and automatic Skill discovery are not
tested. Results remain unsigned local-advisory, not protected certification.

## Completed internal pilot — 2026-10-02

The machine-readable report is [luna-agy-comparison.json](luna-agy-comparison.json).
All 18 initial artifacts passed their fixed holdout cases (390/390 observations);
all six subsequent recovery artifacts also passed (120/120). No scored invocation
timed out, requested an artifact repair, or claimed completion over a failed
holdout result. These are observed outcomes on three tasks, not a coverage proof.

| Model | Workflow | Initial time, 3 tasks | Total including recovery | Tool calls | Reported input tokens |
| --- | --- | ---: | ---: | ---: | ---: |
| Luna medium | Ordinary | 374.8 s | 415.5 s | 36 | 743,804 |
| Luna medium | Previous Harness | 932.9 s | 1,019.3 s | 76 | 1,950,729 |
| Luna medium | Updated Harness | 821.7 s | 860.6 s | 71 | 1,743,804 |
| AGY Gemini high | Ordinary | 993.3 s | 1,099.9 s | 103 | 1,020,170 |
| AGY Gemini high | Previous Harness | 1,192.2 s | 1,404.9 s | 158 | 1,781,378 |
| AGY Gemini high | Updated Harness | 1,118.4 s | 1,287.3 s | 160 | 2,033,673 |

- Updated versus previous total session time: Luna **−15.6%**, AGY **−8.4%**.
  Updated versus ordinary: Luna **+107.1%**, AGY **+17.0%**. There is no demonstrated
  net cost saving or quality advantage over ordinary work in this small pilot.
- Within-provider reported input tokens decreased 10.6% for Luna but increased
  14.2% for AGY versus previous Harness. Cached/thinking/output counters are retained
  separately in JSON; these columns are not comparable money or interchangeable
  token measures across providers. Every scored session supplied usage counters.
- All six updated Runs preserved the configured mandatory scope, pinned the bundle,
  executed the required command, measured baseline failure → Candidate success, and
  closed with workspace equality. All four Harness recovery sessions reused their
  existing closed Run IDs; none of the six recoveries changed implementation bytes.
- Exact supplied scope was retained by five of six previous Runs; the exception is
  described above. A Luna summary also called four unittest cases "four mandatory
  checks". The actual Harness scope was two file predicates plus one command;
  generic command measurement did not collect a test-case count. Model prose is
  not substituted for these structured facts.
- External grading consumed about 3.6–3.8 seconds per four-session group, recorded
  separately from model session time. Automatic setup: 13.9 seconds for the Luna
  continuation; 21.8 seconds for the original mixed-Host setup. Authoring, review,
  subscription charges and discarded diagnostic experiments are not included in
  those solve-time totals. They must not be portrayed as zero total project cost.

### Retained amendments and execution evidence

Local raw receipts remain in the sibling directory
`harness-acceptance-evaluation-20261001/`: `luna-final` for Luna and
`comparison-final` for AGY. Its `comparison-v2` directory preserves excluded
termination-affected trials and the three normally completed AGY settings trials
that were explicitly reused after source/policy/artifact/exit checks. Reused trials
have normal-exit/reuse evidence, not a retroactively fabricated namespace footer.

Before scored runs, a grader preflight was corrected to accept JSONDecodeError as a
ValueError subtype. Later diagnostic runs exposed outer-process termination that
did not prove descendant termination; owned namespace/process-start identities and
an exit footer now enforce that boundary. Two wall-clock-guard-affected trials and
quota-rejected Astra trials are excluded, never relabeled as scored Luna results.
No scored task requirements, expected answers, or budgets were weakened.

The final report rechecks frozen runtime/definition files, receipt consistency,
the exact 18-condition matrix, artifact file sets, and available exit-trace hashes.
The executed evaluator scripts are archived at
`../harness-acceptance-evaluation-20261001/executed-study-scripts-20261002.tar.gz`
(SHA-256 `04948528f9b934dac5a6dc76bc0da3765127e9452c1ca9ef24e7d6e6b5e7ad3c`).
Future runner defaults were changed to Luna/AGY only after all controllers exited;
pre-execution provider rejection now stops either Host without grading untouched
stubs or attempting artifact repairs. Existing frozen plans are not silently
rewritten to match that newer runner.

The study's frozen updated runtime predates the additional Core guard rejecting
malformed custom-Domain mandatory-ID output. Normal Develop behavior is unchanged;
that guard is covered by current-code regressions, not claimed as model-tested in
the frozen snapshot. Source digests keep those two kinds of evidence separate.

## Reproduction

Linux/WSL user/mount/PID namespaces, Python, Bun, and already-authenticated local
CLIs are required. No login or credential copying is performed by the runner.
Use a new, empty directory disjoint from the implementation checkout:

```sh
python3 scripts/agent_study.py prepare --root /absolute/new-study
python3 scripts/agent_study.py controls --root /absolute/new-study
python3 scripts/agent_study.py qualify --root /absolute/new-study
python3 scripts/agent_study.py run --root /absolute/new-study --host codex
python3 scripts/agent_study.py run --root /absolute/new-study --host agy
python3 scripts/agent_study.py summarize --root /absolute/new-study
```

The two `run` invocations may run concurrently. `prepare` refuses to overwrite an
existing study. `run` refuses an unresolved prior invocation instead of silently
launching it again. Check its recorded process handle and stage receipts before
manual recovery. An observation/tool timeout does not mean the process stopped.

Codex uses its foreground unrestricted execution profile inside the separate
evaluation scope so nested Harness namespace verification remains available;
this is not advice to disable isolation in ordinary user tasks. AGY's initially
restricted terminal may need its normal Host execution-permission route. That
must still run the strict Harness Sandbox and retain the scope's read restrictions.
The actual study records additional native-CLI probes confirming these properties.

Execution guidance was checked against the official OpenAI documentation for
[non-interactive JSONL operation](https://learn.chatgpt.com/docs/non-interactive-mode)
and [behavior-based Skill evaluation](https://developers.openai.com/blog/eval-skills),
then the installed CLI help. Final claims alone are never the grader.
