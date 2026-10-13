import { createHash, randomUUID } from "node:crypto"
import { promises as fs, constants as fsConstants } from "node:fs"
import type { BigIntStats } from "node:fs"
import os from "node:os"
import path from "node:path"

export type SandboxBackendName = "wsl2" | "namespace"

export interface IsolationConfig {
  strictBackend?: "auto" | SandboxBackendName
  wslDistro?: string
  timeoutMs?: number
  memoryMiB?: number
  maxProcesses?: number
  maxOutputBytes?: number
  maxInputBytes?: number
}

export interface SandboxPolicy {
  strictBackend: "auto" | SandboxBackendName
  wslDistro: string
  timeoutMs: number
  memoryMiB: number
  maxProcesses: number
  maxOutputBytes: number
  maxInputBytes: number
}

export interface SandboxProvenance {
  backend: SandboxBackendName
  containment: "user_mount_pid_net_namespace"
  network: "loopback_only"
  state: "completed" | "failed"
  distro?: string
  kernel?: string
  timeoutMs: number
  memoryMiB: number
  maxProcesses: number
  maxOutputBytes: number
  inputBytes: number
  workspaceHash?: string
  code?: string
}

export interface SandboxRequest {
  command: string
  workspace: string
  cwd: string
  config?: IsolationConfig
  signal?: AbortSignal
  /** Return an immutable diff of the throwaway workspace. The caller still has
   * to admit every write and import it through its Candidate overlay. */
  captureChanges?: boolean
  /** Application-owned runtime files mounted read-only; never a Candidate overlay. */
  readonlyResource?: string
  /** Bounded structured telemetry from a new file in the throwaway workspace. */
  observationArtifact?: { path: string; maxBytes: number; onLine: (line: string) => void }
  /** Pinned by the caller before copying. Never derived from a later source scan. */
  inputBinding?: ExecutionInputBinding
}

export interface InputFile { path: string; sha256: string; size: number; executable: boolean }
export interface ExecutionInputBinding {
  schema_version: "execution-input-v1"
  scope_hash: string
  input_hash: string
  files: InputFile[]
}
export interface ExecutionInputReceipt {
  schema_version: "execution-input-receipt-v1"
  scope_hash: string
  input_hash: string
  checked_at: string
  post_checked_at: string
  unchanged: boolean
  changed_inputs: string[]
}

export interface SandboxFileChange {
  /** Slash-separated path relative to the sandbox workspace. */
  path: string
  beforeHash: string | null
  afterHash: string | null
  /** Present exactly when afterHash is non-null. */
  after?: Uint8Array
}

export interface SandboxResult {
  exitCode: number
  stdout: string
  stderr: string
  provenance: SandboxProvenance
  changes?: SandboxFileChange[]
  inputReceipt?: ExecutionInputReceipt
}

export class SandboxError extends Error {
  constructor(
    readonly code:
      | "SANDBOX_UNAVAILABLE"
      | "SANDBOX_POLICY_DENIED"
      | "SANDBOX_LIMIT_EXCEEDED"
      | "SANDBOX_ESCAPE_ATTEMPT"
      | "EXECUTION_INPUT_INVALID"
      | "EXECUTION_INPUT_MISMATCH",
    message: string,
    readonly provenance?: Partial<SandboxProvenance>,
  ) {
    super(message)
    this.name = "SandboxError"
  }
}

export const isolationPolicy = (input: IsolationConfig = {}): SandboxPolicy => ({
  strictBackend: input.strictBackend ?? "auto",
  wslDistro: input.wslDistro ?? "Ubuntu-24.04",
  timeoutMs: input.timeoutMs ?? 120_000,
  memoryMiB: input.memoryMiB ?? 4096,
  maxProcesses: input.maxProcesses ?? 128,
  maxOutputBytes: input.maxOutputBytes ?? 10 * 1024 * 1024,
  maxInputBytes: input.maxInputBytes ?? 2 * 1024 * 1024 * 1024,
})

const canonical = (value: string) => (process.platform === "win32" ? value.toLowerCase() : value)
const inside = (parent: string, child: string) => {
  const relative = path.relative(canonical(parent), canonical(child))
  return relative === "" || (!relative.startsWith(".." + path.sep) && relative !== ".." && !path.isAbsolute(relative))
}

