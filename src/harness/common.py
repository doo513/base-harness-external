"""Canonical serialization, atomic records and redaction extracted from Base Harness.

No legacy verifier engine or Ready issuer is included.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

MAX_CAPTURE_CHARS = 32_000
MAX_LIST_ITEMS = 200
TRUNCATED_MARKER = "\n[TRUNCATED]"
TRUNCATED_ITEM_MARKER = "[TRUNCATED]"
SECRET_KEY = re.compile(
    r"(authorization|api[-_]?key|token|secret|password|cookie|credential|cred|비밀|인증)",
    re.IGNORECASE,
)
SECRET_VALUE = re.compile(
    r"(?:bearer|basic)(?:\s|%20)+[A-Za-z0-9._~+/%=-]{8,}"
    r"|AIza[0-9A-Za-z_-]{20,}"
    r"|(?:sk|ghp|github_pat|xox[baprs])[-_][0-9A-Za-z_-]{12,}"
    r"|-----BEGIN(?:%20|\s)+(?:RSA(?:%20|\s+))?PRIVATE(?:%20|\s+)KEY-----",
    re.IGNORECASE,
)


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _truncate_for_capture(value: str) -> str:
    return value if len(value) <= MAX_CAPTURE_CHARS else value[:MAX_CAPTURE_CHARS] + TRUNCATED_MARKER


def _redact_string(value: str) -> str:
    return _truncate_for_capture(SECRET_VALUE.sub("[REDACTED]", value))


def redact(value: Any, key: str = "") -> Any:
    if SECRET_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, str):
        return _redact_string(value)
    if isinstance(value, dict):
        return {str(item_key): redact(item, str(item_key)) for item_key, item in value.items()}
    if isinstance(value, list):
        items = [redact(item) for item in value[:MAX_LIST_ITEMS]]
        if len(value) > MAX_LIST_ITEMS:
            items.append(TRUNCATED_ITEM_MARKER)
        return items
    return value
