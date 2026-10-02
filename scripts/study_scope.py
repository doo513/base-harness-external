"""Evaluation-only mount scope; never imported by the product/runtime."""
import os
import json
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time


def mount(*args):
    subprocess.run(["mount", *map(str, args)], check=True)


def inside(root, trial, original_repo, stash, command):
    root, trial, original_repo, stash = map(Path, (root, trial, original_repo, stash))
    mount("--make-rprivate", "/")
    mount("--bind", trial, stash)
    mount("--bind", root, root)
    mount("-o", "remount,bind,ro", root)
    for name in ("private", "receipts"):
        mount("-t", "tmpfs", "-o", "mode=000,ro", "tmpfs", root / name)
    mount("-t", "tmpfs", "-o", "mode=755", "tmpfs", root / "trials")
    trial.mkdir(parents=True)
    mount("--bind", stash, trial)
    mount("-o", "remount,bind,ro", root / "trials")
    for name in ("client.json", "task-parameters.json"):
        path = trial / name
        if path.exists():
            mount("--bind", path, path)
            mount("-o", "remount,bind,ro", path)
    # Models use the frozen public runtime copies under sources/, not this
    # working checkout which also contains private evaluator positive controls.
    mount("-t", "tmpfs", "-o", "mode=000,ro", "tmpfs", original_repo)
    os.chdir(trial / "workspace")
    os.execvp("setpriv", ["setpriv", "--inh-caps=-all", "--ambient-caps=-all", "--no-new-privs", *command])


def identity(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        return fields[19]  # /proc stat field 22: kernel process start time
    except (FileNotFoundError, ProcessLookupError):
        return None


def namespace_children(pid):
    """Find only PID-namespace init processes descended from this owned child."""
    found, pending = {}, [pid]
    while pending:
        parent = pending.pop()
        try:
            children = Path(f"/proc/{parent}/task/{parent}/children").read_text().split()
        except (FileNotFoundError, ProcessLookupError):
            continue
        for value in children:
            child = int(value)
            pending.append(child)
            try:
                status = Path(f"/proc/{child}/status").read_text()
                ns = next(line.split()[1:] for line in status.splitlines() if line.startswith("NSpid:"))
                token = identity(child)
                if len(ns) > 1 and ns[-1] == "1" and token is not None:
                    found[child] = token
            except (FileNotFoundError, ProcessLookupError, StopIteration):
                continue
    return found


def terminate_owned(namespaces):
    stopped = []
    for pid, token in namespaces.items():
        if identity(pid) == token:
            try:
                os.kill(pid, signal.SIGKILL)
                stopped.append(pid)
            except ProcessLookupError:
                pass
    return stopped


def supervise(command):
    requested = False
    owned = {}
    def stop(_signal, _frame):
        nonlocal requested
        requested = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    child = subprocess.Popen(command)
    try:
        while child.poll() is None and not requested:
            owned.update(namespace_children(child.pid))
            time.sleep(0.1)
        # A reaped wrapper is NOT proof that its namespace init or a process in
        # another process group stopped. Kill only recorded, still-matching
        # namespace owners; kernel PID-namespace teardown includes their workers.
        owned.update(namespace_children(child.pid))
        stopped = terminate_owned(owned)
        if requested and child.poll() is None:
            child.kill()
        child.wait(timeout=3)
        deadline = time.monotonic() + 2
        while any(identity(pid) == token for pid, token in owned.items()) and time.monotonic() < deadline:
            time.sleep(0.02)
        remaining = [pid for pid, token in owned.items() if identity(pid) == token]
        print(json.dumps({"type": "study.scope.exit", "termination_requested": requested,
                          "terminated_namespace_pids": stopped, "remaining_namespace_pids": remaining}), flush=True)
        return 124 if requested else 125 if remaining else child.returncode
    finally:
        terminate_owned(owned)


if __name__ == "__main__":
    if sys.argv[1] == "--inside":
        inside(*sys.argv[2:6], sys.argv[6:])
    else:
        root, trial, repository, *command = sys.argv[1:]
        with tempfile.TemporaryDirectory(prefix="harness-study-scope-") as stash:
            result = supervise(["unshare", "--user", "--map-current-user", "--keep-caps", "--mount", "--pid", "--fork",
                                     "--kill-child=KILL", "--mount-proc", sys.executable, str(Path(__file__).absolute()),
                                     "--inside", root, trial, repository, stash, *command])
        raise SystemExit(result)
