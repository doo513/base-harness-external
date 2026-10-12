"""Bounded snapshot I/O shared by capture and measurement, without either policy.

Consumers must exhaust the iterator before admitting its bytes: final identity
checks run after the last chunk. Closing early releases every open descriptor.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat
import sys

from .json_codec import MAX_BYTES

CHUNK_BYTES = 64 * 1024


def path_parts(relative: str) -> list[str]:
    if not isinstance(relative, str) or "\\" in relative or "\x00" in relative:
        raise ValueError("invalid snapshot path")
    parts = relative.split("/")
    if any(part in {"", ".", ".."} or ":" in part for part in parts):
        raise ValueError("invalid snapshot path")
    return parts


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _bounded(info, limit):
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
        raise OSError("snapshot must be a bounded single-link regular file")


def _chunks(stream, limit, chunk_bytes):
    total = 0
    while data := stream.read(min(chunk_bytes, limit - total + 1)):
        total += len(data)
        if total > limit:
            raise OSError("snapshot exceeds measurement limit")
        yield data


def iter_snapshot_chunks(root: Path, relative: str, *, limit=MAX_BYTES, chunk_bytes=CHUNK_BYTES):
    """Read by pinned descriptors, reject links, and recheck identity on exhaustion."""
    if type(limit) is not int or limit < 0 or type(chunk_bytes) is not int or chunk_bytes <= 0:
        raise ValueError("invalid snapshot read bounds")
    parts = path_parts(relative)
    if sys.platform == "win32":
        # Preserve fail-closed reparse handling; this is not a Windows sandbox.
        resolved_root = root.resolve(strict=True)
        target = root / Path(*parts)
        for item in (target, *target.parents):
            if item.exists() or item.is_symlink():
                info = item.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise OSError("symlink/reparse snapshot not permitted")
        try:
            target.resolve(strict=True).relative_to(resolved_root)
        except ValueError as error:
            raise OSError("snapshot must remain within root") from error
        before = target.stat(follow_symlinks=False)
        _bounded(before, limit)
        with target.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if _identity(before) != _identity(opened):
                raise OSError("snapshot changed during measurement")
            yield from _chunks(stream, limit, chunk_bytes)
            after = os.fstat(stream.fileno())
        current = target.stat(follow_symlinks=False)
        if _identity(before) != _identity(after) or _identity(after) != _identity(current):
            raise OSError("snapshot changed during measurement")
        return
    if os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"):
        raise OSError("SNAPSHOT_CONTAINMENT_UNSUPPORTED")
    descriptors = []
    try:
        parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(parent)
        for part in parts[:-1]:
            parent = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            descriptors.append(parent)
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            _bounded(before, limit)
            yield from _chunks(stream, limit, chunk_bytes)
            after = os.fstat(stream.fileno())
        current = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
        if _identity(before) != _identity(after) or _identity(after) != _identity(current):
            raise OSError("snapshot changed during measurement")
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def read_snapshot(root: Path, relative: str) -> bytes:
    """Compatibility read; streaming consumers should use iter_snapshot_chunks."""
    return b"".join(iter_snapshot_chunks(root, relative))
