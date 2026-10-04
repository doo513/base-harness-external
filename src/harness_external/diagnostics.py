"""Local readiness checks. The optional probe uses the same strict adapter."""
import os
from pathlib import Path
import platform
import shutil
import sys
import tempfile
import threading

from .worker import execute_sandbox


def doctor(store, *, sandbox=False):
    from .identity import deployment_identity
    bridge = Path(__file__).resolve().parents[2] / "runtime/script/external-harness-sandbox.ts"
    bun = shutil.which(os.environ.get("BUN", "bun"))
    with store.transaction() as connection:
        json_available = connection.execute("SELECT json_extract('{\"ok\":1}', '$.ok')").fetchone()[0] == 1
    result = {"capabilities": {"invoke": True, "context": "harness-context-v1", "storage": "run-store-v2"},
              "deployment": deployment_identity(),
              "python": sys.version.split()[0], "executable": sys.executable, "platform": platform.platform(),
              "state_directory": str(store.root), "state_store": "available", "sqlite_json": json_available,
              "structural_available": json_available, "bun": bun, "sandbox_adapter": str(bridge) if bridge.is_file() else None,
              "command_prerequisites_present": bool(bun and bridge.is_file()), "sandbox_probe": "not_requested",
              "command_execution_verified": False}
    if sandbox:
        with tempfile.TemporaryDirectory(prefix="doctor-", dir=store.root) as temporary:
            workspace = Path(temporary) / "workspace"
            workspace.mkdir()
            check = {"argv": ["python3", "-c", "print('harness-sandbox-probe')"], "cwd": ".", "timeout_seconds": 10}
            receipt = execute_sandbox(check, workspace, store.root, threading.Event(), 30)
            capture = receipt.get("capture") or {}
            passed = (receipt["status"] == "completed" and capture.get("execution") == "completed"
                      and capture.get("exitCode") == 0 and capture.get("stdout", "").strip() == "harness-sandbox-probe")
            result.update(sandbox_probe=receipt, command_execution_verified=passed)
    result["healthy"] = result["structural_available"] and (not sandbox or result["command_execution_verified"])
    return result
