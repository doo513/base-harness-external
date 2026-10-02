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
  `minimum_evidence: file|command`); dangling/duplicate mappings are errors;
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