export async function validateSandboxWorkspace(workspace: string, maxInputBytes: number) {
  if (process.platform === "win32" && (/^\\\\[?.]\\/.test(workspace) || /^\\\\/.test(workspace))) {
    throw new SandboxError("SANDBOX_ESCAPE_ATTEMPT", "UNC and Windows device paths are not valid sandbox roots")
  }
  const root = await fs.realpath(workspace).catch(() => {
    throw new SandboxError("SANDBOX_UNAVAILABLE", "Sandbox workspace does not exist")
  })
  let bytes = 0
  const visit = async (directory: string): Promise<void> => {
    for (const entry of await fs.readdir(directory, { withFileTypes: true })) {
      const target = path.join(directory, entry.name)
      const stat = await fs.lstat(target)
      if (stat.isSymbolicLink()) {
        const resolved = await fs.realpath(target).catch(() => "")
        if (!resolved || !inside(root, resolved)) {
          throw new SandboxError("SANDBOX_ESCAPE_ATTEMPT", `Sandbox link escapes workspace: ${target}`)
        }
        continue
      }
      if (stat.isDirectory()) await visit(target)
      else if (stat.isFile()) bytes += stat.size
      else throw new SandboxError("SANDBOX_ESCAPE_ATTEMPT", `Unsupported sandbox filesystem entry: ${target}`)
      if (bytes > maxInputBytes) {
        throw new SandboxError("SANDBOX_LIMIT_EXCEEDED", "Sandbox input exceeds configured maxInputBytes")
      }
    }
  }
  await visit(root)
  return { root, bytes }
}

type Capture = { code: number; stdout: string; stderr: string }

async function execute(
  argv: string[],
  options: { cwd?: string; timeoutMs: number; maxOutputBytes: number; signal?: AbortSignal },
): Promise<Capture> {
  options.signal?.throwIfAborted()
  const child = Bun.spawn(argv, {
    cwd: options.cwd,
    stdin: "ignore",
    stdout: "pipe",
    stderr: "pipe",
    env: process.env,
  })
  let exceeded = false
  let timedOut = false
  const read = async (stream: ReadableStream<Uint8Array>) => {
    const reader = stream.getReader()
    const chunks: Uint8Array[] = []
    let size = 0
    while (true) {
      const next = await reader.read()
      if (next.done) break
      size += next.value.byteLength
      if (size > options.maxOutputBytes) {
        exceeded = true
        child.kill()
        break
      }
      chunks.push(next.value)
    }
    return Buffer.concat(chunks).toString("utf8")
  }
  const abort = () => child.kill()
  options.signal?.addEventListener("abort", abort, { once: true })
  if (options.signal?.aborted) abort()
  const timer = setTimeout(() => {
    timedOut = true
    child.kill()
  }, options.timeoutMs + 5_000)
  try {
    const [code, stdout, stderr] = await Promise.all([
      child.exited,
      read(child.stdout as ReadableStream<Uint8Array>),
      read(child.stderr as ReadableStream<Uint8Array>),
    ])
    if (exceeded) throw new SandboxError("SANDBOX_LIMIT_EXCEEDED", "Sandbox output exceeded maxOutputBytes")
    if (timedOut) throw new SandboxError("SANDBOX_LIMIT_EXCEEDED", "Sandbox process exceeded timeoutMs")
    if (options.signal?.aborted) throw new SandboxError("SANDBOX_POLICY_DENIED", "Sandbox execution was cancelled")
    return { code, stdout, stderr }
  } finally {
    clearTimeout(timer)
    options.signal?.removeEventListener("abort", abort)
  }
}

