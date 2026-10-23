"""Identify the Core/Worker interpreter separately from source implementation.

Candidate commands have their own observed environment. These identities are
local consistency checks, not protection against a same-user state writer.
"""
from functools import lru_cache
import hashlib
from pathlib import Path
import sys

from .errors import require


def _fingerprint(value):
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


@lru_cache(maxsize=8)
def _digest(path, fingerprint):
    target = Path(path)
    require(_fingerprint(target.stat()) == fingerprint, "PYTHON_RUNTIME_CHANGED", "Runtime file changed before reading")
    with target.open("rb") as stream:
        result = hashlib.file_digest(stream, "sha256").hexdigest()
    require(_fingerprint(target.stat()) == fingerprint, "PYTHON_RUNTIME_CHANGED", "Runtime file changed while reading")
    return result


def _file_hash(path):
    path = Path(path).resolve(strict=True)
    return _digest(str(path), _fingerprint(path.stat()))


def python_identity():
    config = Path(sys.prefix) / "pyvenv.cfg"
    return {"implementation": sys.implementation.name, "version": list(sys.version_info[:3]),
            "cache_tag": sys.implementation.cache_tag, "executable_sha256": _file_hash(sys.executable),
            "prefix": str(Path(sys.prefix).resolve()), "base_prefix": str(Path(sys.base_prefix).resolve()),
            "venv_config_sha256": _file_hash(config) if config.is_file() else None}


def validate_domain_runtime(pinned):
    # Legacy records retain the old identity check. Reads never rewrite them.
    if pinned.get("schema_version") == "domain-identity-v3":
        runtime = pinned.get("runtime", {})
        require(runtime.get("schema_version") == "domain-runtime-v1", "DOMAIN_IDENTITY_UNAVAILABLE", "Missing pinned Domain runtime")
        require(runtime.get("python") == python_identity(), "PYTHON_RUNTIME_CHANGED",
                "Pinned Core/Worker Python environment changed; restore it or use an explicit linked continuation")
