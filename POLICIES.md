# Configured acceptance and measured scope

Harness does not decide whether a test adequately expresses a natural-language
goal. A Domain compiles requirements and checks; Core pins their identity and
enforces the selected completion policy. Host reasoning remains autonomous.

## Requirement conditions

Develop accepts optional `parameters.requirements` for caller-defined Runs.
It compiles the same condition shape as configured policy `requirements`:

- `id`, `statement`, `check_ids` are required; IDs are unique and stable.
- `kind` is `input`, `output`, `behavior` (default), `preservation`, `conflict`
  or `failure`. This classifies the declaration; it never chooses a gate.
- Optional `when` describes the condition under which the statement applies.
- `minimum_evidence` is `file` (default), `command` or `testcase`.
  Nonempty mappings must include an appropriate Check; testcase mappings need
  a case-aware adapter. Tests still have to substantiate the declared behavior.

For example, this complete caller parameter object links a preservation
condition to a command and explicitly leaves a failure condition unlinked:

```json
{
  "inputs": ["migrator.py", "tests/test_migrator.py"],
  "artifacts": ["migrator.py"],
  "expectations": [{"id":"entry", "path":"migrator.py", "operator":"contains", "expected":"def migrate"}],
  "execution_checks": [{"kind":"command", "id":"suite", "argv":["python3","-m","unittest","discover","-s","tests","-v"]}],
  "requirements": [
    {"id":"R-preserve", "kind":"preservation", "when":"When migrating an existing configuration",
     "statement":"Retain unknown fields and their values", "check_ids":["domain.suite"], "minimum_evidence":"command"},
    {"id":"R-failure", "kind":"failure", "when":"When writing the result fails",
     "statement":"Preserve the original and remove the temporary file", "check_ids":[], "minimum_evidence":"testcase"}
  ]
}
```

References target checks declared with explicit `id` values in the same Domain
parameters. An undefined ID is rejected once preparation is complete. `check_ids: []`
explicitly declares a coverage gap, including while planning a future case check.
An exploratory Run can retain requirements before its file/check scope is ready;
its summary says `pending_preparation` and does not emit observation links yet.
`references_validated` means the declaration references were checked, not that
the tests ran or the stated behavior was proved.

Caller `parameters.coverage` reuses the coverage formats below. Its test paths
must belong to the declared inputs. The v2 format binds exact cases through the
selected adapter and compiles explicitly required scenarios into that Check's
existing case rules. In exploratory mode this does not make the Check a gate.
Legacy v1 scenario names remain advisory; only Check-level observations are joined.

The compiled contract adds `requirements`, `requirement_summary`,
`coverage_inventory` and common `observation_links`. Summaries identify the
declaration source, unlinked requirements, requirements without scenarios and
declared gaps. Prepare does not create observations. Check-only links report
`linked_checks_passed` separately from `linked_cases_passed`; neither establishes
goal completeness. Unlinked conditions remain visible even after a permitted
closeout; completion continues to follow the selected Policy.

Caller conditions can be refined via `revise`; the original Intent and pinned
Policy stay separate. A condition change changes the contract hash and resets
the current verification. Design choices, assumptions and open questions belong
in the existing Interpretation fields, not in requirement approval metadata.
Required-case changes to an already pinned Check follow the existing gate
immutability rule; an advisory scenario link can describe an evolving hypothesis.

With configured acceptance, `requirements` and `coverage` come from the pinned
policy top level. Do not put duplicates in policy `parameters`, or override them
through caller `start`/`revise` parameters. Changing that policy requires the
existing operator-configuration/new-Run path. The labels `caller_proposal` and
`operator_configuration` describe input channels, not authenticated authorship.

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
  `minimum_evidence: file|command|testcase`, `kind`, `when`); dangling/duplicate mappings are errors;
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

Develop uses `parameters.execution_checks` for both ordinary commands and
case-aware checks. Selecting the bundled pytest adapter looks like:

```json
{"execution_checks": [
  {"kind":"cases", "id":"behavior", "adapter_id":"pytest-cases-v1",
   "selector":{"paths":["acceptance_tests/test_queue.py"], "args":[]},
   "required_cases":["acceptance_tests/test_queue.py::test_replay_has_one_event"],
   "allowed_outcomes":["passed"], "timeout_seconds":30},
  {"kind":"command", "id":"cli", "argv":["python3","app.py","--help"]}
]}
```