const runner = String.raw`
root=$1
workspace=$2
workdir=$3
seconds=$4
memory=$5
processes=$6
filesize=$7
command=$8
resource=$9
mount -t tmpfs -o mode=755 tmpfs "$root"
mkdir -p "$root/bin" "$root/usr" "$root/lib" "$root/lib64" "$root/etc" "$root/dev" "$root/proc" "$root/sys" "$root/tmp" "$root/workspace"
for source in /bin /usr /lib /lib64 /etc /dev /sys; do
  [ -e "$source" ] || continue
  mount --rbind "$source" "$root$source"
  mount -o remount,ro,bind "$root$source"
done
mount --bind "$workspace" "$root/workspace"
mount -o remount,rw,bind "$root/workspace"
if [ -n "$resource" ]; then
  mkdir -p "$root/opt/harness-runtime"
  mount --bind "$resource" "$root/opt/harness-runtime"
  mount -o remount,ro,bind "$root/opt/harness-runtime"
fi
mount -t proc proc "$root/proc"
mount -t tmpfs -o mode=1777 tmpfs "$root/tmp"
mkdir -p "$root/tmp/home"
ip link set lo up
exec chroot "$root" /usr/bin/env -i PATH=/usr/local/bin:/usr/bin:/bin HOME=/tmp/home TMPDIR=/tmp LANG=C.UTF-8 LC_ALL=C.UTF-8 TERM=dumb /usr/bin/prlimit --as="$memory" --nproc="$processes" --fsize="$filesize" --cpu="$seconds" /usr/bin/timeout --signal=TERM --kill-after=5s "$seconds"s /usr/bin/setsid /bin/sh -lc "cd \"$workdir\" && exec /bin/sh -lc \"\$1\"" sandbox-command "$command"
`

const safeId = () => randomUUID().replaceAll("-", "")
const byteOrder = (left: string, right: string) => Buffer.compare(Buffer.from(left, "utf8"), Buffer.from(right, "utf8"))

/** Fixed keys match Python canonical_bytes, including Unicode paths. */
export const inputDigest = (files: InputFile[]) => createHash("sha256").update(JSON.stringify(
  files.map(({ path, sha256, size, executable }) => ({ executable, path, sha256, size })),
), "utf8").digest("hex")

export function validateInputBinding(value: unknown): ExecutionInputBinding {
  const invalid = (): never => { throw new SandboxError("EXECUTION_INPUT_INVALID", "Missing or invalid pinned execution input binding") }
  if (!value || typeof value !== "object") return invalid()
  const binding = value as ExecutionInputBinding
  if (Object.keys(binding).sort().join(",") !== "files,input_hash,schema_version,scope_hash" ||
      binding.schema_version !== "execution-input-v1" || typeof binding.scope_hash !== "string" || !/^[0-9a-f]{64}$/.test(binding.scope_hash) ||
      typeof binding.input_hash !== "string" || !/^[0-9a-f]{64}$/.test(binding.input_hash) || !Array.isArray(binding.files) || binding.files.length > 128) return invalid()
  let total = 0
  let previous: string | undefined
  for (const file of binding.files) {
    if (!file || Object.keys(file).sort().join(",") !== "executable,path,sha256,size" ||
        typeof file.path !== "string" || file.path.length === 0 || [...file.path].length > 512 ||
        /[\x00-\x1f\x7f\\:]/.test(file.path) || file.path.split("/").some(part => !part || part === "." || part === ".." || /[ .]$/.test(part)) ||
        typeof file.sha256 !== "string" || !/^[0-9a-f]{64}$/.test(file.sha256) || typeof file.executable !== "boolean" ||
        !Number.isSafeInteger(file.size) || file.size < 0 || (previous !== undefined && byteOrder(previous, file.path) >= 0)) return invalid()
    previous = file.path
    total += file.size
  }
  if (total > 10 * 1024 * 1024 || inputDigest(binding.files) !== binding.input_hash || Buffer.byteLength(JSON.stringify(binding)) > 512 * 1024) return invalid()
  return structuredClone(binding)
}

