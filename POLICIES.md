# Configured acceptance and measured scope

Harness does not decide whether a test adequately expresses a natural-language
goal. A Domain compiles requirements and checks; Core pins their identity and
enforces the selected completion policy. Host reasoning remains autonomous.

## Operator configuration versus task proposals

Configure an acceptance registry outside the agent's editable project:

```text
policies/
├── registry.json                 optional Domain defaults
└── addition/
    ├── policy.json               versioned Domain parameters and approval record
    └── bundle/
        └── acceptance_tests/test_addition.py
```

`registry.json` is `{"defaults":{"develop":"addition"}}`. A configured Domain
default cannot be replaced by `start --policy-id weaker`. Without a default, the
caller can select an explicitly registered policy; the selection is recorded as
caller-selected, not as operator approval of its applicability to this task.

See the complete [example policy](examples/acceptance/policies/addition/policy.json).
It contains:

- `policy_id`, `revision`, `domain_id`, `schema_version: acceptance-policy-v1`;
- Domain `parameters`, including explicit named checks;
- `required_check_ids` and `requirements` (`id`, `statement`, `check_ids`, optional
  `minimum_evidence: file|command|testcase`); dangling/duplicate mappings are errors;
- optional Domain-owned `coverage` inventory with a profile revision, scenario-to-
  requirement/check references into the pinned test bundle, and explicit known gaps;
- `bundle.version` and explicit `bundle.files`, relative to the bundle directory;
- `approval.declared_by` and `approval.reference`, operator configuration records.

Optional `authorship: {"criteria":"model","tests":"user"}` distinguishes the
declared criteria/test authors from the approver and Domain generator. Each value
is `user`, `model`, `application` or `unknown`; omission means unknown, not that
an application authored the tests. These remain configuration claims.

Develop requires a declared bundle for mandatory command checks. Bundle files are
inputs, not implementation artifacts. Each mandatory check must map to a declared
requirement. An unmapped requirement remains explicitly uncovered. A requirement
declared to need commands cannot be mapped to file-only checks. These are format
and consistency checks, not an LLM judgment that the criteria are sufficient.

The optional `coverage` inventory gives a Domain a typed place to describe which
pinned test cases are intended to exercise which requirements and checks. Scenario
`kind` values are `positive`, `negative`, `boundary`, `state_transition`,
`concurrency`, `recovery` and `fault_injection`. Each `test_ref.path` must be in the
pinned bundle; `case_id` is an author-declared logical test name. Known gaps require
a requirement and an explanation. Resume/status exposes scenario counts, known gaps
and requirements with no inventory entry. Coverage entries are declarations only:
the Domain does not parse test code or claim per-case execution. The generic verifier
still measures the configured command as a whole and reports no test-case count.
All existing required check IDs, gates, baselines and strict Sandbox behavior remain
unchanged by this inventory.

## Pytest case observations

Develop can opt into `parameters.pytest_checks` alongside ordinary `test_commands`:

```json
{"id":"behavior", "paths":["acceptance_tests/test_queue.py"],
 "args":[], "required_cases":["acceptance_tests/test_queue.py::test_replay_has_one_event"],
 "allowed_outcomes":["passed"], "timeout_seconds":30}
```

Collection paths are explicit input files. Supported selection options are `-k`,
`-m`, `-x`, `--maxfail`, `-q`, `-v`, `--strict-markers` and `--collect-only`.
Collection and execution are observed in the same session. Collection-only
results never satisfy the default execution condition. Project `conftest.py`
and configuration files must be included in the declared input scope when needed.

Use `coverage.schema_version: develop-coverage-v2` for exact pytest bindings.
Each scenario has `test_ref: {framework: "pytest", path: "...", case_id: "...::..."}`
and may set `required: true`. Parameter IDs, including brackets and Unicode, are
preserved. The declared file and node-ID prefix must agree. Required scenarios
are compiled into immutable Check rules; ordinary v1 inventory remains advisory.
Case-aware acceptance test files must belong to the pinned operator bundle.
See the [pytest queue example](examples/acceptance/policies/pytest-queue/policy.json).

Each case records discovery, selection, protocol start, call-phase execution,
completion, phase outcomes and duration. `missing` is a derived mapping result
only after complete collection. A collection error yields `unconfirmed`;
selection filters yield `not_selected`; an early stop can leave `not_run` or
`incomplete`. Setup skips have `executed: false`. Teardown failures override a
passing body. `xfail` and `xpass` remain distinct and are rejected by default.
Optional allowed outcomes do not remove the explicit required-case execution
condition. Empty selection is not a vacuous success.

Case observations are checkpointed separately from completed Checks. Their IDs
bind Run, Job/attempt, Candidate or baseline, Check revision, bundle, adapter and
runtime identities. They are available through `records --kind cases --job-id ID`,
lossless context pages, and assessment citations. Old Candidate citations are
contextual and do not satisfy current gates. Case IDs/paths are retained exactly
for binding; use non-secret parameter IDs. Diagnostic messages are redacted.
`runtime_environment_hash` identifies the pinned runtime known when the case is
checkpointed. `execution_observation_id` links to its Check receipt, which carries
the final Sandbox provenance and complete recorded environment. That parent can
remain unrecorded after interruption; a completed case alone does not complete a Check.

