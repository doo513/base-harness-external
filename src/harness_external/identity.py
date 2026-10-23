"""Explicit execution boundary, separate from presentation/deployment identity."""
import hashlib
from pathlib import Path
import os
import shutil
from functools import lru_cache

from harness.common import canonical_hash
from .errors import require


# Changes to these files can change admission, measurements or their bindings.
# Query rendering, CLI/caller transport and documentation are not executors.
EXECUTION_FILES = (
    "harness/measurement_v5.py", "harness/common.py", "harness/json_codec.py", "harness/snapshot_io.py",
    "harness_external/service.py", "harness_external/worker.py",
    "harness_external/snapshots.py", "harness_external/store.py",
    "harness_external/journal.py", "harness_external/read_core.py",
    "harness_external/errors.py", "harness_external/semantics.py",
    "harness_external/need_records.py",
    "harness_external/registry.py", "harness_external/identity.py", "harness_external/runtime_identity.py",
    "harness_external/acceptance.py", "harness_external/evidence.py",
    "harness_external/maintenance.py", "harness_external/dispatch.py",
    "harness_external/observation_rules.py", "harness_external/observation_links.py", "harness_external/adapter_execution.py",
    # Concrete adapter/runner code is pinned independently for each used adapter.
    "harness_external/adapter_ports.py", "harness_external/adapter_registry.py", "harness_external/adapter_loading.py",
    "harness_external/execution_inputs.py",
    "harness_external/check_preparation.py",
    "harness_external/intent_sources.py",
    "harness_external/operation_schema.py", "harness_external/workspace_constraints.py",
    "harness_external/paired_inputs.py",
    "harness_external/task_storage.py", "harness_external/artifact_io.py",
    "harness_external/application_schema.py",
    "harness_external/delivery.py",
)


@lru_cache(maxsize=8)
def _runtime_hash(filename, identity):
    path = Path(filename)
    before = path.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != identity:
        raise OSError("Runtime changed before identity hashing")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    after = path.stat()
    if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) != identity:
        raise OSError("Runtime changed during identity hashing")
    return digest


def verifier_identity():
    source = Path(__file__).resolve().parents[1]
    root = source.parent
    paths = {name: source / name for name in EXECUTION_FILES}
    paths.update(sandbox_bridge=root / "runtime/script/external-harness-sandbox.ts",
                 sandbox=root / "runtime/packages/security/src/sandbox.ts")
    sources = {name: hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "unavailable"
               for name, path in paths.items()}
    selected = os.environ.get("BUN")
    require(not selected or Path(selected).is_absolute(), "RUNTIME_UNTRUSTED", "BUN must be an operator-selected absolute executable path")
    standard = Path.home() / ".bun" / "bin" / "bun"
    bun = str(Path(selected).resolve()) if selected else str(standard.resolve()) if standard.is_file() else shutil.which("bun", path="/usr/local/bin:/usr/bin:/bin")
    if bun:
        target = Path(bun).resolve()
        info = target.stat()
        fingerprint = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        sources["runtime:bun"] = _runtime_hash(str(target), fingerprint)
    return {"schema_version": "verifier-identity-v2", "id": "external-develop-measurement",
            "revision": "2", "measurement_protocol": 5,
            "implementation_hash": canonical_hash(sources), "sources": sources}


def deployment_identity():
    """Diagnostic only: never used to accept or reject a verification."""
    source = Path(__file__).resolve().parents[1]
    sources = {str(path.relative_to(source)): hashlib.sha256(path.read_bytes()).hexdigest()
               for package in ("harness", "harness_external") for path in sorted((source / package).glob("*.py"))}
    return {"schema_version": "deployment-source-v1", "source_hash": canonical_hash(sources)}
