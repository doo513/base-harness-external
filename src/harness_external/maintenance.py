"""Recover abandoned capture staging using OS locks; never remove user files."""
from contextlib import contextmanager
import os
from pathlib import Path
import re
import time
import uuid

from .errors import integer, require
from .snapshots import no_links


@contextmanager
def capture_lease(directory):
    # Recovery is supported on POSIX. Windows captures still work, but cleanup
    # reports unsupported rather than guessing whether another process is alive.
    if os.name != "posix":
        yield
        return
    import fcntl
    with (Path(directory) / ".capture.lock").open("xb") as lease:
        fcntl.flock(lease, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lease, fcntl.LOCK_UN)


def cleanup(store, run_id, *, apply=False, min_age_seconds=3600):
    integer(min_age_seconds, 60, 365 * 86400, "min_age_seconds")
    require(type(apply) is bool, "INVALID_PARAMETERS", "apply must be boolean")
    with store.transaction() as connection:
        store.run(connection, run_id)
    directory = store.directory(run_id)
    if os.name != "posix":
        return {"run_id": run_id, "supported": False, "reason": "POSIX capture locks required", "items": []}
    import fcntl
    items = []
    for path in sorted(directory.iterdir()):
        if not re.fullmatch(r"\.submit_[a-z0-9_]{8}", path.name):
            continue
        try:
            no_links(path)
            lock = path / ".capture.lock"
            no_links(lock)
            if not path.is_dir() or not lock.is_file():
                items.append({"path": str(path), "state": "unknown_ownership"})
                continue
            if time.time() - path.stat().st_mtime < min_age_seconds:
                items.append({"path": str(path), "state": "recent"})
                continue
            with lock.open("rb") as lease:
                try:
                    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    items.append({"path": str(path), "state": "active"})
                    continue
                # Another recovery may have renamed this directory while we
                # opened it. Only operate on the exact locked directory entry.
                if not lock.exists() or os.fstat(lease.fileno()).st_ino != lock.stat().st_ino:
                    continue
                item = {"path": str(path), "state": "abandoned"}
                if apply:
                    target = directory / (".quarantine_" + uuid.uuid4().hex)
                    os.rename(path, target)
                    item.update(state="quarantined", recovery_path=str(target))
                items.append(item)
        except FileNotFoundError:
            continue
    return {"run_id": run_id, "supported": True, "applied": apply, "items": items,
            "recovery": "Quarantined captures are retained on disk; no published candidate or workspace is changed."}