Job `result.requirement_observations` links requirements/scenarios to observations
and reports `declared_scenarios_observed`, `linked_cases_passed`,
`linked_checks_passed`, missing/unconfirmed cases and known gaps. It includes its
execution scope. One case can support several links without counting as several
executions. A passing linked suite still does not establish goal completeness.
`measurement_scope.test_case_count` counts observed call-phase executions in
case-aware checks; `test_case_count_complete` and `test_case_scope` describe its
limits. Generic command checks keep unknown case counts. Baseline case runs are
not included in the Candidate's count.

`parameters.validation_profile` may explicitly select `stateful-cli`,
`concurrent-queue` or `pure-function`. These profiles supply advisory verification
perspectives. The Host selects a suitable profile; no natural-language keyword
matcher, automatic test generation or extra mandatory test is introduced.

## Optional pytest runtime

The Core remains standard-library-only. Install the optional adapter dependencies
in the interpreter selected by the Harness launcher, for example:

```sh
.venv/bin/python -m pip install -e '.[pytest-adapter]'
.venv/bin/python scripts/prepare-pytest-runtime.py --state-dir /absolute/state
```

The first case-aware verification can prepare this snapshot automatically.
Preparation only reads installed packages; it does not access the network or
import Candidate code. Prewarming avoids package preparation in a task's time
budget. Runtime package bytes and the reporter are hashed, cached outside the
workspace and copied into a per-execution read-only mount. A Run keeps its selected
runtime even when Host packages change. `--refresh` prepares a new snapshot for
future Runs without rewriting existing observations. Missing optional packages
produce an unavailable adapter, never an unsandboxed fallback.

This adapter supports the namespace backend on Linux/WSL. Windows-native adapter
execution is not implemented. Global third-party pytest plugin autoload is
disabled; additional project dependencies are not automatically installed. The
reporter and tests share a Python process: these are locally reported observations,
not tamper-resistant certification or proof of test validity. The record retains
the distinction from caller-submitted claims.

Start through the existing CLI:

```sh
bash scripts/harness-tool --state-dir /absolute/state \
  --policy-root /absolute/policies start --domain develop \
  --goal 'Implement integer addition' --workspace /absolute/project \
  --mode acceptance --capture-baseline --request-id addition-start-1
```

The caller helper instead accepts `policy_root` in its installation config, or
`configure --policy-root /absolute/policies` when creating a new config. A task
request cannot replace that configuration. No automatic criteria generation or
operator-policy installation is performed by the Skill or helper.

`parameters` on an acceptance Run are additional exploratory proposals. Mandatory
definitions and input scope are compiled from the pinned policy and cannot be
removed, revised or downgraded to structural-only checks. Exploratory checks may
fail, evolve or be retired without erasing their measurement history or becoming
mandatory. Existing `strict` and `exploratory` caller-defined policies remain
available without configured acceptance, explicitly labeled `caller_defined`.

## What is pinned

Start captures test code and fixtures, executable modes, the policy definition,
bundle version, Domain identity and verifier identity. Submit overlays those
captured files onto a new Candidate; identically named workspace files are never
used as acceptance tests. Neither submit nor verify modifies the workspace.
An ordinary later edit to the registry cannot change an existing Run's bundle.

`status --check-workspace` compares only submitted files sourced from the workspace;
`workspace_comparison_excludes_pinned_bundle` lists operator-sourced paths. Run,
Candidate, check-set and bundle hashes bind each measured fact. Local corruption
is detected by consistency checks, but an attacker with the same OS permissions
can replace code and recompute hashes. Directory separation and these hashes are
not an OS security boundary or a signature.

A policy change requires updating operator configuration and explicitly starting
a new Run with `--predecessor-run-id` and `--policy-change-reason`. The predecessor
must be terminal and identify the same goal, workspace, Domain and constraints.
Its history and usage remain recorded. Restarting a Host is not a policy change;
use resume. Approval status is **configured_not_authenticated**, never authenticated
human identity. The current product does not have a protected approval service.

## Baseline and result meaning

`start --capture-baseline` snapshots declared inputs before edits. Missing files
are reported; Harness does not synthesize an original from an edited checkout.
`verify --compare-baseline` measures that original and the current Candidate in
one owned Job with identical check definitions. Both sets of facts are durable;
only current Candidate observations can satisfy its gates.

Comparison categories are `improved` (fail→pass), `already_passing`, `regressed`,
`still_failing`, and `inconclusive`. A command comparison needs a configured pinned
test bundle and the same input path scope. Errors, unavailable execution, different
recorded environments or missing sides remain inconclusive. This is evidence of
change against a check, not proof of overall goal quality or regression freedom.
Runtime identity is recorded; arbitrary transitive dependencies, clocks, random
seeds and test independence are not automatically controlled.

`measurement_scope` distinguishes file checks, completed commands, unavailable,
not-run, errored and timed-out execution. The generic command verifier measures
exit status and streams; it does **not** invent test-case counts from successful
exit status. `test_case_count: null` explicitly says that case coverage was not
collected. Detailed stdout/stderr remains in paginated measurement records.

`finish completed` is still a requested disposition. Read measurement scope,
mandatory gate results, caller assessment, mapped/unmapped requirements and
uncertainties separately. All results remain unsigned **local-advisory**,
`ready=false`, with no protected certification.
