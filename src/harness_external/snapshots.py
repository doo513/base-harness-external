"""Explicit-file candidate snapshots using shared bounded streaming I/O."""
from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path
import shutil
import stat
import uuid

from harness.json_codec import MAX_BYTES
from harness.common import atomic_json, canonical_bytes, canonical_hash
from harness.snapshot_io import iter_snapshot_chunks
from .errors import HarnessError, relative_path, require


def no_links(path: Path) -> None:
    for item in (path, *path.parents):
        if item.exists() or item.is_symlink():
            info = item.lstat()
            require(not stat.S_ISLNK(info.st_mode) and not (getattr(info, "st_file_attributes", 0) & 0x400),
                    "LINK_UNSUPPORTED", "Symlinks and Windows reparse points are not supported")


def stream_file(root: Path, relative: str, sink=None) -> tuple[str, int, bool]:
    relative_path(relative)
    target = root / relative
    no_links(target)
    before = target.stat(follow_symlinks=False)
    digest, size = hashlib.sha256(), 0
    for data in iter_snapshot_chunks(root, relative):
        digest.update(data)
        size += len(data)
        if sink is not None:
            sink.write(data)
    after = target.stat(follow_symlinks=False)
    identity = lambda s: (s.st_dev, s.st_ino, s.st_mode, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    require(identity(before) == identity(after), "INPUT_CHANGED", "Input changed while capturing")
    return digest.hexdigest(), size, bool(before.st_mode & 0o111)


def read_file(root: Path, relative: str) -> tuple[bytes, bool]:
    stream = io.BytesIO()
    _, _, executable = stream_file(root, relative, stream)
    return stream.getvalue(), executable


def capture(workspace: Path, inputs: list[str], directory: Path, *, overlays: dict[str, Path] | None = None) -> dict:
    overlays = overlays or {}
    require(set(overlays) <= set(inputs), "SNAPSHOT_SCOPE", "Pinned overlay files must be declared inputs")
    candidate_id = "candidate_" + uuid.uuid4().hex
    temporary = directory / (".capture_" + uuid.uuid4().hex)
    payload = temporary / "payload"
    payload.mkdir(parents=True, mode=0o700)
    entries, executable = [], {}
    total = 0
    try:
        for relative in inputs:
            target = payload / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as stream:
                digest, size, execute = stream_file(overlays.get(relative, workspace), relative, stream)
                stream.flush()
                os.fsync(stream.fileno())
            target.chmod(0o555 if execute else 0o444)
            total += size
            require(total <= MAX_BYTES, "SNAPSHOT_TOO_LARGE", "Combined input exceeds the v5 10 MiB limit")
            entries.append({"path": relative, "sha256": digest, "size": size})
            executable[relative] = execute
        # Detect ordinary concurrent editor changes before sealing the copied set.
        # This is not a filesystem-wide atomic snapshot or a hostile-user barrier.
        for entry in entries:
            current, _, mode = stream_file(overlays.get(entry["path"], workspace), entry["path"])
            require(current == entry["sha256"] and mode == executable[entry["path"]],
                    "INPUT_CHANGED", "Workspace changed during snapshot capture; submit again with a new request ID")
        manifest = {"kind": "candidate", "files": entries, "dependencies": []}
        manifest_json = canonical_bytes(manifest).decode("utf-8")
        result = {"candidate_id": candidate_id, "manifest": manifest, "executable": executable,
                  "subject": {"kind": "candidate", "id": candidate_id, "revision": 1, "sha256": hashlib.sha256(manifest_json.encode()).hexdigest()},
                  "candidate_hash": canonical_hash({"manifest": manifest, "executable": executable})}
        atomic_json(temporary / "candidate.json", result)
        validate_candidate(result, payload)
        os.rename(temporary, directory / candidate_id)
        return result
    except Exception:
        # Only our newly allocated capture directory is eligible for cleanup.
        def writable_remove(fn, path, exc):
            os.chmod(path, 0o700)
            fn(path)
        shutil.rmtree(temporary, onerror=writable_remove)
        raise


def validate_candidate(candidate: dict, payload: Path) -> None:
    require(candidate["candidate_hash"] == canonical_hash({"manifest": candidate["manifest"], "executable": candidate["executable"]}),
            "CANDIDATE_CORRUPT", "Candidate metadata changed")
    require(candidate["subject"]["sha256"] == canonical_hash(candidate["manifest"]), "CANDIDATE_CORRUPT", "Subject manifest changed")
    listed = {entry["path"] for entry in candidate["manifest"]["files"]}
    found = set()
    for parent, dirs, files in os.walk(payload, followlinks=False):
        no_links(Path(parent))
        for name in dirs:
            no_links(Path(parent) / name)
        for name in files:
            found.add((Path(parent) / name).relative_to(payload).as_posix())
    require(found == listed and set(candidate["executable"]) == listed, "CANDIDATE_CORRUPT", "Snapshot file set changed")
    total = 0
    for entry in candidate["manifest"]["files"]:
        digest, size, executable = stream_file(payload, entry["path"])
        total += size
        require(total <= MAX_BYTES and size == entry["size"] and digest == entry["sha256"]
                and executable == candidate["executable"][entry["path"]], "CANDIDATE_CORRUPT", "Snapshot bytes or executable mode changed")