function watchArtifact(workspace: string, artifact: SandboxRequest["observationArtifact"]) {
  if (!artifact) return async () => {}
  if (!/^[A-Za-z0-9_.-]{1,160}$/.test(artifact.path) || artifact.path === "." || artifact.path === ".." ||
      !Number.isSafeInteger(artifact.maxBytes) || artifact.maxBytes < 1 || artifact.maxBytes > 8 * 1024 * 1024) {
    throw new SandboxError("SANDBOX_POLICY_DENIED", "Invalid observation artifact")
  }
  let position = 0
  let pending = ""
  let failure: unknown
  let identity: string | undefined
  const decoder = new TextDecoder("utf-8", { fatal: true })
  const drain = async () => {
    let file
    try {
      file = await fs.open(path.join(workspace, artifact.path), fsConstants.O_RDONLY | fsConstants.O_NOFOLLOW)
    } catch (error: any) {
      if (error?.code === "ENOENT") return
      throw error
    }
    try {
      const info = await file.stat()
      const current = `${info.dev}:${info.ino}`
      if (!info.isFile() || info.nlink !== 1 || (identity && current !== identity) || info.size < position || info.size > artifact.maxBytes) {
        throw new SandboxError("SANDBOX_LIMIT_EXCEEDED", "Observation artifact changed identity or exceeded bounds")
      }
      identity = current
      const bytes = Buffer.alloc(info.size - position)
      const read = await file.read(bytes, 0, bytes.length, position)
      position += read.bytesRead
      pending += decoder.decode(bytes.subarray(0, read.bytesRead), { stream: true })
      let end
      while ((end = pending.indexOf("\n")) >= 0) {
        artifact.onLine(pending.slice(0, end))
        pending = pending.slice(end + 1)
      }
    } finally {
      await file.close()
    }
  }
  let chain = Promise.resolve()
  const timer = setInterval(() => { chain = chain.then(drain).catch(error => { failure = error }) }, 100)
  return async () => {
    clearInterval(timer)
    await chain
    await drain()
    if (failure) throw failure
    // An interrupted final line is not a complete observation.
  }
}

type WorkspaceScan = { files: Map<string, InputFile & { bytes?: Buffer }>; links: Map<string, string> }
const statIdentity = (info: BigIntStats) => [info.dev, info.ino, info.mode, info.nlink, info.size, info.mtimeNs, info.ctimeNs].join(":")

/** Fresh bounded reads of the copy. Pre-execution state keeps hashes, not bodies. */
async function scanWorkspace(root: string, maxBytes: number, before?: WorkspaceScan): Promise<WorkspaceScan> {
  const scan = async (root: string) => {
    const files: WorkspaceScan["files"] = new Map()
    const links = new Map<string, string>()
    let bytes = 0
    const visit = async (directory: string): Promise<void> => {
      const directoryBefore = await fs.lstat(directory, { bigint: true })
      if (!directoryBefore.isDirectory() || directoryBefore.isSymbolicLink()) throw new SandboxError("SANDBOX_ESCAPE_ATTEMPT", "Snapshot directory changed")
      for (const item of await fs.readdir(directory, { withFileTypes: true })) {
        const target = path.join(directory, item.name)
        const relative = path.relative(root, target).split(path.sep).join("/")
        const stat = await fs.lstat(target, { bigint: true })
        if (stat.isDirectory()) await visit(target)
        else if (stat.isSymbolicLink()) links.set(relative, await fs.readlink(target))
        else if (stat.isFile()) {
          if (stat.nlink !== 1n) throw new SandboxError("SANDBOX_ESCAPE_ATTEMPT", `Snapshot has a hard link: ${relative}`)
          if (Number(stat.size) > maxBytes - bytes) throw new SandboxError("SANDBOX_LIMIT_EXCEEDED", "Snapshot exceeds maxInputBytes")
          const handle = await fs.open(target, fsConstants.O_RDONLY | fsConstants.O_NOFOLLOW | fsConstants.O_NONBLOCK)
          try {
            const opened = await handle.stat({ bigint: true })
            if (statIdentity(stat) !== statIdentity(opened)) throw new SandboxError("EXECUTION_INPUT_MISMATCH", "Snapshot changed before reading")
            const digest = createHash("sha256")
            const buffer = Buffer.alloc(64 * 1024)
            const chunks: Buffer[] = []
            let size = 0
            while (true) {
              const { bytesRead } = await handle.read(buffer, 0, Math.min(buffer.length, maxBytes - bytes + 1), null)
              if (!bytesRead) break
              bytes += bytesRead
              size += bytesRead
              if (bytes > maxBytes) throw new SandboxError("SANDBOX_LIMIT_EXCEEDED", "Snapshot exceeds maxInputBytes")
              digest.update(buffer.subarray(0, bytesRead))
              if (before) chunks.push(Buffer.from(buffer.subarray(0, bytesRead)))
            }
            const after = await handle.stat({ bigint: true })
            const named = await fs.lstat(target, { bigint: true })
            if (statIdentity(opened) !== statIdentity(after) || statIdentity(after) !== statIdentity(named)) {
              throw new SandboxError("EXECUTION_INPUT_MISMATCH", "Snapshot changed during reading")
            }
            const sha256 = digest.digest("hex")
            files.set(relative, { path: relative, sha256, size, executable: Boolean(stat.mode & 0o111n),
              ...(before && before.files.get(relative)?.sha256 !== sha256 ? { bytes: Buffer.concat(chunks) } : {}) })
          } finally { await handle.close() }
        } else throw new SandboxError("SANDBOX_ESCAPE_ATTEMPT", `Unsupported sandbox output entry: ${relative}`)
      }
      if (statIdentity(directoryBefore) !== statIdentity(await fs.lstat(directory, { bigint: true }))) {
        throw new SandboxError("EXECUTION_INPUT_MISMATCH", "Snapshot directory changed during reading")
      }
    }
    await visit(root)
    return { files, links }
  }
  return scan(root)
}

