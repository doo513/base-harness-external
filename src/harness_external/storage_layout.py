"""Project control paths, not Domain semantics or an additional Run ledger."""
import os
from pathlib import Path

from .artifact_io import read_bytes
from .errors import require, relative_path
from .snapshots import no_links

CONTROL_DIRECTORY = ".harness"


def project_path(value):
    require(isinstance(value, str) and Path(value).is_absolute(), "PROJECT_WORKSPACE", "Project storage requires an absolute workspace")
    path = Path(value).absolute()
    no_links(path)
    require(path.is_dir() and path != Path(path.anchor) and CONTROL_DIRECTORY not in path.parts,
            "PROJECT_WORKSPACE", "Select an existing project, not a filesystem or Harness control directory")
    return path.resolve()


def control_path(workspace):
    root = project_path(str(workspace)) / CONTROL_DIRECTORY
    no_links(root)
    return root


def prepare_control(workspace):
    root = control_path(workspace)
    root.mkdir(mode=0o700, exist_ok=True)
    if os.name == "posix":
        require(root.stat().st_uid == os.getuid(), "PROJECT_STORAGE_OWNER", "Project state must be owned by the Harness user")
        root.chmod(0o700)
    for name in (".gitignore", ".ignore"):
        path = root / name
        no_links(path)
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except FileExistsError:
            require(read_bytes(path, 4096) == b"*\n", "PROJECT_IGNORE_CONFLICT", "Existing .harness exclusion differs; preserve it and explicitly resolve the exclusion")
        else:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(b"*\n")
                stream.flush()
                os.fsync(stream.fileno())
    return root


def validate_inputs(contract):
    for path in [*contract.get("inputs", []), *contract.get("artifacts", [])]:
        relative_path(path)
        require(path.split("/", 1)[0].casefold() != CONTROL_DIRECTORY,
                "WORKSPACE_CONTROL_PATH", "Project control files are not source inputs or artifacts")


def validate_store(workspace, directory):
    base = control_path(workspace) / "tasks"
    require(directory.parent == base and directory.name.startswith("task_"),
            "STATE_WORKSPACE_OVERLAP", "Only an explicitly selected project task store may be inside a workspace")