The Domain does not interpret this selector, pytest options or node-ID grammar;
the selected Adapter's data-only schema capability validates them. Develop owns
the common outcome rules and requirement/scenario mapping. The application bridge
still converts old `test_commands` and registered `pytest_checks` lists. A supplied
`execution_checks` field cannot be combined with nonempty legacy lists;
clear old lists explicitly when migrating via `revise`.

Collection paths are explicit input files. Supported selection options are `-k`,
`-m`, `-x`, `--maxfail`, `-q`, `-v`, `--strict-markers` and `--collect-only`.
Collection and execution are observed in the same session. Collection-only
results never satisfy the default execution condition. Project `conftest.py`
and configuration files must be included in the declared input scope when needed.

Use `coverage.schema_version: develop-coverage-v2` for exact case bindings.
Each scenario has `test_ref: {path: "...", case_id: "...::..."}` for this adapter
(optional `framework: "pytest"` is display metadata validated by that adapter)
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

### Domain, Core and execution boundaries

Develop compiles v2 coverage into the contract's optional `observation_links`
(`requirement-case-links-v1`). This common format contains requirement/scenario
IDs, Check IDs and opaque case IDs; `test_ref` is display-only metadata. Core
validates the references and joins them only to observations matching the pinned
Job, Candidate, Check revision and execution identities. It does not interpret
Develop coverage labels, pytest node-ID syntax or the meaning of a requirement.
The link report is informational: it neither creates a gate nor replaces a Check.

Worker calls the common joiner, not a Develop report function. The existing result
field `requirement_observations` is retained; new reports use the framework-neutral
schema `requirement-observations-v1` and include `links_hash`. Previously stored
reports remain readable and are not rewritten.

Case-aware Domain checks specify adapter ID, collection/selection conditions and
outcome rules, not `argv`, `cwd` or private Runner paths. The pytest adapter builds
the actual command and owns its runtime mount and observation artifact. Worker
checks the returned receipt against that materialized command. Ordinary caller-
supplied `execution_checks` with `kind: command` still contain their explicit argv. Pytest remains the
only bundled production adapter; applications can explicitly register others.
As with any pinned implementation change, start a new Run to use the new code;
old Run identities are not silently migrated.

Application composition injects a `CheckPreparationPort` into Develop. The
bundled `AdapterCheckPreparation` bridge converts legacy input aliases, dispatches
selector/reference validation to the registered adapter, and checks its returned
source paths against the admitted input scope. No callback executes tests, reads
the Candidate, or changes a Run. The resulting Check stores adapter `id`, opaque
`selector`, common `source_paths` and Domain-owned `rules`. Neither Develop nor
Core imports a concrete adapter or parses its test names.
The preparation port exposes a stable `identity()`; the built-in bridge hashes
its method implementations, effective configuration and alias dispatch separately
from the used adapters. Default Develop identity includes it. A custom Domain's
explicit `identity_config` remains that implementation's complete configuration
contract; it may be a JSON value or a data-returning method.

### Adapter interface and application registration

The framework-neutral `ExecutionAdapter` port has four methods:

- `runtime(state_root, pinned=None)`: prepare an identity or validate/reuse an
  existing frozen runtime. Raise `AdapterUnavailable` for missing optional setup.
- `command(check)`: describe bounded argv, cwd, exit condition and timeout. It
  must not execute the command or increase the admitted timeout. Worker calls it
  once per Check/subject and uses that same materialization for runtime identity,
  Sandbox execution and receipt comparison. The recorded command digest also
  prevents differing invocations from being treated as comparable baseline runs.
- `stage(state_root, runtime, destination, token)`: validate the pinned runtime
  and stage trusted files into the common Sandbox's read-only resource mount.
- `observer(token, runtime)`: create a fresh session-local `CaseObserver`. Its
  `feed(event)` returns a completed normalized case or None; `finish(error)`
  returns the normalized session summary, including incomplete facts.

To use the same Develop compiler, an adapter also implements the optional
`CaseSchemaAdapter` data-only capability:

