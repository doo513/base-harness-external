"""Application-owned store selection. Each task still uses the unchanged Core."""
import copy
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile
import time

from harness.common import canonical_hash
from .artifact_io import locked, read_json, write_json
from .errors import HarnessError, fields, integer, require
from .snapshots import no_links
from .store import identifier


def task_id_for(request_id, workspace, domain):
    require(isinstance(request_id, str) and 0 < len(request_id) <= 128
            and isinstance(workspace, str) and Path(workspace).is_absolute() and isinstance(domain, str),
            "TASK_START_CONTEXT", "Task creation requires the original request ID, absolute workspace and Domain")
    return "task_" + canonical_hash({"request_id": request_id, "workspace": os.path.abspath(workspace), "domain": domain})[:32]


class TaskStores:
    MAX_TASKS = 512
    def __init__(self, api):
        self.api = api
        self.root = api.store.root

    def directory(self, task_id):
        target = self.root / "tasks" / identifier(task_id, "task")
        no_links(target)
        return target

    def manifest(self, task_id):
        target = self.directory(task_id) / "task.json"
        require(target.is_file(), "TASK_STORE_MISSING", "Selected task store is missing; do not create a replacement")
        body = read_json(target, 16384)
        fields(body, {"schema_version", "task_id", "workspace", "domain", "start_request_id", "created_at", "database_created", "manifest_hash"},
               {"schema_version", "task_id", "workspace", "domain", "start_request_id", "created_at", "database_created", "manifest_hash"})
        require(body["schema_version"] == "task-store-v1" and body["task_id"] == task_id
                and body["manifest_hash"] == canonical_hash({k: v for k, v in body.items() if k != "manifest_hash"})
                and isinstance(body["workspace"], str) and Path(body["workspace"]).is_absolute()
                and isinstance(body["domain"], str) and body["database_created"] is True,
                "TASK_STORE_CORRUPT", "Task connection metadata is invalid")
        return body

    def selected(self, task_id):
        from .service import Harness
        manifest = self.manifest(task_id)
        directory = self.directory(task_id)
        no_links(directory / "runs.sqlite3")
        require((directory / "runs.sqlite3").is_file(), "TASK_STORE_MISSING",
                "Selected task database is missing; recovery must not initialize an empty database")
        try:
            with closing(sqlite3.connect((directory / "runs.sqlite3").as_uri() + "?mode=ro", uri=True)) as connection:
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            from .store import LEGACY_TABLES, REQUIRED_TABLES
            require((REQUIRED_TABLES if "store_schema" in tables else LEGACY_TABLES) <= tables,
                    "TASK_STORE_CORRUPT", "Task database is incomplete; do not initialize or repair a replacement")
        except sqlite3.Error as error:
            raise HarnessError("TASK_STORE_CORRUPT", "Task database is unreadable; preserve it for recovery") from error
        selected = Harness(directory, domains=self.api.domains, adapters=self.api.adapters, policy_root=self.api.policies.root)
        expected = (task_id, manifest["workspace"], manifest["domain"], manifest["start_request_id"], manifest["manifest_hash"])
        with selected.store.transaction(write=False) as connection:
            row = connection.execute("SELECT task_id,workspace,domain,start_request_id,manifest_hash FROM task_identity WHERE singleton=1").fetchone()
            if row is not None:
                require(row == expected, "TASK_STORE_IDENTITY", "Task manifest and database identity differ")
                return selected
        with selected.store.transaction() as connection:
            row = connection.execute("SELECT task_id,workspace,domain,start_request_id,manifest_hash FROM task_identity WHERE singleton=1").fetchone()
            if row is None:
                ids = [item[0] for item in connection.execute("SELECT run_id FROM runs UNION SELECT run_id FROM run_heads")]
                for run_id in ids:
                    run = selected.store.run(connection, run_id)
                    require(run["workspace"] == manifest["workspace"] and run["contract"]["domain_id"] == manifest["domain"],
                            "TASK_STORE_IDENTITY", "Legacy task database belongs to another workspace or Domain")
                connection.execute("INSERT INTO task_identity VALUES(1,?,?,?,?,?)", expected)
            else:
                require(row == expected, "TASK_STORE_IDENTITY", "Task manifest and database identity differ")
        return selected

    @staticmethod
    def _allowed(manifest, allowed_workspace):
        require(allowed_workspace is None or allowed_workspace(Path(manifest["workspace"])),
                "WORKSPACE_NOT_ALLOWED", "Task workspace is outside the configured roots")

    def invoke(self, request, invoke_core, *, allowed_workspace=None):
        task_id = identifier(request["task_id"], "task")
        inner = {k: v for k, v in request.items() if k != "task_id"}
        if request["operation"] != "start":
            manifest = self.manifest(task_id)
            self._allowed(manifest, allowed_workspace)
            return {**invoke_core(self.selected(task_id), inner), "task_id": task_id}
        arguments = request["arguments"]
        workspace = arguments.get("workspace")
        domain = arguments.get("domain", arguments.get("domain_id"))
        expected = task_id_for(request.get("request_id"), workspace, domain)
        source = Path(workspace).absolute()
        no_links(source)
        require(source.is_dir(), "WORKSPACE_REQUIRED", "Task workspace must exist")
        require(not self.root.is_relative_to(source) and not source.is_relative_to(self.root),
                "STATE_WORKSPACE_OVERLAP", "Task storage and workspace must remain disjoint")
        self._allowed({"workspace": str(source)}, allowed_workspace)
        target = self.directory(task_id)
        # Different task IDs need one catalog lock while enforcing the global
        # retention limit and publishing a new directory. Release it before the
        # potentially slow Core start; that part remains serialized per task.
        with locked(self.root, "task-catalog"):
            with locked(self.root, task_id):
                manifest_path = target / "task.json"
                if manifest_path.exists():
                    manifest = self.manifest(task_id)
                    require(manifest["workspace"] == str(source) and manifest["domain"] == domain,
                            "RUN_CONTEXT_MISMATCH", "Task belongs to another workspace/Domain")
                    require(manifest["start_request_id"] == request["request_id"] or arguments.get("predecessor_run_id"),
                            "TASK_START_CONFLICT", "Use a new task for unrelated work; existing tasks only admit explicit linked continuations")
                else:
                    require(not target.exists(), "TASK_STORE_CORRUPT", "Task directory has no connection metadata; preserve it for recovery")
                    require(expected == task_id and not arguments.get("predecessor_run_id"),
                            "TASK_START_CONFLICT", "New tasks require their original derived identity; continuations require an existing task")
                    folder = self.root / "tasks"
                    existing = sum(1 for path in folder.iterdir() if path.name.startswith("task_")) if folder.exists() else 0
                    require(existing < self.MAX_TASKS, "TASK_LIMIT", "Task store limit reached; archive tasks before creating another")
                    manifest = {"schema_version": "task-store-v1", "task_id": task_id, "workspace": str(source),
                                "domain": domain, "start_request_id": request["request_id"], "created_at": time.time(),
                                "database_created": True}
                    # Publish connection metadata and an initialized DB together.
                    # A lost start response never looks like permission to rebuild
                    # a missing database, including a crash during initial setup.
                    from .service import Harness
                    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    with tempfile.TemporaryDirectory(prefix=".task-init-", dir=target.parent) as temporary:
                        staged = Path(temporary)
                        provision = Harness(staged, domains=self.api.domains, adapters=self.api.adapters, policy_root=self.api.policies.root)
                        document = {**manifest, "manifest_hash": canonical_hash(manifest)}
                        with provision.store.transaction() as connection:
                            connection.execute("INSERT INTO task_identity VALUES(1,?,?,?,?,?)",
                                               (task_id, manifest["workspace"], manifest["domain"], manifest["start_request_id"], document["manifest_hash"]))
                        write_json(staged / "task.json", document)
                        os.rename(staged, target)
                        if os.name == "posix":
                            descriptor = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
                            try:
                                os.fsync(descriptor)
                            finally:
                                os.close(descriptor)
        with locked(self.root, task_id):
            selected = self.selected(task_id)
            result = invoke_core(selected, inner)
            return {**result, "task_id": task_id}

    def list(self, *, workspace=None, domain=None, offset=0, limit=20, allowed_workspace=None):
        offset, limit = integer(offset, 0, 2**31 - 1, "offset"), integer(limit, 1, 100, "limit")
        require(workspace is None or isinstance(workspace, str) and Path(workspace).is_absolute(),
                "TASK_FILTER", "Workspace filter must be absolute")
        require(domain is None or isinstance(domain, str), "TASK_FILTER", "Domain filter must be a string")
        folder = self.root / "tasks"
        no_links(folder)
        items, corrupt = [], []
        for path in sorted(folder.iterdir()) if folder.exists() else ():
            if not path.name.startswith("task_"):
                continue
            try:
                body = self.manifest(path.name)
            except (HarnessError, OSError, ValueError) as error:
                if allowed_workspace is None:
                    corrupt.append({"task_id": path.name, "error": getattr(error, "code", "TASK_STORE_CORRUPT")})
                continue
            if allowed_workspace and not allowed_workspace(Path(body["workspace"])):
                continue
            if workspace is not None and body["workspace"] != str(Path(workspace).absolute()):
                continue
            if domain is not None and body["domain"] != domain:
                continue
            items.append({key: copy.deepcopy(body[key]) for key in ("task_id", "workspace", "domain", "created_at")})
        page = items[offset:offset + limit]
        return {"items": page, "total": len(items), "next_offset": offset + len(page) if offset + len(page) < len(items) else None,
                "corrupt": corrupt[:20], "corrupt_count": len(corrupt),
                "corrupt_scope": "hidden_by_workspace_filter" if allowed_workspace is not None else "all_visible_tasks",
                "meaning": "connection_metadata_not_run_state"}
