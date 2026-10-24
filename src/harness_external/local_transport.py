"""Optional local transport for an operator-launched, model-free Harness.

The Host sends existing invoke envelopes. Core owns all state and dispatch;
workers inherit this process's execution context, not the Host tool's sandbox.
No command execution or shell endpoint is exposed by this transport.
"""
import os
from pathlib import Path
import socket
import socketserver
import stat
import struct
import threading
import uuid

from harness.json_codec import parse_message, render_message
from . import API_VERSION, ASSURANCE
from .dispatch import METHODS, invoke
from .errors import HarnessError, fields, require
from .snapshots import no_links
from .identity import deployment_identity


PROTOCOL = "harness-local-transport-v1"
MAX_REQUEST = 256 * 1024
MAX_RESPONSE = 8 * 1024 * 1024


def configuration(api):
    return {"state_dir": str(api.store.root), "policy_root": str(api.policies.root) if api.policies.root else None}


def request(socket_path, expected_configuration, envelope, *, timeout=None):
    """No fallback/retry: response loss leaves the mutation outcome unknown."""
    payload = render_message({"protocol": PROTOCOL, "configuration": expected_configuration, "request": envelope}) + b"\n"
    require(len(payload) <= MAX_REQUEST, "TRANSPORT_REQUEST_LIMIT", "Request exceeds 256 KiB")
    sent = False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout)
            connection.connect(str(socket_path))
            sent = True
            connection.sendall(payload)
            with connection.makefile("rb") as stream:
                raw = stream.readline(MAX_RESPONSE + 1)
    except OSError as error:
        raise HarnessError("CALL_OUTCOME_UNKNOWN" if sent else "TRANSPORT_UNAVAILABLE",
                           "No reliable server response. Query the same Run or replay the IDENTICAL request ID and payload; "
                           "do not create a replacement Run or silently execute locally." if sent else
                           "Configured Harness server is unavailable; start it outside the Host's restricted execution context.") from error
    require(raw.endswith(b"\n") and len(raw) <= MAX_RESPONSE, "CALL_OUTCOME_UNKNOWN", "Missing or oversized server response")
    result = parse_message(raw)
    require(isinstance(result, dict) and result.get("api_version") == API_VERSION and type(result.get("ok")) is bool,
            "TRANSPORT_RESPONSE", "Server returned an incompatible response")
    if result.get("ok"):
        require(result.get("transport", {}).get("configuration") == expected_configuration,
                "TRANSPORT_CONFIGURATION_MISMATCH", "Response belongs to a different Harness/store configuration")
    return result


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(10)
        try:
            _, uid, _ = struct.unpack("3i", self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            require(uid in self.server.peer_uids, "TRANSPORT_PEER", "Only a configured local client UID may connect")
            raw = self.rfile.readline(MAX_REQUEST + 1)
            require(raw.endswith(b"\n") and len(raw) <= MAX_REQUEST, "TRANSPORT_REQUEST_LIMIT", "Request must be one bounded JSON line")
            packet = fields(parse_message(raw), {"protocol", "configuration", "request"}, {"protocol", "configuration", "request"})
            require(packet["protocol"] == PROTOCOL, "TRANSPORT_VERSION", "Unsupported local transport protocol")
            require(packet["configuration"] == self.server.configuration,
                    "TRANSPORT_CONFIGURATION_MISMATCH", "Request addresses a different Harness/store configuration")
            require(deployment_identity() == self.server.deployment, "SERVER_RUNTIME_CHANGED",
                    "Harness code changed after this server started. Restart the operator server; retain the existing store and request IDs.")
            envelope = packet["request"]
            self.server.authorize(envelope)
            wait_seconds = envelope.get("arguments", {}).get("wait_seconds", 0)
            long_poll = envelope.get("operation") in {"wait", "checkpoint"} and type(wait_seconds) in {int, float} and wait_seconds > 0
            require(not long_poll or wait_seconds <= 30, "WAIT_INVALID", "Socket long polling is limited to 30 seconds")
            acquired = not long_poll or self.server._long_slots.acquire(blocking=False)
            require(acquired, "SERVER_BUSY", "Long-poll capacity is occupied; use a short status read")
            try:
                result = invoke(self.server.api, envelope, allowed_workspace=self.server.allowed)
            finally:
                if long_poll and acquired:
                    self.server._long_slots.release()
            if envelope["operation"] == "list-runs":
                result["workspace_filter_applied"] = True
            response = {"ok": True, "api_version": API_VERSION, "assurance": ASSURANCE, "ready": False, **result}
        except (HarnessError, ValueError, OSError, TypeError, RecursionError) as error:
            response = {"ok": False, "api_version": API_VERSION, "assurance": ASSURANCE, "ready": False,
                        "error": error.as_dict() if isinstance(error, HarnessError) else {"code": "TRANSPORT_ERROR", "message": str(error)[:2000]},
                        **({"checkpoint_progress": error.checkpoint_progress} if hasattr(error, "checkpoint_progress") else {})}
        response["transport"] = {"protocol": PROTOCOL, "server_id": self.server.server_id, "configuration": self.server.configuration}
        encoded = render_message(response) + b"\n"
        if len(encoded) > MAX_RESPONSE:
            encoded = render_message({"ok": False, "api_version": API_VERSION, "assurance": ASSURANCE, "ready": False,
                                       "error": {"code": "RESPONSE_TOO_LARGE", "message": "Use a compact view or smaller records page. A mutation may already have committed."}}) + b"\n"
        try:
            self.wfile.write(encoded)
            self.wfile.flush()
        except OSError:
            # A lost response does not undo a committed Core operation.
            pass


class HarnessServer(socketserver.ThreadingMixIn, getattr(socketserver, "UnixStreamServer", socketserver.TCPServer)):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 16

    def __init__(self, api, socket_path, workspace_roots, peer_uids=None):
        require(os.name == "posix" and hasattr(socket, "AF_UNIX") and hasattr(socket, "SO_PEERCRED"),
                "TRANSPORT_UNAVAILABLE", "Local socket transport requires Linux AF_UNIX peer credentials")
        require(workspace_roots, "WORKSPACE_ROOT_REQUIRED", "Explicitly configure one or more allowed workspace roots")
        self.api = api
        self.peer_uids = frozenset(peer_uids or {os.getuid()})
        require(self.peer_uids and all(type(uid) is int and uid >= 0 for uid in self.peer_uids),
                "TRANSPORT_PEER", "Configure one or more numeric client UIDs")
        self.configuration = configuration(api)
        self.deployment = deployment_identity()
        self.server_id = uuid.uuid4().hex
        self.workspace_roots = []
        for value in workspace_roots:
            root = Path(value).expanduser().absolute()
            no_links(root)
            require(root.is_dir() and root != Path(root.anchor), "WORKSPACE_ROOT_REQUIRED", "Use an existing, bounded workspace root")
            self.workspace_roots.append(root.resolve())
        self.socket_path = Path(socket_path).expanduser().absolute()
        no_links(self.socket_path)
        require(len(os.fsencode(self.socket_path)) <= 103, "TRANSPORT_PATH", "Unix socket path exceeds 103 bytes; choose a shorter runtime directory")
        require(not self.allowed(self.socket_path) and not self.allowed(api.store.root),
                "TRANSPORT_PATH", "Socket and state must be outside every allowed workspace root")
        self.socket_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        parent = self.socket_path.parent.stat()
        foreign = self.peer_uids != {os.getuid()}
        require(parent.st_uid == os.getuid() and parent.st_mode & 0o007 == 0
                and (not foreign and parent.st_mode & 0o077 == 0 or foreign and parent.st_mode & 0o010),
                "TRANSPORT_PATH", "Socket parent must be server-owned, deny other users, and grant group traversal only for configured foreign clients")
        require(not self.socket_path.exists(), "TRANSPORT_EXISTS", "An endpoint already exists; never replace it automatically")
        self._socket_identity = None
        self._slots = threading.BoundedSemaphore(16)
        self._long_slots = threading.BoundedSemaphore(8)
        super().__init__(str(self.socket_path), _Handler)
        self.socket_path.chmod(0o660 if foreign else 0o600)
        metadata = self.socket_path.stat()
        self._socket_identity = (metadata.st_dev, metadata.st_ino)

    def allowed(self, value):
        return any(value == root or value.is_relative_to(root) for root in self.workspace_roots)

    def authorize(self, envelope):
        from .application_schema import OPTIONS as APP_OPTIONS
        from .task_storage import TaskStores
        fields(envelope, {"operation", "arguments", "request_id", "context", "task_id", "project_workspace", "knowledge", "input_bindings"}, {"operation", "arguments"})
        if "project_workspace" in envelope:
            value = envelope["project_workspace"]
            require(isinstance(value, str) and Path(value).is_absolute() and self.allowed(Path(value)),
                    "WORKSPACE_NOT_ALLOWED", "Project store is outside the configured workspace roots")
        operation, arguments = envelope["operation"], envelope["arguments"]
        require(isinstance(operation, str) and operation in {*METHODS, *APP_OPTIONS, "doctor", "checkpoint", "wait"}, "OPERATION_UNSUPPORTED", "Unknown operation")
        require(isinstance(arguments, dict), "INVALID_PARAMETERS", "Arguments must be an object")
        if operation in {"doctor", "list-runs", "describe", "list-tasks", "knowledge-find", "knowledge-read", "report-list", "report-read"}:
            # Global knowledge reads are filtered inside the application before
            # text is returned; scoped reads also validate their task manifest.
            return
        if operation == "start":
            value = arguments.get("workspace")
            require(isinstance(value, str) and Path(value).is_absolute(), "WORKSPACE_NOT_ALLOWED", "An absolute allowed workspace is required")
            target = Path(value)
            no_links(target)
            target = target.resolve()
        else:
            selected = TaskStores(self.api, envelope.get("project_workspace")).selected(envelope["task_id"]) if "task_id" in envelope else self.api
            with selected.store.transaction(write=False) as connection:
                target = Path(selected.store.current(connection, arguments.get("run_id"))["workspace"])
        require(self.allowed(target), "WORKSPACE_NOT_ALLOWED", "Workspace is outside this server's configured roots")

    def process_request(self, connection, address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(connection)
            return
        try:
            super().process_request(connection, address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, connection, address):
        try:
            super().process_request_thread(connection, address)
        finally:
            self._slots.release()

    def server_close(self):
        super().server_close()
        try:
            metadata = self.socket_path.lstat()
            if stat.S_ISSOCK(metadata.st_mode) and (metadata.st_dev, metadata.st_ino) == self._socket_identity:
                self.socket_path.unlink()
        except FileNotFoundError:
            pass


def serve(api, socket_path, workspace_roots, peer_uids=None):
    with HarnessServer(api, socket_path, workspace_roots, peer_uids=peer_uids) as server:
        print(render_message({"event": "harness_server_ready", "socket": str(server.socket_path),
                               "configuration": server.configuration, "server_id": server.server_id}).decode("utf-8"), flush=True)
        try:
            server.serve_forever(poll_interval=.1)
        except KeyboardInterrupt:
            pass
