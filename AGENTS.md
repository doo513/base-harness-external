# Base Harness External

- This repository is an external verification tool, not an agent runtime.
- Preserve the start/observe/submit/verify/status/finish JSON interface.
- Model notes and test expectations are untrusted inputs. Do not present them as independent evidence.
- All results are local-advisory, unsigned, and ready=false. Do not add Ready claims without a protected verifier and independent consumer.
- Keep Run state outside and independent of the caller's workspace/session.
- Do not edit or publish workspace files; verify pinned snapshots.
- Test commands may execute only through the existing strict Sandbox adapter. Never add an unsandboxed fallback.
- Preserve request idempotency, deadlines, budgets, ownership and late-result rejection.
- The Python package has no runtime dependencies. Bun is currently required only for sandboxed command checks from the source checkout.
- Do not restore the old Host, UI, provider gateway or legacy Ready engine.
- Run Python tests and an opt-in real Sandbox test when changing execution boundaries.