- `normalize_selection(selector, inputs, required_cases)` validates framework
  syntax and returns `{selector, source_paths}`. It may not widen input scope or
  change required-case IDs/outcome criteria.
- `validate_reference(selector, reference)` rejects incompatible case/source
  links. Case IDs are opaque to Develop; their grammar belongs here.

Existing adapters used by application-defined Domains need not implement this
capability; selecting one from Develop without it fails explicitly. These schema
methods are fingerprinted with the execution methods. An optional registration
`parameter_alias` requires `legacy_execution_check(value)` to translate old
caller input into a canonical `kind: cases` entry; it never supplies policy approval.
The registration alias is included in the adapter's configuration identity.

An adapter also supplies `adapter_id`, `revision` and a relative `report_path`.
Normalized cases preserve opaque `case_id`, discovered/selected/started/executed/
finished flags, outcome and diagnostic phases. A summary contains cases and
collection/session completion flags. The common transport bounds output, checks
case consistency, invokes only the strict Sandbox, enforces cancellation and
deadlines, and owns checkpoint callbacks. It does not interpret framework events.
Adapter output cannot create gates or Ready; Core compares Domain-defined rules.

Registration belongs to the application's bootstrap, not a caller's Check:

```python
from harness_external.adapter_registry import AdapterRegistration, AdapterRegistry
from harness_external.service import Harness

adapters = AdapterRegistry([
    AdapterRegistration("pytest-cases-v1", "harness_external.pytest_adapter:PytestAdapter"),
    AdapterRegistration("my-cases-v1", "my_application.adapters:MyAdapter", config={}),
])
api = Harness(state_dir, adapters=adapters)  # built-in Develop gets the schema port
```

For an explicit custom Domain registry containing Develop, construct it with
`DevelopModule(check_preparation=AdapterCheckPreparation(adapters))` and pass
that registry as `domains`. A standalone `DevelopModule()` supports file and
ordinary command checks; case-aware preparation requires the injected port.
The default adapter registry also registers the legacy `pytest_checks` alias;
custom registrations can opt into it with `parameter_alias="pytest_checks"`.

Factories are explicit top-level Python classes with JSON-compatible constructor
configuration/defaults. An optional absolute `import_root` names an operator-
managed plugin directory; it is not discovered from the Candidate. Explicit roots
load into private packages without editing the process-wide `sys.path` or replacing
ordinary module names. Use package-relative imports for helpers inside such a root;
absolute imports refer to installed application dependencies. Lazy relative imports
and roots containing the same module names remain separate in Host and Worker.
Registrations without `import_root` use the application's normal installed packages.
Source, methods,
effective configuration and declared `identity_files` dependencies are hashed;
provide stable `identity_file_ids` and, when needed, explicit `identity_config`.
Omitted and explicitly supplied constructor defaults have the same identity.
Factory and adapter methods are trusted application code, must be bounded and
must not execute Candidate code themselves. This is not a sandbox for arbitrary
Python plugins or a transitive dependency/tamper-proof attestation. Keep config
non-secret: registration/binding records are diagnostic data.

Implementation identity is pinned when a Check is admitted; optional runtime
artifacts are bound on verification. Jobs persist only the used registrations so
a detached Worker reconstructs the same configured instances and rejects changed
implementations/runtimes. Restore the same application configuration after a Host
restart. A changed or missing registration cannot silently replace an earlier
selection. Registries/observers are instance/session-scoped, not global callbacks.
Completion records retain the used adapter bindings alongside the Core verifier
identity; neither a plugin name nor a Core digest alone describes that execution.
Only used adapter identities affect a Run; concrete plugin source is separate
from the shared Core execution digest. Historical records remain readable.
The stock CLI uses the built-in pytest registration. Custom applications inject
their registry through `Harness(..., adapters=...)`; there is no model-facing
factory-import API or automatic plugin discovery.

Run revision/check preparation, verification setup and completion file/identity
checks take place outside the SQLite writer transaction. Publication rechecks the
request ID, current Run revision, lifecycle, budget/deadline and execution ownership.
Concurrent replay of the same request publishes once. A conflicting Run change
returns `RUN_CONFLICT` or `VERIFICATION_CONFLICT`; inspect the Run before deciding
whether to retry. A concurrent update is never overwritten by a prepared copy.

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
