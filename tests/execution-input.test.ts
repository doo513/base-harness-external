import { afterEach, expect, spyOn, test } from "bun:test"
import { createHash } from "node:crypto"
import { promises as fs, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { SandboxManager, inputDigest, validateInputBinding } from "../runtime/packages/security/src/sandbox"
import type { ExecutionInputBinding } from "../runtime/packages/security/src/sandbox"

const roots: string[] = []
const originalState = process.env.XDG_STATE_HOME
afterEach(async () => {
  if (originalState === undefined) delete process.env.XDG_STATE_HOME
  else process.env.XDG_STATE_HOME = originalState
  await Promise.all(roots.splice(0).map(root => fs.rm(root, { recursive: true, force: true })))
})
const digest = (value: string) => createHash("sha256").update(value).digest("hex")
async function fixture() {
  const root = await fs.mkdtemp(join(tmpdir(), "execution-input-"))
  roots.push(root)
  const workspace = join(root, "project")
  await fs.mkdir(workspace)
  await fs.writeFile(join(workspace, "data"), "ready", { mode: 0o644 })
  process.env.XDG_STATE_HOME = join(root, "state")
  const files = [{ path: "data", sha256: digest("ready"), size: 5, executable: false }]
  const inputBinding: ExecutionInputBinding = { schema_version: "execution-input-v1", scope_hash: "a".repeat(64), input_hash: inputDigest(files), files }
  return { root, workspace, request: { workspace, cwd: workspace, command: "cat data", inputBinding,
    captureChanges: true, config: { strictBackend: "namespace" as const, timeoutMs: 5000, maxInputBytes: 1024 * 1024 } } }
}

test("input binding validates ordered exact fields without file access", () => {
  const files = [{ path: "a/한글😀", sha256: digest("ready"), size: 5, executable: false }]
  const valid: ExecutionInputBinding = { schema_version: "execution-input-v1", scope_hash: "a".repeat(64), input_hash: inputDigest(files), files }
  expect(validateInputBinding(valid)).toEqual(valid)
  for (const changed of [undefined, { ...valid, input_hash: "0".repeat(64) }, { ...valid, files: [true] },
    { ...valid, scope_hash: "foreign" }, { ...valid, files: [files[0], files[0]] }]) {
    expect(() => validateInputBinding(changed)).toThrow("pinned execution input")
  }
})

test("a backend without execution-side input admission must fail closed", async () => {
  const { request } = await fixture()
  await expect(new SandboxManager().run({ ...request, config: { ...request.config, strictBackend: "wsl2" } }))
    .rejects.toMatchObject({ code: "SANDBOX_UNAVAILABLE" })
})

for (const mutation of ["bytes", "missing", "extra", "executable", "symlink", "hardlink"]) {
  test(`actual copy admission rejects ${mutation}, before any command or observation`, async () => {
    const { root, request } = await fixture()
    const copy = fs.cp
    const cp = spyOn(fs, "cp").mockImplementation(async (source, destination, options) => {
      await copy(source, destination, options)
      const target = join(String(destination), "data")
      if (mutation === "bytes") await fs.writeFile(target, "wrong")
      if (mutation === "missing") await fs.unlink(target)
      if (mutation === "extra") await fs.writeFile(join(String(destination), "unsubmitted"), "extra")
      if (mutation === "executable") await fs.chmod(target, 0o755)
      if (mutation === "symlink") { await fs.unlink(target); await fs.symlink(join(request.workspace, "data"), target) }
      if (mutation === "hardlink") await fs.link(target, join(String(destination), "alias"))
    })
    const spawn = spyOn(Bun, "spawn").mockImplementation(() => { throw new Error("COMMAND_MUST_NOT_START") })
    const onLine = () => { throw new Error("CASE_MUST_NOT_BE_OBSERVED") }
    try {
      await expect(new SandboxManager().run({ ...request, observationArtifact: { path: "events", maxBytes: 1000, onLine } }))
        .rejects.toMatchObject({ code: mutation === "hardlink" ? "SANDBOX_ESCAPE_ATTEMPT" : "EXECUTION_INPUT_MISMATCH" })
      expect(spawn).not.toHaveBeenCalled()
      expect(await fs.readFile(join(request.workspace, "data"), "utf8")).toBe("ready")
      expect(await fs.readdir(join(root, "state/base-harness/sandboxes"))).toEqual([])
    } finally { cp.mockRestore(); spawn.mockRestore() }
  })
}

test.skipIf(process.env.BASE_HARNESS_EXTERNAL_LIVE !== "1")("after comparison never rereads a mutable original as its baseline", async () => {
  const { request } = await fixture()
  const spawn = Bun.spawn
  const calls = spyOn(Bun, "spawn").mockImplementation((...args: any[]) => {
    writeFileSync(join(request.workspace, "data"), "other")
    return (spawn as any)(...args)
  })
  try {
    const result = await new SandboxManager().run(request)
    expect(result.exitCode).toBe(0)
    expect(result.stdout).toBe("ready")
    expect(result.changes).toEqual([])
    expect(result.inputReceipt?.unchanged).toBe(true)
    expect(result.inputReceipt?.input_hash).toBe(request.inputBinding.input_hash)
    expect(await fs.readFile(join(request.workspace, "data"), "utf8")).toBe("other")
    // This is only Sandbox's copy receipt. Worker's fresh source post-check
    // still rejects this persistent outer edit; it is not waived by the receipt.
  } finally { calls.mockRestore() }
})

test.skipIf(process.env.BASE_HARNESS_EXTERNAL_LIVE !== "1")("generic capture keeps changed bytes and submitted executable changes observable", async () => {
  const { request } = await fixture()
  const result = await new SandboxManager().run({ ...request, command: "printf wrong > data; chmod +x data; printf new > output" })
  expect(result.exitCode).toBe(0)
  expect(result.changes?.map(change => change.path)).toEqual(["data", "output"])
  expect(new TextDecoder().decode(result.changes![0].after)).toBe("wrong")
  expect(result.inputReceipt?.unchanged).toBe(false)
  expect(result.inputReceipt?.changed_inputs).toEqual(["data"])
})

test.skipIf(process.env.BASE_HARNESS_EXTERNAL_LIVE !== "1")("input binding uses two fresh copy reads, not another original read or a cached post-check", async () => {
  const { request } = await fixture()
  const open = fs.open
  const reads: { path: string; bytes: number }[] = []
  const calls = spyOn(fs, "open").mockImplementation(async (...args: any[]) => {
    const handle = await (open as any)(...args)
    const observed = { path: String(args[0]), bytes: 0 }
    reads.push(observed)
    const read = handle.read.bind(handle)
    handle.read = async (...options: any[]) => {
      const result = await read(...options)
      observed.bytes += result.bytesRead
      return result
    }
    return handle
  })
  try {
    const result = await new SandboxManager().run(request)
    expect(result.inputReceipt?.unchanged).toBe(true)
    expect(reads.length).toBe(2)
    expect(reads.map(item => item.bytes)).toEqual([5, 5])
    expect(reads.every(item => item.path !== join(request.workspace, "data"))).toBe(true)
    console.log("COPY_READ_COST: 2 fresh hash passes, 10 bytes total, 0 original hash reads; no cross-phase hash reuse")
  } finally { calls.mockRestore() }
})