function scanInventory(scan: WorkspaceScan): InputFile[] {
  return [...scan.files.values()].sort((a, b) => byteOrder(a.path, b.path))
    .map(({ path, sha256, size, executable }) => ({ path, sha256, size, executable }))
}

function admitInputs(scan: WorkspaceScan, binding: ExecutionInputBinding) {
  if (scan.links.size || inputDigest(scanInventory(scan)) !== binding.input_hash) {
    throw new SandboxError("EXECUTION_INPUT_MISMATCH", "Actual execution copy differs from the pinned file inventory")
  }
}

function inputReceipt(before: WorkspaceScan, after: WorkspaceScan, binding: ExecutionInputBinding, checkedAt: string): ExecutionInputReceipt {
  const changed = [...before.files.values()].filter(left => {
    const right = after.files.get(left.path)
    return !right || left.sha256 !== right.sha256 || left.size !== right.size || left.executable !== right.executable
  }).map(file => file.path).sort(byteOrder)
  return { schema_version: "execution-input-receipt-v1", scope_hash: binding.scope_hash,
    input_hash: inputDigest(scanInventory(before)), checked_at: checkedAt, post_checked_at: new Date().toISOString(),
    unchanged: changed.length === 0, changed_inputs: changed }
}

function captureWorkspaceChanges(before: WorkspaceScan, after: WorkspaceScan): SandboxFileChange[] {
  if (before.links.size !== after.links.size || [...before.links].some(([key, value]) => after.links.get(key) !== value)) {
    throw new SandboxError("SANDBOX_ESCAPE_ATTEMPT", "Sandbox commands cannot create, remove, or retarget symbolic links")
  }
  const paths = [...new Set([...before.files.keys(), ...after.files.keys()])].sort()
  return paths.flatMap((relativePath) => {
    const left = before.files.get(relativePath)
    const right = after.files.get(relativePath)
    if (left?.sha256 === right?.sha256) return []
    return [{ path: relativePath, beforeHash: left?.sha256 ?? null, afterHash: right?.sha256 ?? null,
      ...(right ? { after: new Uint8Array(right.bytes!) } : {}) }]
  })
}

async function checked(argv: string[], timeoutMs = 30_000) {
  const result = await execute(argv, { timeoutMs, maxOutputBytes: 1024 * 1024 })
  if (result.code !== 0) {
    throw new SandboxError("SANDBOX_UNAVAILABLE", result.stderr.trim() || `Sandbox setup failed: ${argv[0]}`)
  }
  return result.stdout.trim()
}

