// Narrow reuse adapter. No Host, model provider, session, plugin, or UI imports.
import { randomUUID } from "node:crypto"
import path from "node:path"
import { StrictSandbox } from "../packages/security/src/sandbox"

const controller = new AbortController()
process.once("SIGTERM", () => controller.abort())
process.once("SIGINT", () => controller.abort())
const quote = (value: string) => "'" + value.replaceAll("'", "'\\''") + "'"
try {
  const raw = await Bun.stdin.text()
  if (Buffer.byteLength(raw) > 128 * 1024) throw new Error("SANDBOX_REQUEST_LIMIT")
  const input = JSON.parse(raw)
  if (Object.keys(input).sort().join(",") !== "argv,cwd,timeoutMs,workspace" ||
      typeof input.workspace !== "string" || typeof input.cwd !== "string" ||
      !Array.isArray(input.argv) || !input.argv.length || !input.argv.every((v: unknown) => typeof v === "string" && !v.includes("\0")) ||
      !Number.isSafeInteger(input.timeoutMs) || input.timeoutMs < 1 || input.timeoutMs > 120_000) throw new Error("SANDBOX_REQUEST_INVALID")
  const marker = "external-harness-start-" + randomUUID()
  const startedAt = new Date().toISOString()
  const result = await StrictSandbox.run({
    workspace: input.workspace, cwd: path.resolve(input.workspace, input.cwd),
    command: "printf '%s\\n' " + quote(marker) + "; exec " + input.argv.map(quote).join(" "),
    signal: controller.signal,
    captureChanges: true,
    config: { timeoutMs: input.timeoutMs, maxInputBytes: 10 * 1024 * 1024, maxOutputBytes: 256 * 1024, memoryMiB: 1024, maxProcesses: 64 },
  })
  // Namespace/bootstrap failures must not masquerade as an executed test.
  if (!result.stdout.startsWith(marker + "\n")) {
    process.stdout.write(JSON.stringify({ status: "not_run", reason: "SANDBOX_SETUP_FAILED", provenance: result.provenance }) + "\n")
  } else {
    const changedInputs = result.changes?.filter((change) => change.beforeHash !== null && change.afterHash !== change.beforeHash).map((change) => change.path) ?? []
    const altered = changedInputs.length > 0
    process.stdout.write(JSON.stringify({ status: altered ? "error" : "completed", provenance: result.provenance, capture: {
      argv: input.argv, cwd: input.cwd, startedAt, finishedAt: new Date().toISOString(), execution: "completed",
      stdout: result.stdout.slice(marker.length + 1), stderr: result.stderr,
      ...(altered ? { execution: "error", error: { code: "TEST_MODIFIED_INPUT", message: "Submitted inputs changed in the test sandbox: " + changedInputs.join(", ") } } : { exitCode: result.exitCode }),
    } }) + "\n")
  }
} catch (error) {
  process.stdout.write(JSON.stringify({ status: "not_run", reason: error instanceof Error ? error.message : "SANDBOX_ERROR" }) + "\n")
}
