import { createHash, randomUUID } from "node:crypto"
import { promises as fs, constants as fsConstants, watch as watchFileSystem } from "node:fs"
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
  timingMs?: { setup: number; execution: number; observation: number; cleanup: number }
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
  /** Trusted scenario setup in the disposable copy; cannot overlap submitted inputs. */
  fixtures?: { path: string; text: string }[]
  /** Explicit bounded paths observed by the parent, outside the Candidate namespace. */
  observedFiles?: string[]
  /** Fixture paths whose attempted mutation invalidates an otherwise restored result. */
  protectedFiles?: string[]
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
  fileObservations?: { path: string; before: FileState; after: FileState; afterBase64?: string; contentStatus?: string }[]
  protectedChanges?: string[]
}

interface FileState { kind: "absent" | "directory" | "file"; sha256?: string; size?: number; executable?: boolean; mtimeNs?: string }

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
  memoryMiB: input.memoryMiB ?? 1024,
  maxProcesses: input.maxProcesses ?? 32,
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
  let entries = 0
  const visit = async (directory: string, depth = 0): Promise<void> => {
    if (depth > 64) throw new SandboxError("SANDBOX_LIMIT_EXCEEDED", "Sandbox directory depth exceeds 64")
    for (const entry of await fs.readdir(directory, { withFileTypes: true })) {
      entries += 1
      if (entries > 4096) throw new SandboxError("SANDBOX_LIMIT_EXCEEDED", "Sandbox file/directory count exceeds 4096")
      const target = path.join(directory, entry.name)
      const stat = await fs.lstat(target)
      if (stat.isSymbolicLink()) {
        const resolved = await fs.realpath(target).catch(() => "")
        if (!resolved || !inside(root, resolved)) {
          throw new SandboxError("SANDBOX_ESCAPE_ATTEMPT", `Sandbox link escapes workspace: ${target}`)
        }
        continue
      }
      if (stat.isDirectory()) await visit(target, depth + 1)
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
  options: { cwd?: string; timeoutMs: number; maxOutputBytes: number; signal?: AbortSignal; monitor?: (pid: number) => Promise<void> },
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
  let monitorFailure: unknown
  let monitorTask: Promise<void> | undefined
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
  let monitoring = false
  const monitor = options.monitor ? setInterval(() => {
    if (monitoring || monitorFailure) return
    monitoring = true
    monitorTask = options.monitor!(child.pid).catch((error) => { monitorFailure = error; child.kill() })
      .finally(() => { monitoring = false })
  }, 200) : undefined
  try {
    const [code, stdout, stderr] = await Promise.all([
      child.exited,
      read(child.stdout as ReadableStream<Uint8Array>),
      read(child.stderr as ReadableStream<Uint8Array>),
    ])
    if (monitorTask) await monitorTask
    if (exceeded) throw new SandboxError("SANDBOX_LIMIT_EXCEEDED", "Sandbox output exceeded maxOutputBytes")
    if (monitorFailure) throw monitorFailure
    if (timedOut) throw new SandboxError("SANDBOX_LIMIT_EXCEEDED", "Sandbox process exceeded timeoutMs")
    if (options.signal?.aborted) throw new SandboxError("SANDBOX_POLICY_DENIED", "Sandbox execution was cancelled")
    return { code, stdout, stderr }
  } finally {
    clearTimeout(timer)
    if (monitor) clearInterval(monitor)
    options.signal?.removeEventListener("abort", abort)
  }
}

async function checkWorkspaceBudget(root: string, maxBytes: number, maxEntries = 4096, maxDepth = 64) {
  let bytes = 0
  let entries = 0
  const visit = async (directory: string, depth = 0): Promise<void> => {
    if (depth > maxDepth) throw new SandboxError("SANDBOX_LIMIT_EXCEEDED", `Workspace depth exceeds ${maxDepth}`)
    for (const entry of await fs.readdir(directory, { withFileTypes: true })) {
      entries += 1
      if (entries > maxEntries) throw new SandboxError("SANDBOX_LIMIT_EXCEEDED", `Workspace entries exceed ${maxEntries}`)
      const target = path.join(directory, entry.name)
      const info = await fs.lstat(target)
      if (info.isDirectory()) await visit(target, depth + 1)
      else if (info.isFile()) bytes += info.size
      else if (!info.isSymbolicLink()) throw new SandboxError("SANDBOX_ESCAPE_ATTEMPT", "Unsupported workspace entry appeared")
      if (bytes > maxBytes) throw new SandboxError("SANDBOX_LIMIT_EXCEEDED", "Workspace output exceeds the aggregate byte limit")
    }
  }
  await visit(root)
}

async function checkProcessBudget(rootPid: number, maxRssKiB: number, maxProcesses: number) {
  const pending = [rootPid]
  const seen = new Set<number>()
  let rss = 0
  while (pending.length) {
    const pid = pending.pop()!
    if (seen.has(pid)) continue
    seen.add(pid)
    if (seen.size > maxProcesses) throw new SandboxError("SANDBOX_LIMIT_EXCEEDED", "Sandbox process tree exceeds the aggregate process limit")
    const status = await fs.readFile(`/proc/${pid}/status`, "utf8").catch(() => "")
    const match = /^VmRSS:\s+(\d+)\s+kB$/m.exec(status)
    if (match) rss += Number(match[1])
    if (rss > maxRssKiB) throw new SandboxError("SANDBOX_LIMIT_EXCEEDED", "Sandbox process tree exceeds the aggregate RSS limit")
    const children = await fs.readFile(`/proc/${pid}/task/${pid}/children`, "utf8").catch(() => "")
    pending.push(...children.trim().split(/\s+/).filter(Boolean).map(Number).filter(Number.isSafeInteger))
  }
}

export const runner = String.raw`
root=$1
workspace=$2
workdir=$3
seconds=$4
memory=$5
processes=$6
filesize=$7
command=$8
resource=$9
shift 9
inputs=$1
mount -t tmpfs -o mode=755 tmpfs "$root"
mkdir -p "$root/bin" "$root/usr" "$root/lib" "$root/lib64" "$root/etc" "$root/dev" "$root/proc" "$root/sys" "$root/tmp" "$root/workspace"
readonly_tree() {
  source=$1
  target=$2
  mount --rbind "$source" "$target"
  mount --make-rslave "$target"
  findmnt -Rrn -o TARGET "$target" | sort -r | while IFS= read -r mounted; do
    # findmnt --raw emits unsafe bytes as \xHH, including spaces and UTF-8.
    # Decode exactly once, never with eval. The sentinel preserves trailing LF.
    decoded=$(/usr/bin/printf '%b.' "$mounted")
    decoded=${"${decoded%.}"}
    mount -o remount,ro,bind -- "$decoded"
  done
}
for source in /bin /usr /lib /lib64; do
  [ -e "$source" ] || continue
  readonly_tree "$source" "$root$source"
done
for source in /etc/ld.so.cache /etc/localtime; do
  [ -f "$source" ] || continue
  target="$root$source"
  mkdir -p "$(dirname "$target")"
  : > "$target"
  mount --bind "$source" "$target"
  mount -o remount,ro,bind "$target"
done
printf 'root:x:0:0:root:/tmp/home:/bin/sh\n' > "$root/etc/passwd"
printf 'root:x:0:\n' > "$root/etc/group"
printf 'passwd: files\ngroup: files\nhosts: files dns\n' > "$root/etc/nsswitch.conf"
chmod 0444 "$root/etc/passwd" "$root/etc/group" "$root/etc/nsswitch.conf"
if [ -d /etc/ssl/certs ]; then
  mkdir -p "$root/etc/ssl/certs"
  readonly_tree /etc/ssl/certs "$root/etc/ssl/certs"
fi
mount -t tmpfs -o mode=755,size=16m tmpfs "$root/dev"
for source in /dev/null /dev/zero /dev/random /dev/urandom; do
  target="$root$source"
  : > "$target"
  mount --bind "$source" "$target"
  mount -o remount,ro,bind "$target"
done
mkdir -p "$root/dev/shm"
mount -t tmpfs -o mode=1777,size=16m tmpfs "$root/dev/shm"
ln -s /proc/self/fd "$root/dev/fd"
ln -s /proc/self/fd/0 "$root/dev/stdin"
ln -s /proc/self/fd/1 "$root/dev/stdout"
ln -s /proc/self/fd/2 "$root/dev/stderr"
mount --bind "$workspace" "$root/workspace"
mount -o remount,rw,bind "$root/workspace"
if [ -n "$inputs" ]; then
  while IFS= read -r relative; do
    [ -n "$relative" ] || continue
    source="$workspace/$relative"
    target="$root/workspace/$relative"
    [ -f "$source" ] && [ ! -L "$source" ] || exit 92
    mount --bind "$source" "$target"
    mount -o remount,ro,bind "$target"
  done < "$inputs"
fi
if [ -n "$resource" ]; then
  mkdir -p "$root/opt/harness-runtime"
  mount --bind "$resource" "$root/opt/harness-runtime"
  mount -o remount,ro,bind "$root/opt/harness-runtime"
fi
mount -t proc proc "$root/proc"
mount -t tmpfs -o mode=1777 tmpfs "$root/tmp"
mkdir -p "$root/tmp/home"
ip link set lo up
exec chroot "$root" /usr/bin/env -i PATH=/usr/local/bin:/usr/bin:/bin HOME=/tmp/home TMPDIR=/tmp LANG=C.UTF-8 LC_ALL=C.UTF-8 TERM=dumb /usr/bin/setpriv --no-new-privs --bounding-set=-all --inh-caps=-all --ambient-caps=-all /usr/bin/prlimit --as="$memory" --nproc="$processes" --nofile=256 --fsize="$filesize" --cpu="$seconds" /usr/bin/timeout --signal=TERM --kill-after=5s "$seconds"s /usr/bin/setsid /bin/sh -c 'cd "$1" && exec /bin/sh -lc "$2"' sandbox-command "$workdir" "$command"
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
      file = await fs.open(path.join(workspace, artifact.path), fsConstants.O_RDONLY | fsConstants.O_NOFOLLOW | fsConstants.O_NONBLOCK)
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

type WorkspaceScan = { files: Map<string, InputFile & { bytes?: Buffer; mtimeNs?: string }>; links: Map<string, string>; directories: Set<string> }
const statIdentity = (info: BigIntStats) => [info.dev, info.ino, info.mode, info.nlink, info.size, info.mtimeNs, info.ctimeNs].join(":")

/** Fresh bounded reads of the copy. Pre-execution state keeps hashes, not bodies. */
async function scanWorkspace(root: string, maxBytes: number, before?: WorkspaceScan, retain = new Set<string>()): Promise<WorkspaceScan> {
  const scan = async (root: string) => {
    const files: WorkspaceScan["files"] = new Map()
    const links = new Map<string, string>()
    const directories = new Set<string>()
    let bytes = 0
    let entries = 0
    const visit = async (directory: string, depth = 0): Promise<void> => {
      if (depth > 64) throw new SandboxError("SANDBOX_LIMIT_EXCEEDED", "Snapshot directory depth exceeds 64")
      const directoryBefore = await fs.lstat(directory, { bigint: true })
      if (!directoryBefore.isDirectory() || directoryBefore.isSymbolicLink()) throw new SandboxError("SANDBOX_ESCAPE_ATTEMPT", "Snapshot directory changed")
      for (const item of await fs.readdir(directory, { withFileTypes: true })) {
        entries += 1
        if (entries > 4096) throw new SandboxError("SANDBOX_LIMIT_EXCEEDED", "Snapshot file/directory count exceeds 4096")
        const target = path.join(directory, item.name)
        const relative = path.relative(root, target).split(path.sep).join("/")
        const stat = await fs.lstat(target, { bigint: true })
        if (stat.isDirectory()) { directories.add(relative); await visit(target, depth + 1) }
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
              if (before || retain.has(relative)) chunks.push(Buffer.from(buffer.subarray(0, bytesRead)))
            }
            const after = await handle.stat({ bigint: true })
            const named = await fs.lstat(target, { bigint: true })
            if (statIdentity(opened) !== statIdentity(after) || statIdentity(after) !== statIdentity(named)) {
              throw new SandboxError("EXECUTION_INPUT_MISMATCH", "Snapshot changed during reading")
            }
            const sha256 = digest.digest("hex")
            files.set(relative, { path: relative, sha256, size, executable: Boolean(stat.mode & 0o111n), mtimeNs: stat.mtimeNs.toString(),
              ...((before && before.files.get(relative)?.sha256 !== sha256) || retain.has(relative) ? { bytes: Buffer.concat(chunks) } : {}) })
          } finally { await handle.close() }
        } else throw new SandboxError("SANDBOX_ESCAPE_ATTEMPT", `Unsupported sandbox output entry: ${relative}`)
      }
      if (statIdentity(directoryBefore) !== statIdentity(await fs.lstat(directory, { bigint: true }))) {
        throw new SandboxError("EXECUTION_INPUT_MISMATCH", "Snapshot directory changed during reading")
      }
    }
    await visit(root)
    return { files, links, directories }
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

function scenarioPath(value: unknown): string {
  if (typeof value !== "string" || !value || value.length > 512 || value.includes("\\") || value.includes(":") ||
      value.includes("\0") || value.split("/").some(part => !part || part === "." || part === "..")) {
    throw new SandboxError("SANDBOX_POLICY_DENIED", "Scenario paths must be relative files")
  }
  return value
}

function validateScenario(request: SandboxRequest) {
  const fixtures = request.fixtures ?? []
  const observed = request.observedFiles ?? []
  const protectedFiles = request.protectedFiles ?? []
  if (!Array.isArray(fixtures) || fixtures.length > 32 || !Array.isArray(observed) || observed.length > 32 ||
      !Array.isArray(protectedFiles) || protectedFiles.length > 32) {
    throw new SandboxError("SANDBOX_POLICY_DENIED", "Scenario setup/observation exceeds bounds")
  }
  let total = 0
  const paths = new Set<string>()
  for (const fixture of fixtures) {
    if (!fixture || Object.keys(fixture).sort().join(",") !== "path,text" || typeof fixture.text !== "string") {
      throw new SandboxError("SANDBOX_POLICY_DENIED", "Invalid scenario fixture")
    }
    const name = scenarioPath(fixture.path)
    const bytes = Buffer.byteLength(fixture.text, "utf8")
    total += bytes
    if (bytes > 64000 || total > 128000 || paths.has(name.toLowerCase())) throw new SandboxError("SANDBOX_LIMIT_EXCEEDED", "Scenario fixture bounds exceeded")
    paths.add(name.toLowerCase())
  }
  observed.forEach(scenarioPath)
  if (new Set(observed.map(p => p.toLowerCase())).size !== observed.length) throw new SandboxError("SANDBOX_POLICY_DENIED", "Duplicate observed paths")
  protectedFiles.forEach(scenarioPath)
  if (new Set(protectedFiles.map(p => p.toLowerCase())).size !== protectedFiles.length ||
      protectedFiles.some(name => !fixtures.some(fixture => fixture.path === name)) ||
      protectedFiles.some(name => !observed.includes(name))) {
    throw new SandboxError("SANDBOX_POLICY_DENIED", "Protected files must be unique observed fixture paths")
  }
}

async function seedFixtures(workspace: string, before: WorkspaceScan, fixtures: NonNullable<SandboxRequest["fixtures"]>) {
  const reserved = [...before.files.keys(), ...before.links.keys()]
  for (const fixture of fixtures) {
    const name = fixture.path.toLowerCase()
    if (reserved.some(key => { const old = key.toLowerCase(); return old === name || old.startsWith(name + "/") || name.startsWith(old + "/") })) {
      throw new SandboxError("SANDBOX_POLICY_DENIED", "Scenario fixture overlaps a submitted input")
    }
    const target = path.join(workspace, fixture.path)
    await fs.mkdir(path.dirname(target), { recursive: true })
    await fs.writeFile(target, fixture.text, { encoding: "utf8", flag: "wx" })
    reserved.push(fixture.path)
  }
}

function observeFiles(before: WorkspaceScan, after: WorkspaceScan, paths: string[]) {
  let bytes = 0
  const state = (scan: WorkspaceScan, name: string): FileState => {
    const item = scan.files.get(name)
    return item ? { kind: "file", sha256: item.sha256, size: item.size, executable: item.executable, mtimeNs: item.mtimeNs }
      : { kind: scan.directories.has(name) ? "directory" : "absent" }
  }
  return paths.map(name => {
    const body = after.files.get(name)?.bytes
    const retained = body !== undefined && body.length <= 64000 && bytes + body.length <= 128000
    if (retained) bytes += body.length
    return { path: name, before: state(before, name), after: state(after, name),
      ...(retained ? { afterBase64: body.toString("base64"), contentStatus: "captured" }
        : body !== undefined ? { contentStatus: "limit_exceeded" } : {}) }
  })
}

function watchProtectedFiles(workspace: string, names: string[]) {
  const changed = new Set<string>()
  let failure: unknown
  let stopped = false
  const watchers = names.map(name => {
    const watcher = watchFileSystem(path.join(workspace, name), { persistent: false }, () => changed.add(name))
    watcher.on("error", error => { failure ??= error })
    return watcher
  })
  return async () => {
    if (!stopped) {
      stopped = true
      watchers.forEach(watcher => watcher.close())
    }
    if (failure) throw new SandboxError("EXECUTION_INPUT_MISMATCH", "Protected fixture observation failed")
    return [...changed].sort(byteOrder)
  }
}

async function checked(argv: string[], timeoutMs = 30_000) {
  const result = await execute(argv, { timeoutMs, maxOutputBytes: 1024 * 1024 })
  if (result.code !== 0) {
    throw new SandboxError("SANDBOX_UNAVAILABLE", result.stderr.trim() || `Sandbox setup failed: ${argv[0]}`)
  }
  return result.stdout.trim()
}

async function workspaceCwd(root: string, cwd: string) {
  const resolved = await fs.realpath(cwd).catch(() => {
    throw new SandboxError("SANDBOX_POLICY_DENIED", "Sandbox working directory does not exist")
  })
  const info = await fs.lstat(resolved)
  if (!info.isDirectory() || !inside(root, resolved)) {
    throw new SandboxError("SANDBOX_ESCAPE_ATTEMPT", "Sandbox working directory is outside workspace or is not a directory")
  }
  return path.relative(root, resolved)
}

async function inputManifest(base: string, binding?: ExecutionInputBinding) {
  const paths = binding?.files.map(item => item.path) ?? []
  if (!paths.length) return ""
  const target = path.join(base, "pinned-inputs")
  await fs.writeFile(target, paths.join("\n") + "\n", { encoding: "utf8", mode: 0o600, flag: "wx" })
  return target
}

async function wslRun(request: SandboxRequest, policy: SandboxPolicy, root: string, inputBytes: number) {
  const prefix = ["wsl.exe", "-d", policy.wslDistro, "--exec"]
  const probe = await checked([...prefix, "/bin/sh", "-lc", "command -v unshare prlimit timeout setsid setpriv findmnt mount chroot ip /usr/bin/printf >/dev/null && uname -r"])
  const home = await checked([...prefix, "/bin/sh", "-lc", 'printf %s "$HOME"'])
  const source = await checked([...prefix, "/usr/bin/wslpath", "-a", root])
  const id = safeId()
  const base = `${home}/.local/state/base-harness/sandboxes/${id}`
  const workspace = `${base}/workspace`
  const mountRoot = `${base}/root`
  const relative = (await workspaceCwd(root, request.cwd)).replaceAll("\\", "/")
  if (relative === ".." || relative.startsWith("../") || path.isAbsolute(relative)) {
    throw new SandboxError("SANDBOX_ESCAPE_ATTEMPT", "Sandbox working directory is outside workspace")
  }
  const workdir = relative ? `/workspace/${relative}` : "/workspace"
  const captured = request.captureChanges ? await fs.mkdtemp(path.join(os.tmpdir(), "base-harness-sandbox-capture-")) : undefined
  try {
    await checked([...prefix, "/bin/mkdir", "-p", workspace, mountRoot])
    await checked([...prefix, "/bin/cp", "-a", "--no-preserve=links", `${source}/.`, workspace], Math.max(30_000, policy.timeoutMs))
    const pinned = await inputManifest(base, request.inputBinding)
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
        "--ipc",
        "--uts",
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
        String(16 * 1024 * 1024),
        request.command,
        "",
        pinned,
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
  const relative = await workspaceCwd(root, request.cwd)
  if (relative === ".." || relative.startsWith(".." + path.sep) || path.isAbsolute(relative)) {
    throw new SandboxError("SANDBOX_ESCAPE_ATTEMPT", "Sandbox working directory is outside workspace")
  }
  await fs.mkdir(mountRoot, { recursive: true })
  let stopObserving = async () => {}
  let stopProtecting = async (): Promise<string[]> => []
  const began = performance.now()
  const timingMs = { setup: 0, execution: 0, observation: 0, cleanup: 0 }
  const elapsed = (start: number) => Math.round((performance.now() - start) * 1000) / 1000
  try {
    await fs.cp(root, workspace, { recursive: true, dereference: false, verbatimSymlinks: true })
    const scenario = Boolean(request.fixtures?.length || request.observedFiles?.length || request.protectedFiles?.length)
    const original = request.captureChanges || request.inputBinding || scenario ? await scanWorkspace(workspace, policy.maxInputBytes) : undefined
    if (request.inputBinding) admitInputs(original!, request.inputBinding)
    if (request.fixtures?.length) await seedFixtures(workspace, original!, request.fixtures)
    const pinned = await inputManifest(base, request.inputBinding)
    const before = request.fixtures?.length ? await scanWorkspace(workspace, policy.maxInputBytes) : original
    if (request.inputBinding && request.fixtures?.length && !inputReceipt(original!, before!, request.inputBinding, new Date().toISOString()).unchanged) {
      throw new SandboxError("EXECUTION_INPUT_MISMATCH", "Scenario setup changed submitted inputs")
    }
    const checkedAt = new Date().toISOString()
    const seconds = Math.max(1, Math.ceil(policy.timeoutMs / 1000))
    // No command or case event runs before admitting the actual copied tree.
    stopObserving = watchArtifact(workspace, request.observationArtifact)
    stopProtecting = watchProtectedFiles(workspace, request.protectedFiles ?? [])
    timingMs.setup = elapsed(began)
    const executionStarted = performance.now()
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
        "--ipc",
        "--uts",
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
        String(16 * 1024 * 1024),
        request.command,
        request.readonlyResource ?? "",
        pinned,
      ],
      { timeoutMs: policy.timeoutMs, maxOutputBytes: policy.maxOutputBytes, signal: request.signal,
        monitor: async (pid) => {
          await Promise.all([
            checkWorkspaceBudget(workspace, Math.max(policy.maxInputBytes * 4, 32 * 1024 * 1024)),
            checkProcessBudget(pid, policy.memoryMiB * 1024, policy.maxProcesses),
          ])
        } },
    )
    timingMs.execution = elapsed(executionStarted)
    const observationStarted = performance.now()
    const after = before ? await scanWorkspace(workspace, policy.maxInputBytes, request.captureChanges ? before : undefined, new Set(request.observedFiles ?? [])) : undefined
    const changes = request.captureChanges ? captureWorkspaceChanges(before!, after!) : undefined
    const receipt = request.inputBinding ? inputReceipt(original!, after!, request.inputBinding, checkedAt) : undefined
    const fileObservations = request.observedFiles ? observeFiles(before!, after!, request.observedFiles) : undefined
    const protectedChanges = await stopProtecting()
    stopProtecting = async () => protectedChanges
    timingMs.observation = elapsed(observationStarted)
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
        timingMs,
      },
      ...(changes ? { changes } : {}),
      ...(receipt ? { inputReceipt: receipt } : {}),
      ...(fileObservations ? { fileObservations } : {}),
      ...(protectedChanges.length ? { protectedChanges } : {}),
    }
  } finally {
    const cleanupStarted = performance.now()
    try { await stopObserving() } finally {
      try { await stopProtecting() } finally {
        await fs.rm(base, { recursive: true, force: true })
        timingMs.cleanup = elapsed(cleanupStarted)
      }
    }
  }
}

export class SandboxManager {
  async run(request: SandboxRequest): Promise<SandboxResult> {
    validateScenario(request)
    if (request.command.includes("\0")) throw new SandboxError("SANDBOX_POLICY_DENIED", "Sandbox command contains NUL")
    const policy = isolationPolicy(request.config)
    if (request.inputBinding !== undefined) request = { ...request, inputBinding: validateInputBinding(request.inputBinding) }
    const input = await validateSandboxWorkspace(request.workspace, policy.maxInputBytes)
    request.signal?.throwIfAborted()
    const backend = policy.strictBackend === "auto" ? (process.platform === "win32" ? "wsl2" : "namespace") : policy.strictBackend
    if (backend === "wsl2") {
      if (request.inputBinding) throw new SandboxError("SANDBOX_UNAVAILABLE", "Pinned execution input verification requires the namespace backend")
      if (request.readonlyResource || request.observationArtifact || request.fixtures || request.observedFiles || request.protectedFiles) throw new SandboxError("SANDBOX_UNAVAILABLE", "Structured adapter resources require the namespace backend")
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