async function wslRun(request: SandboxRequest, policy: SandboxPolicy, root: string, inputBytes: number) {
  const prefix = ["wsl.exe", "-d", policy.wslDistro, "--exec"]
  const probe = await checked([...prefix, "/bin/sh", "-lc", "command -v unshare prlimit timeout setsid mount chroot ip >/dev/null && uname -r"])
  const home = await checked([...prefix, "/bin/sh", "-lc", 'printf %s "$HOME"'])
  const source = await checked([...prefix, "/usr/bin/wslpath", "-a", root])
  const id = safeId()
  const base = `${home}/.local/state/base-harness/sandboxes/${id}`
  const workspace = `${base}/workspace`
  const mountRoot = `${base}/root`
  const relative = path.relative(root, request.cwd).replaceAll("\\", "/")
  if (relative === ".." || relative.startsWith("../") || path.isAbsolute(relative)) {
    throw new SandboxError("SANDBOX_ESCAPE_ATTEMPT", "Sandbox working directory is outside workspace")
  }
  const workdir = relative ? `/workspace/${relative}` : "/workspace"
  const captured = request.captureChanges ? await fs.mkdtemp(path.join(os.tmpdir(), "base-harness-sandbox-capture-")) : undefined
  try {
    await checked([...prefix, "/bin/mkdir", "-p", workspace, mountRoot])
    await checked([...prefix, "/bin/cp", "-a", "--no-preserve=links", `${source}/.`, workspace], Math.max(30_000, policy.timeoutMs))
    const before = request.captureChanges ? await scanWorkspace(root, policy.maxInputBytes) : undefined
    const seconds = Math.max(1, Math.ceil(policy.timeoutMs / 1000))
    const result = await execute(
      [
        ...prefix,
        "/usr/bin/unshare",
        "--user",
        "--map-root-user",
        "--mount",
        "--pid",
        "--fork",
        "--kill-child=KILL",
        "--net",
        "/bin/sh",
        "-eu",
        "-c",
        runner,
        "sandbox-runner",
        mountRoot,
        workspace,
        workdir,
        String(seconds),
        String(policy.memoryMiB * 1024 * 1024),
        String(policy.maxProcesses),
        String(512 * 1024 * 1024),
        request.command,
        "",
      ],
      { timeoutMs: policy.timeoutMs, maxOutputBytes: policy.maxOutputBytes, signal: request.signal },
    )
    let changes: SandboxFileChange[] | undefined
    if (captured) {
      const captureTarget = await checked([...prefix, "/usr/bin/wslpath", "-a", captured])
      await checked([...prefix, "/bin/cp", "-a", "--no-preserve=links", `${workspace}/.`, captureTarget], Math.max(30_000, policy.timeoutMs))
      changes = captureWorkspaceChanges(before!, await scanWorkspace(captured, policy.maxInputBytes, before))
    }
    return {
      exitCode: result.code,
      stdout: result.stdout,
      stderr: result.stderr,
      provenance: {
        backend: "wsl2" as const,
        containment: "user_mount_pid_net_namespace" as const,
        network: "loopback_only" as const,
        state: "completed" as const,
        distro: policy.wslDistro,
        kernel: probe,
        timeoutMs: policy.timeoutMs,
        memoryMiB: policy.memoryMiB,
        maxProcesses: policy.maxProcesses,
        maxOutputBytes: policy.maxOutputBytes,
        inputBytes,
      },
      ...(changes ? { changes } : {}),
    }
  } finally {
    if (base.startsWith(`${home}/.local/state/base-harness/sandboxes/`)) {
      await execute([...prefix, "/bin/rm", "-rf", "--", base], { timeoutMs: 30_000, maxOutputBytes: 1024 * 1024 }).catch(() => undefined)
    }
    if (captured) await fs.rm(captured, { recursive: true, force: true })
  }
}

