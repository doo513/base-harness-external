"""Explicit fixture expectations/receipts. Fake receipts are NOT live evidence."""
import hashlib

from harness_external.execution_inputs import make_binding


def inputs(root):
    files = [{"path": p.relative_to(root).as_posix(), "size": p.stat().st_size,
              "sha256": hashlib.sha256(p.read_bytes()).hexdigest(), "executable": bool(p.stat().st_mode & 0o111)}
             for p in root.rglob("*") if p.is_file()]
    return make_binding(files, {"test_workspace": str(root)})


def receipt(binding):
    return {"schema_version": "execution-input-receipt-v1", "scope_hash": binding["scope_hash"],
            "input_hash": binding["input_hash"], "checked_at": "2026-10-01T00:00:00Z",
            "post_checked_at": "2026-10-01T00:00:01Z", "unchanged": True, "changed_inputs": []}
