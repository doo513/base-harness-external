import { afterEach, expect, test } from "bun:test"
import { mkdir, mkdtemp, readFile, readlink, rm, symlink, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"
import {
  SandboxError,
  SandboxManager,
  isolationPolicy,
  validateSandboxWorkspace,
} from "../runtime/packages/security/src/sandbox"

const roots: string[] = []
afterEach(async () => {
  await Promise.all(roots.splice(0).map((root) => rm(root, { recursive: true, force: true })))
})

test("strict isolation defaults are deterministic", () => {
  expect(isolationPolicy()).toEqual({
    strictBackend: "auto",
    wslDistro: "Ubuntu-24.04",
    timeoutMs: 120_000,
    memoryMiB: 1024,
    maxProcesses: 32,
    maxOutputBytes: 10 * 1024 * 1024,
    maxInputBytes: 2 * 1024 * 1024 * 1024,
  })
})

test("workspace validation enforces input limits and Windows path policy", async () => {
  const workspace = await mkdtemp(join(tmpdir(), "base-harness-sandbox-validation-"))
  roots.push(workspace)
  await writeFile(join(workspace, "large.txt"), "12345", "utf8")

  await expect(validateSandboxWorkspace(workspace, 4)).rejects.toMatchObject({
    code: "SANDBOX_LIMIT_EXCEEDED",
  })
  if (process.platform === "win32") {
    await expect(validateSandboxWorkspace("\\\\server\\share", 1024)).rejects.toMatchObject({
      code: "SANDBOX_ESCAPE_ATTEMPT",
    })
  }
})

test("workspace validation rejects links that resolve outside the declared root", async () => {
  const workspace = await mkdtemp(join(tmpdir(), "base-harness-sandbox-link-root-"))
  const outside = await mkdtemp(join(tmpdir(), "base-harness-sandbox-link-outside-"))
  roots.push(workspace, outside)
  await writeFile(join(outside, "outside.txt"), "outside", "utf8")
  await symlink(outside, join(workspace, "escape"), process.platform === "win32" ? "junction" : "dir")

  await expect(validateSandboxWorkspace(workspace, 1024 * 1024)).rejects.toMatchObject({
    code: "SANDBOX_ESCAPE_ATTEMPT",
  })
})

test.skipIf(process.env.BASE_HARNESS_EXTERNAL_LIVE !== "1")(
  "namespace runner treats metacharacters in cwd as a directory name",
  async () => {
    const workspace = await mkdtemp(join(tmpdir(), "base-harness-sandbox-cwd-"))
    roots.push(workspace)
    const cwd = join(workspace, 'x"; printf CWD_INJECTED; #')
    await mkdir(cwd)
    await writeFile(join(cwd, "input.txt"), "ready", "utf8")
    const result = await new SandboxManager().run({
      workspace,
      cwd,
      command: "printf ORIGINAL",
      config: { strictBackend: "namespace", timeoutMs: 10_000, maxInputBytes: 1024 * 1024, maxOutputBytes: 1024 },
    })
    expect(result.exitCode).toBe(0)
    expect(result.stdout).toBe("ORIGINAL")
    expect(result.stdout).not.toContain("CWD_INJECTED")
  },
)

test.skipIf(process.env.BASE_HARNESS_EXTERNAL_LIVE !== "1")(
  "namespace runner drops mount capabilities and rejects FIFO observations",
  async () => {
    const workspace = await mkdtemp(join(tmpdir(), "base-harness-sandbox-caps-"))
    roots.push(workspace)
    await writeFile(join(workspace, "input.txt"), "ready", "utf8")
    const result = await new SandboxManager().run({
      workspace,
      cwd: workspace,
      command: "grep -q '^NoNewPrivs:[[:space:]]*1$' /proc/self/status && grep -q '^CapEff:[[:space:]]*0000000000000000$' /proc/self/status && ! mount -o remount,rw /usr 2>/dev/null && mkfifo events && sleep 0.2",
      observationArtifact: { path: "events", maxBytes: 1024, onLine: () => {} },
      config: { strictBackend: "namespace", timeoutMs: 10_000, maxInputBytes: 1024 * 1024, maxOutputBytes: 1024 },
    }).catch(error => error)
    expect(result).toBeInstanceOf(SandboxError)
    expect(["SANDBOX_ESCAPE_ATTEMPT", "SANDBOX_LIMIT_EXCEEDED"]).toContain(result.code)
  },
)

test.skipIf(process.env.BASE_HARNESS_EXTERNAL_LIVE !== "1")(
  "namespace runner isolates IPC and exposes only the minimal system view",
  async () => {
    const workspace = await mkdtemp(join(tmpdir(), "base-harness-sandbox-system-"))
    roots.push(workspace)
    await writeFile(join(workspace, "input.txt"), "ready", "utf8")
    const hostIpc = await readlink("/proc/self/ns/ipc")
    const result = await new SandboxManager().run({
      workspace,
      cwd: workspace,
      command: "readlink /proc/self/ns/ipc; test ! -e /etc/machine-id; test \"$(wc -l < /etc/passwd)\" -eq 1; grep -q '^root:x:0:0:' /etc/passwd; test -z \"$(find /sys -mindepth 1 -maxdepth 1 -print -quit)\"; findmnt -n -o FSTYPE /dev/shm",
      config: { strictBackend: "namespace", timeoutMs: 10_000, maxInputBytes: 1024 * 1024, maxOutputBytes: 1024 },
    })
    expect(result.exitCode).toBe(0)
    const lines = result.stdout.trim().split("\n")
    expect(lines[0]).not.toBe(hostIpc.trim())
    expect(lines[1]).toBe("tmpfs")
  },
)

test.skipIf(process.env.BASE_HARNESS_EXTERNAL_LIVE !== "1")(
  "namespace monitor stops aggregate workspace and process exhaustion",
  async () => {
    const workspace = await mkdtemp(join(tmpdir(), "base-harness-sandbox-budget-"))
    roots.push(workspace)
    await writeFile(join(workspace, "input.txt"), "ready", "utf8")
    const entries = await new SandboxManager().run({
      workspace,
      cwd: workspace,
      command: "i=0; while [ $i -lt 5000 ]; do : > output-$i; i=$((i+1)); done; sleep 5",
      config: { strictBackend: "namespace", timeoutMs: 10_000, maxInputBytes: 1024 * 1024, maxOutputBytes: 1024, memoryMiB: 128, maxProcesses: 8 },
    }).catch(error => error)
    expect(entries).toBeInstanceOf(SandboxError)
    expect(entries.code).toBe("SANDBOX_LIMIT_EXCEEDED")
    expect(entries.message).toContain("Workspace entries")

    const memory = await new SandboxManager().run({
      workspace,
      cwd: workspace,
      command: "python3 -c 'import subprocess,sys; code=\"x=bytearray(80*1024*1024); import time; time.sleep(10)\"; ps=[subprocess.Popen([sys.executable,\"-c\",code]) for _ in range(4)]; [p.wait() for p in ps]'",
      config: { strictBackend: "namespace", timeoutMs: 15_000, maxInputBytes: 1024 * 1024, maxOutputBytes: 1024, memoryMiB: 256, maxProcesses: 16 },
    }).catch(error => error)
    expect(memory).toBeInstanceOf(SandboxError)
    expect(memory.code).toBe("SANDBOX_LIMIT_EXCEEDED")
    expect(memory.message).toContain("aggregate RSS")
  },
  30_000,
)

test.skipIf(process.platform !== "win32")(
  "WSL2 strict sandbox isolates filesystem and external network namespace",
  async () => {
    const workspace = await mkdtemp(join(tmpdir(), "base-harness-sandbox-wsl-"))
    roots.push(workspace)
    const target = join(workspace, "host.txt")
    await writeFile(target, "host-original", "utf8")

    const result = await new SandboxManager().run({
      workspace,
      cwd: workspace,
      command:
        'printf sandbox-ok; printf sandbox-copy > host.txt; test "$(find /sys/class/net -mindepth 1 -maxdepth 1 | wc -l)" -eq 1; if printf denied > /usr/bin/base-harness-probe 2>/dev/null; then exit 91; fi',
      config: {
        strictBackend: "wsl2",
        wslDistro: "Ubuntu-24.04",
        timeoutMs: 30_000,
        maxOutputBytes: 1024 * 1024,
        maxInputBytes: 16 * 1024 * 1024,
      },
    })

    expect(result.exitCode).toBe(0)
    expect(result.stdout).toContain("sandbox-ok")
    expect(result.provenance.backend).toBe("wsl2")
    expect(result.provenance.network).toBe("loopback_only")
    expect(await readFile(target, "utf8")).toBe("host-original")
  },
  60_000,
)

test.skipIf(process.platform !== "win32")("missing WSL distro fails closed", async () => {
  const workspace = await mkdtemp(join(tmpdir(), "base-harness-sandbox-missing-"))
  roots.push(workspace)
  await writeFile(join(workspace, "input.txt"), "input", "utf8")

  const error = await new SandboxManager()
    .run({
      workspace,
      cwd: workspace,
      command: "true",
      config: { strictBackend: "wsl2", wslDistro: "base-harness-missing-distro", timeoutMs: 5_000 },
    })
    .catch((value) => value)
  expect(error).toBeInstanceOf(SandboxError)
  expect(error.code).toBe("SANDBOX_UNAVAILABLE")
})

test.skipIf(process.platform !== "win32")(
  "timeout tears down the sandbox PID namespace and descendants",
  async () => {
    const workspace = await mkdtemp(join(tmpdir(), "base-harness-sandbox-timeout-"))
    roots.push(workspace)
    await writeFile(join(workspace, "input.txt"), "input", "utf8")
    const marker = `base-harness-child-${Date.now()}`
    const started = Date.now()
    const result = await new SandboxManager().run({
      workspace,
      cwd: workspace,
      command: `( /bin/bash -c 'exec -a ${marker} sleep 30' ) & wait`,
      config: {
        strictBackend: "wsl2",
        wslDistro: "Ubuntu-24.04",
        timeoutMs: 1_000,
        maxOutputBytes: 1024 * 1024,
        maxInputBytes: 16 * 1024 * 1024,
      },
    })
    expect(result.exitCode).toBe(124)
    expect(Date.now() - started).toBeLessThan(10_000)

    const probe = Bun.spawn(
      ["wsl.exe", "-d", "Ubuntu-24.04", "--exec", "/usr/bin/pgrep", "-f", marker],
      { stdout: "ignore", stderr: "ignore" },
    )
    expect(await probe.exited).not.toBe(0)
  },
  60_000,
)
