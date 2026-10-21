"""Bounded local artifact I/O; locks are mechanics, not a second Run database."""
from contextlib import contextmanager
import os
from pathlib import Path
import tempfile

from harness.common import canonical_bytes
from harness.json_codec import decode
from .errors import require
from .snapshots import no_links


def read_bytes(path, maximum):
    path = Path(path)
    no_links(path)
    with path.open("rb") as stream:
        body = stream.read(maximum + 1)
    require(len(body) <= maximum, "ARTIFACT_LIMIT", "Stored artifact exceeds its byte limit")
    return body


def read_json(path, maximum=65536):
    return decode(read_bytes(path, maximum).decode("utf-8"))


def atomic_write(path, body):
    path = Path(path)
    no_links(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".publish-", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        no_links(path)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_json(path, value):
    atomic_write(path, canonical_bytes(value) + b"\n")


@contextmanager
def locked(root, name):
    """Use a separate descriptor per caller, so threads and processes serialize."""
    root = Path(root)
    require(isinstance(name, str) and name and all(c.isalnum() or c in "-_." for c in name),
            "ARTIFACT_LOCK", "Invalid artifact lock name")
    target = root / ".locks" / (name + ".lock")
    no_links(target)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(target, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "r+b") as stream:
        if os.name == "nt":
            import msvcrt
            if not os.fstat(stream.fileno()).st_size:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
