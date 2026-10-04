"""Explicit execution boundary, separate from presentation/deployment identity."""
import hashlib
from pathlib import Path

from harness.common import canonical_hash


# Changes to these files can change admission, measurements or their bindings.
# Query rendering, CLI/caller transport and documentation are not executors.
EXECUTION_FILES = (
    "harness/measurement_v5.py", "harness/common.py", "harness/json_codec.py",
    "harness_external/service.py", "harness_external/worker.py",
    "harness_external/snapshots.py", "harness_external/store.py",
    "harness_external/journal.py", "harness_external/read_core.py",
    "harness_external/errors.py", "harness_external/semantics.py",
    "harness_external/registry.py", "harness_external/identity.py",
    "harness_external/acceptance.py", "harness_external/evidence.py",
    "harness_external/maintenance.py", "harness_external/dispatch.py",
)


def verifier_identity():
    source = Path(__file__).resolve().parents[1]
    root = source.parent
    paths = {name: source / name for name in EXECUTION_FILES}
    paths.update(sandbox_bridge=root / "runtime/script/external-harness-sandbox.ts",
                 sandbox=root / "runtime/packages/security/src/sandbox.ts")
    sources = {name: hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "unavailable"
               for name, path in paths.items()}
    return {"schema_version": "verifier-identity-v2", "id": "external-develop-measurement",
            "revision": "2", "measurement_protocol": 5,
            "implementation_hash": canonical_hash(sources), "sources": sources}


def deployment_identity():
    """Diagnostic only: never used to accept or reject a verification."""
    source = Path(__file__).resolve().parents[1]
    sources = {str(path.relative_to(source)): hashlib.sha256(path.read_bytes()).hexdigest()
               for package in ("harness", "harness_external") for path in sorted((source / package).glob("*.py"))}
    return {"schema_version": "deployment-source-v1", "source_hash": canonical_hash(sources)}
