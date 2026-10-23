"""Bounded file discovery: never import or execute the target project."""
import hashlib
import io
from itertools import islice
import os
from pathlib import Path
import stat
import tokenize

EXCLUDED = frozenset({".git", ".hg", ".svn", ".venv", "venv", "env", "__pycache__", ".pytest_cache",
                      ".mypy_cache", ".ruff_cache", ".tox", "node_modules", "build", "dist", ".eggs"})


class AnalysisError(ValueError):
    pass


def regular_bytes(path, maximum):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise AnalysisError("Only single-link regular files are supported")
        if before.st_size > maximum:
            raise AnalysisError("File size limit exceeded")
        raw = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
    fingerprint = lambda s: (s.st_dev, s.st_ino, s.st_mode, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    if len(raw) > maximum or fingerprint(before) != fingerprint(after):
        raise AnalysisError("File changed while reading or exceeds the limit")
    return raw


def scan(root, *, max_files=5000, max_file_bytes=2_000_000, max_total_bytes=32_000_000, cache=None):
    root = Path(root).expanduser().absolute()
    if not root.is_dir():
        raise AnalysisError("Project path must be an existing directory")
    if any(p.is_symlink() or getattr(p.lstat(), "st_file_attributes", 0) & 0x400 for p in (root, *root.parents)):
        raise AnalysisError("Project root may not contain symbolic links or reparse points")
    if any(type(v) is not int or v <= 0 for v in (max_files, max_file_bytes, max_total_bytes)):
        raise AnalysisError("Limits must be positive integers")
    files, diagnostics, directories = [], [], set()
    total = count = entries = bytes_read = 0
    pending = [(root, 0)]
    stopped = False
    while pending and not stopped:
        directory, depth = pending.pop()
        try:
            with os.scandir(directory) as stream:
                children = list(islice(stream, 100000 - entries + 1))
            if len(children) > 100000 - entries:
                diagnostics.append({"path": directory.relative_to(root).as_posix(), "code": "ENTRY_LIMIT"})
                stopped = True
                break
            children.sort(key=lambda e: e.name)
            for entry in children:
                entries += 1
                if entries > 100000:
                    diagnostics.append({"path": ".", "code": "ENTRY_LIMIT"})
                    stopped = True
                    break
                path = Path(entry.path)
                relative = path.relative_to(root).as_posix()
                try:
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                        diagnostics.append({"path": relative, "code": "LINK_SKIPPED"})
                    elif stat.S_ISDIR(info.st_mode):
                        if entry.name not in EXCLUDED and not entry.name.endswith(".egg-info"):
                            if depth >= 64:
                                diagnostics.append({"path": relative, "code": "DEPTH_LIMIT"})
                            else:
                                directories.add(relative)
                                pending.append((path, depth + 1))
                    elif path.suffix == ".py":
                        if count >= max_files:
                            diagnostics.append({"path": relative, "code": "FILE_COUNT_LIMIT"})
                            stopped = True
                            break
                        count += 1
                        remaining = max_total_bytes - total
                        if remaining <= 0:
                            diagnostics.append({"path": relative, "code": "TOTAL_BYTE_LIMIT"})
                            stopped = True
                            break
                        if cache is not None and stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= min(max_file_bytes, remaining):
                            parsed = cache.get(relative, info)
                            if parsed is not None:
                                total += info.st_size
                                files.append({"path": relative, "size": info.st_size, "sha256": parsed[0]["sha256"],
                                              "cached_parse": parsed, "fingerprint": [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_mode, info.st_nlink]})
                                continue
                        raw = regular_bytes(path, min(max_file_bytes, remaining))
                        total += len(raw)
                        bytes_read += len(raw)
                        encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
                        sha256 = hashlib.sha256(raw).hexdigest()
                        files.append({"path": relative, "text": raw.decode(encoding), "size": len(raw), "sha256": sha256,
                                      "cached_parse": cache.content(relative, sha256) if cache is not None else None,
                                      "fingerprint": [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_mode, info.st_nlink]})
                except (OSError, ValueError, SyntaxError, LookupError) as error:
                    diagnostics.append({"path": relative, "code": "FILE_UNREADABLE", "message": str(error)[:300]})
        except OSError as error:
            diagnostics.append({"path": directory.relative_to(root).as_posix(), "code": "DIRECTORY_UNREADABLE", "message": str(error)[:300]})
    return {"root": root, "files": sorted(files, key=lambda item: item["path"]),
            "directories": sorted(directories), "diagnostics": diagnostics, "read_bytes": bytes_read, "source_bytes": total}