async function namespaceRun(request: SandboxRequest, policy: SandboxPolicy, root: string, inputBytes: number) {
  if (process.platform === "win32") throw new SandboxError("SANDBOX_UNAVAILABLE", "Linux namespace backend is unavailable on Windows")
  const id = safeId()
  const base = path.join(process.env.XDG_STATE_HOME ?? path.join(os.homedir(), ".local", "state"), "base-harness", "sandboxes", id)
  const workspace = path.join(base, "workspace")
  const mountRoot = path.join(base, "root")
  const relative = path.relative(root, request.cwd)
  if (relative === ".." || relative.startsWith(".." + path.sep) || path.isAbsolute(relative)) {
    throw new SandboxError("SANDBOX_ESCAPE_ATTEMPT", "Sandbox working directory is outside workspace")
  }
  await fs.mkdir(mountRoot, { recursive: true })
  let stopObserving = async () => {}
  try {
    await fs.cp(root, workspace, { recursive: true, dereference: false, verbatimSymlinks: true })
    const before = request.captureChanges || request.inputBinding ? await scanWorkspace(workspace, policy.maxInputBytes) : undefined
    if (request.inputBinding) admitInputs(before!, request.inputBinding)
    const checkedAt = new Date().toISOString()
    const seconds = Math.max(1, Math.ceil(policy.timeoutMs / 1000))
    // No command or case event runs before admitting the actual copied tree.
    stopObserving = watchArtifact(workspace, request.observationArtifact)
    const result = await execute(
      [
        "unshare",
        "--user",
        "--map-root-user",
        "--mount",
        "--pid",
        "--fork",
        "--kill-child=KILL",
        "--net",
        "/bin/sh",
        "-eu",
        "-c",
        runner,
        "sandbox-runner",
        mountRoot,
        workspace,
        relative ? `/workspace/${relative.replaceAll(path.sep, "/")}` : "/workspace",
        String(seconds),
        String(policy.memoryMiB * 1024 * 1024),
        String(policy.maxProcesses),
        String(512 * 1024 * 1024),
        request.command,
        request.readonlyResource ?? "",
      ],
      { timeoutMs: policy.timeoutMs, maxOutputBytes: policy.maxOutputBytes, signal: request.signal },
    )
    const after = before ? await scanWorkspace(workspace, policy.maxInputBytes, request.captureChanges ? before : undefined) : undefined
    const changes = request.captureChanges ? captureWorkspaceChanges(before!, after!) : undefined
    const receipt = request.inputBinding ? inputReceipt(before!, after!, request.inputBinding, checkedAt) : undefined
    return {
      exitCode: result.code,
      stdout: result.stdout,
      stderr: result.stderr,
      provenance: {
        backend: "namespace" as const,
        containment: "user_mount_pid_net_namespace" as const,
        network: "loopback_only" as const,
        state: "completed" as const,
        kernel: os.release(),
        timeoutMs: policy.timeoutMs,
        memoryMiB: policy.memoryMiB,
        maxProcesses: policy.maxProcesses,
        maxOutputBytes: policy.maxOutputBytes,
        inputBytes,
      },
      ...(changes ? { changes } : {}),
      ...(receipt ? { inputReceipt: receipt } : {}),
    }
  } finally {
    try { await stopObserving() } finally {
    await fs.rm(base, { recursive: true, force: true })
    }
  }
}

export class SandboxManager {
  async run(request: SandboxRequest): Promise<SandboxResult> {
    if (request.command.includes("\0")) throw new SandboxError("SANDBOX_POLICY_DENIED", "Sandbox command contains NUL")
    const policy = isolationPolicy(request.config)
    if (request.inputBinding !== undefined) request = { ...request, inputBinding: validateInputBinding(request.inputBinding) }
    const input = await validateSandboxWorkspace(request.workspace, policy.maxInputBytes)
    request.signal?.throwIfAborted()
    const backend = policy.strictBackend === "auto" ? (process.platform === "win32" ? "wsl2" : "namespace") : policy.strictBackend
    if (backend === "wsl2") {
      if (request.inputBinding) throw new SandboxError("SANDBOX_UNAVAILABLE", "Pinned execution input verification requires the namespace backend")
      if (request.readonlyResource || request.observationArtifact) throw new SandboxError("SANDBOX_UNAVAILABLE", "Structured adapter resources require the namespace backend")
      if (process.platform !== "win32") throw new SandboxError("SANDBOX_UNAVAILABLE", "WSL2 backend requires Windows")
      return wslRun(request, policy, input.root, input.bytes)
    }
    if (request.readonlyResource) {
      const resource = await validateSandboxWorkspace(request.readonlyResource, policy.maxInputBytes)
      request = { ...request, readonlyResource: resource.root }
    }
    return namespaceRun(request, policy, input.root, input.bytes)
  }
}

export const StrictSandbox = new SandboxManager()
