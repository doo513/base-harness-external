# Base Harness External

A model-free Develop Harness called by an existing agent through a JSON CLI.
It owns independent Run state, pinned contracts, snapshots and verification jobs.
It does not include a model gateway, agent Host, UI or MCP server.

## Installation

Python 3.11+; no Python runtime dependencies. From the repository root:

```sh
python3 -m pip install -e ".[dev]"
base-harness-external --help
```

Without installation, use `PYTHONPATH=src python3 -m harness_external` on Linux,
or set `PYTHONPATH` to this checkout's `src` in your platform's shell. Command
checks reuse the existing Bun/WSL-or-Linux-namespace Sandbox through a small
adapter; file-only structural checks do not need Bun. In this first release,
command checks require this source checkout and Bun on PATH (or `BUN`). A missing
adapter/Sandbox returns incomplete verification, never an unsandboxed fallback.

The default independent state directory is `$XDG_STATE_HOME/base-harness-external`
(falling back to `~/.local/state/base-harness-external`) on Linux, or
`%LOCALAPPDATA%/base-harness-external` on Windows. Override with `--state-dir`.
State and workspace must be disjoint. `runs.sqlite3` stores Runs, jobs, contracts,
untrusted notes and verification records; `runs/<run_id>/<candidate_id>/payload`
stores snapshots. No Host session database or authentication is used.

### First actual run

The repository includes a small addition implementation and real unittest:

```sh
base-harness-external start --domain develop --goal "Implement integer addition and test it" --workspace examples/external-harness/project --parameters examples/external-harness/parameters.json --request-id demo-start-001
base-harness-external submit --run-id RUN_ID_FROM_START --request-id submit-001
base-harness-external verify --run-id RUN_ID_FROM_START --request-id verify-001
base-harness-external status --run-id RUN_ID_FROM_START --job-id JOB_ID_FROM_VERIFY
base-harness-external finish --run-id RUN_ID_FROM_START --outcome completed --request-id finish-001
```

`verify` returns a durable `job_id` immediately. Poll `status` until the job is
terminal; only a current all-checks-passed result permits `finish completed`.
Use new request IDs for intentional new operations. Retrying the same operation
with the same ID returns the original response; reusing an ID for different input
is an error. A `start` ID identifies that specific Run, not a reusable template.

The agent may edit the workspace with its existing tools between attempts. Use a
new `submit` and `verify` request ID after changes. A new submission invalidates
the prior verification. Harness has no file-editing/apply API and never publishes
changes into the user's workspace.

### API and contract

- `start`: pin original goal, Develop policy revision, parameters and verifier
  implementation digest. `--budget` accepts a JSON file containing `max_actions`,
  `max_verifications`, `timeout_seconds` (defaults 50, 3, 3600).
- `observe --data note.json`: append `{ "note": "...", "references": [] }` as
  **untrusted caller data**. References are not fetched. This cannot create a
  verifier result or change the contract.
- `submit`: capture only the contract's explicit input files; reject links,
  traversal, special files and inputs exceeding the v5 10 MiB combined bound.
- `verify`: run existing Python v5 measurements against that snapshot; test
  commands run only in the existing disposable Sandbox. Submitted-file changes
  detected in the test sandbox invalidate that command result.
- `status`: retrieve durable state; `--check-workspace` additionally compares the
  current listed workspace files/modes against the submitted snapshot.
- `finish --outcome completed|partial|abandoned`: close the Run. Partial/abandoned
  can stop a pending job; the job's `cleanup` field distinguishes a request from
  acknowledged cleanup. A stale worker cannot publish into a closed/newer Run.

Develop parameters are `inputs`, `artifacts`, `expectations`, `test_commands`, and
optional `profile`. Every artifact requires a concrete expectation (`equals`,
`contains`, or `sha256`). The default `execution` profile requires at least one
test command and always expects exit code zero. Explicit `structural` profile
requires file expectations and excludes commands; it says nothing about runtime
behavior. Missing criteria return `NEEDS_INPUT`, not invented success conditions.
Contracts cannot be weakened or revised in place: start a new Run for new criteria.

Run/job state survives CLI exit. Workers use durable ownership and heartbeat
leases; killed workers become `interrupted` when observed, and are **not silently
restarted**. A retry is a new explicit verification attempt against the pinned
snapshot. Budgets cover only Harness work, not external model tokens or actions.
Exhaustion returns a handoff state; it cannot stop the agent outside the Harness.

### Honest assurance boundary

All responses say `assurance: local-advisory`, `ready: false`. A finished record
binds the Run, candidate hash, contract hash, verifier implementation and result
hash, but has **no signature**. Digests detect accidental mismatches; they do not
protect against a same-user model rewriting state, verifier code and hashes.
This is not the legacy GoalContract-v2/Ready protocol and does not invoke its
Ready issuer. A passing check does not prove goal coverage or test independence.

Results describe the submitted snapshot, not later workspace edits or unlisted
files. Command observations retain v5's environment/input-closure limitations.
For tamper-resistant certification, use a separately protected verifier/state/key
and an independent consumer (for example CI); that protected mode is not included.

Tests: `python3 -m pytest -q tests/test_external_harness.py`. On a machine allowing
namespace sandboxing, run the actual execution check with
`BASE_HARNESS_EXTERNAL_LIVE=1 python3 -m pytest -q -s tests/test_external_harness.py -k live_sandboxed`.
Windows-native operation and packaged standalone command-sandbox distribution
have not been verified. Python-only structural execution works without the old Host.

## Validation

This extracted repository was checked independently on WSL/Linux with Python
3.12 and Bun 1.4.0: 50 Python tests passed (one opt-in test skipped), the real
namespace Sandbox test passed separately, and three Sandbox unit tests passed
(three Windows-only tests skipped). No commercial-model quality claim is made.

Run the complete local suite with `python3 -m pytest -q tests`, and the Sandbox
unit suite with `bun test tests/sandbox.test.ts`. See `NOTICE` for extraction
provenance. No local Run databases, credentials, or execution logs are included.
