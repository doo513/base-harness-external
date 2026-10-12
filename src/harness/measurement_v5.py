"""Protocol v5 facts only. No contract acceptance, repair scheduler or Ready issuer.

File observations read digest-bound snapshots. Command observations consume a
receipt from the authenticated Host execution adapter, which owns permission,
sandbox and process cancellation. This engine has no subprocess execution path.
"""
from __future__ import annotations

import hashlib
import codecs
from collections import OrderedDict
import io
import json
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple
from .json_codec import MAX_BYTES, MeasurementProtocolError, decode, _pairs
from .common import canonical_bytes
from .snapshot_io import path_parts, read_snapshot, iter_snapshot_chunks

PROTOCOL_VERSION = 5
MAX_CACHED_REPLY_BYTES = 256 * 1024


class _ReplyRef(NamedTuple):
    offset: int
    size: int
    sha256: str


def _object(value: Any, fields: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise MeasurementProtocolError("unexpected object fields")
    return value


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise MeasurementProtocolError("nonempty string required")
    return value


def _digest(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise MeasurementProtocolError("invalid digest")
    return value


def _integer(value: Any, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= 2**53 - 1:
        raise MeasurementProtocolError("invalid integer")
    return value


def _ref(value: Any, subject: bool = False) -> dict[str, Any]:
    fields = {"id", "revision", "sha256"} | ({"kind"} if subject else set())
    _object(value, fields)
    _text(value["id"])
    _integer(value["revision"], 1)
    _digest(value["sha256"])
    if subject and value["kind"] not in {"source", "report", "candidate", "workspace", "resource_snapshot"}:
        raise MeasurementProtocolError("invalid subject kind")
    return value


def _timestamp(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timezone required")
        return parsed
    except ValueError as error:
        raise MeasurementProtocolError("invalid timestamp") from error


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _safe_parts(relative: str) -> list[str]:
    try:
        return path_parts(relative)
    except ValueError as error:
        raise MeasurementProtocolError(str(error)) from error


def _snapshot_bytes(root: Path, relative: str) -> bytes:
    _safe_parts(relative)
    return read_snapshot(root, relative)


def _value(name: str, observed: Any) -> dict[str, Any]:
    return {"kind": "value", "name": name, "observed": observed}


def _comparison(name: str, expected: Any, observed: Any, operator: str = "equals") -> dict[str, Any]:
    matched = expected == observed if operator == "equals" else expected in observed
    return {"kind": "comparison", "name": name, "operator": operator, "expected": expected,
            "observed": observed, "result": "pass" if matched else "fail"}


class MeasurementEngine:
    def __init__(self) -> None:
        self.run_id: str | None = None
        self.root: Path | None = None
        self.requests: dict[str, tuple[str, bytes | _ReplyRef]] = {}
        self.reply_bytes = 0
        self.reply_file = None
        self.closed = False
        # Only immutable manifest metadata is cached, never file bytes/results.
        self.manifests = OrderedDict()
        self.manifest_bytes = 0

    def handle(self, envelope: dict[str, Any]) -> dict[str, Any]:
        if self.closed:
            raise MeasurementProtocolError("measurement engine is closed")
        _object(envelope, {"version", "id", "runId", "scopeId", "type", "payload"})
        if type(envelope["version"]) is not int or envelope["version"] != PROTOCOL_VERSION:
            raise MeasurementProtocolError("MEASUREMENT_PROTOCOL_VERSION")
        for key in ("id", "runId", "scopeId", "type"):
            _text(envelope[key])
        kind = envelope["type"]
        if kind == "hello":
            _object(envelope["payload"], set())
            return {"protocolVersion": PROTOCOL_VERSION, "semantics": "observations-only"}
        if kind == "run.open":
            payload = _object(envelope["payload"], {"snapshotRoot"})
            root = Path(_text(payload["snapshotRoot"])).resolve(strict=True)
            if not root.is_dir() or self.run_id is not None:
                raise MeasurementProtocolError("run already opened or invalid snapshot root")
            self.run_id, self.root = envelope["runId"], root
            return {"opened": True}
        if self.run_id != envelope["runId"] or self.root is None:
            raise MeasurementProtocolError("run binding mismatch")
        if kind != "measure":
            raise MeasurementProtocolError("unsupported measurement operation")
        fingerprint = hashlib.sha256(json.dumps(envelope, ensure_ascii=False, sort_keys=True,
                                               separators=(",", ":")).encode("utf-8")).hexdigest()
        previous = self.requests.get(envelope["id"])
        if previous:
            if previous[0] != fingerprint:
                raise MeasurementProtocolError("MEASUREMENT_REQUEST_CONFLICT")
            cached = previous[1]
            if isinstance(cached, _ReplyRef):
                self.reply_file.seek(cached.offset)
                encoded = self.reply_file.read(cached.size)
                if len(encoded) != cached.size or hashlib.sha256(encoded).hexdigest() != cached.sha256:
                    raise MeasurementProtocolError("MEASUREMENT_CACHE_CORRUPT")
            else:
                encoded = cached
            return json.loads(encoded.decode("utf-8"), object_pairs_hook=_pairs)
        result = self._measure(envelope)
        encoded = canonical_bytes(result)
        if self.reply_bytes + len(encoded) <= MAX_CACHED_REPLY_BYTES:
            cached = encoded
            self.reply_bytes += len(encoded)
        else:
            # Anonymous, engine-scoped replay storage is NOT another Run store.
            # Do not evict IDs or reread changed inputs to reconstruct old facts.
            if self.reply_file is None:
                self.reply_file = tempfile.TemporaryFile(mode="w+b")
            self.reply_file.seek(0, 2)
            offset = self.reply_file.tell()
            self.reply_file.write(encoded)
            cached = _ReplyRef(offset, len(encoded), hashlib.sha256(encoded).hexdigest())
        self.requests[envelope["id"]] = (fingerprint, cached)
        return result

    def close(self):
        if self.reply_file is not None:
            self.reply_file.close()
        self.requests.clear()
        self.closed = True

    def __del__(self):
        stream = getattr(self, "reply_file", None)
        if stream is not None:
            stream.close()

    def _manifest(self, subject, manifest_json):
        encoded = manifest_json.encode("utf-8")
        if len(encoded) > MAX_BYTES or hashlib.sha256(encoded).hexdigest() != subject["sha256"]:
            raise MeasurementProtocolError("subject manifest digest mismatch")
        key = (subject["kind"], subject["id"], subject["revision"], subject["sha256"])
        if key in self.manifests:
            self.manifests.move_to_end(key)
            return self.manifests[key][0]
        manifest = _object(decode(manifest_json), {"kind", "files", "dependencies"})
        if manifest["kind"] != subject["kind"] or not isinstance(manifest["files"], list) or not isinstance(manifest["dependencies"], list):
            raise MeasurementProtocolError("invalid subject manifest")
        paths, entries, total = set(), [], 0
        for entry in manifest["files"]:
            _object(entry, {"path", "sha256", "size"})
            _safe_parts(_text(entry["path"]))
            _digest(entry["sha256"])
            _integer(entry["size"])
            total += entry["size"]
            if total > MAX_BYTES:
                raise MeasurementProtocolError("manifest input exceeds measurement limit")
            if entry["path"] in paths:
                raise MeasurementProtocolError("duplicate manifest path")
            paths.add(entry["path"])
            entries.append((entry["path"], entry["sha256"], entry["size"]))
        for dependency in manifest["dependencies"]:
            _ref(dependency, True)
        compiled = tuple(entries)
        budget = 1024 * 1024
        if len(encoded) <= budget:
            while self.manifests and (len(self.manifests) >= 16 or self.manifest_bytes + len(encoded) > budget):
                _, (_, size) = self.manifests.popitem(last=False)
                self.manifest_bytes -= size
            self.manifests[key] = (compiled, len(encoded))
            self.manifest_bytes += len(encoded)
        return compiled

    def _measure(self, envelope: dict[str, Any]) -> dict[str, Any]:
        payload = envelope["payload"]
        if not isinstance(payload, dict) or set(payload) not in (
            {"subject", "manifestJson", "check", "environmentHash"},
            {"subject", "manifestJson", "check", "environmentHash", "capture"},
        ):
            raise MeasurementProtocolError("unexpected measurement fields")
        subject = _ref(payload["subject"], True)
        manifest_json = _text(payload["manifestJson"])
        manifest = self._manifest(subject, manifest_json)
        check = _object(payload["check"], {"schemaVersion", "ref", "author", "executorId", "supportedSubjects", "parameters", "requiredCapabilities", "timeoutMs"})
        _ref(check["ref"])
        if check["schemaVersion"] != "check-spec-v1" or check["author"] not in {"application", "user", "model", "unknown"}:
            raise MeasurementProtocolError("invalid check source")
        if _text(check["executorId"]) != "python-measurement":
            raise MeasurementProtocolError("measurement executor binding mismatch")
        _integer(check["timeoutMs"], 1)
        if not isinstance(check["supportedSubjects"], list) or subject["kind"] not in check["supportedSubjects"]:
            raise MeasurementProtocolError("check does not support subject")
        if not isinstance(check["requiredCapabilities"], list) or any(op not in {"read", "search", "mutate", "execute", "delegate", "publish"} for op in check["requiredCapabilities"]):
            raise MeasurementProtocolError("invalid check capabilities")
        _digest(payload["environmentHash"])
        params = check["parameters"]
        if not isinstance(params, dict):
            raise MeasurementProtocolError("invalid check parameters")
        started = _now()
        limitations = ["Measurements do not establish that the user's overall goal is satisfied.",
                       "Check author: " + check["author"] + "; execution does not establish test validity or independence.",
                       "Environment identity and dependency manifests are supplied by the authenticated Host."]
        try:
            # Validate every local input, not only the selected file. Never report
            # mismatched bytes as if they belonged to the pinned subject version.
            files = {}
            selected_text = None
            text_error = None
            for path, expected_hash, expected_size in manifest:
                selected = params.get("kind") == "file" and params.get("path") == path
                text = io.StringIO() if selected and params.get("operator") != "sha256" else None
                decoder = codecs.getincrementaldecoder("utf-8")("strict") if text is not None else None
                digest, size = hashlib.sha256(), 0
                for data in iter_snapshot_chunks(self.root, path):
                    size += len(data)
                    digest.update(data)
                    if text is not None:
                        try:
                            text.write(decoder.decode(data))
                        except UnicodeError as error:
                            text_error, text = error, None
                if text is not None:
                    try:
                        text.write(decoder.decode(b"", final=True))
                        selected_text = text.getvalue()
                    except UnicodeError as error:
                        text_error = error
                actual_hash = digest.hexdigest()
                if size != expected_size or actual_hash != expected_hash:
                    raise OSError("SUBJECT_BYTES_CHANGED")
                files[path] = (actual_hash, size)
            if text_error is not None:
                raise text_error
            if params.get("kind") == "file":
                _object(params, {"kind", "path", "operator", "expected"})
                if "capture" in payload or params["path"] not in files or params["operator"] not in {"equals", "contains", "sha256"}:
                    raise MeasurementProtocolError("invalid file measurement")
                if not isinstance(params["expected"], str):
                    raise MeasurementProtocolError("file comparison expected string")
                digest, size = files[params["path"]]
                actual = digest if params["operator"] == "sha256" else selected_text
                result = {"execution": "completed", "findings": [
                    _value("sha256", digest), _value("size", size),
                    _comparison("file_content", params["expected"], actual, "contains" if params["operator"] == "contains" else "equals"),
                ]}
            elif params.get("kind") == "command":
                if "execute" not in check["requiredCapabilities"]:
                    raise MeasurementProtocolError("command check requires execute capability")
                result = self._command(params, payload.get("capture"))
                limitations.append("Process exit and streams were captured by the runtime execution adapter; Python did not independently execute the command.")
                limitations.append("The subject manifest alone does not attest the command's complete input or environment closure.")
            else:
                result = {"execution": "not_run", "reason": "Unsupported registered measurement kind"}
        except (OSError, UnicodeError) as error:
            result = {"execution": "error", "error": {"code": "MEASUREMENT_INPUT_ERROR", "message": str(error)}, "partialFindings": []}
        return {
            "schemaVersion": "observation-v1", "observationId": "observation:" + envelope["id"],
            "requestId": envelope["id"], "runId": self.run_id, "taskId": envelope["scopeId"],
            "subject": subject, "checkRef": check["ref"], "environmentHash": payload["environmentHash"],
            "startedAt": started, "finishedAt": _now(), "producer": {"kind": "verifier", "id": "python-measurement", "revision": "5"},
            "result": result, "artifacts": [], "limitations": limitations,
        }

    def _command(self, params: dict[str, Any], capture: Any) -> dict[str, Any]:
        _object(params, {"kind", "argv", "cwd", "expectedExitCode"})
        if not isinstance(params["argv"], list) or not params["argv"] or not all(isinstance(v, str) for v in params["argv"]):
            raise MeasurementProtocolError("invalid command argv")
        _text(params["argv"][0])
        _text(params["cwd"])
        if type(params["expectedExitCode"]) is not int:
            raise MeasurementProtocolError("invalid expected exit code")
        if capture is None:
            return {"execution": "not_run", "reason": "No admitted process capture supplied"}
        base = {"argv", "cwd", "startedAt", "finishedAt", "stdout", "stderr", "execution"}
        if not isinstance(capture, dict) or capture.get("execution") not in {"completed", "error", "not_run"}:
            raise MeasurementProtocolError("invalid process capture")
        extra = {"completed": {"exitCode"}, "error": {"error"}, "not_run": {"reason"}}[capture["execution"]]
        _object(capture, base | extra)
        if capture["argv"] != params["argv"] or capture["cwd"] != params["cwd"]:
            raise MeasurementProtocolError("command capture binding mismatch")
        if _timestamp(capture["startedAt"]) > _timestamp(capture["finishedAt"]):
            raise MeasurementProtocolError("invalid process capture interval")
        if any(not isinstance(capture[key], str) or len(capture[key].encode("utf-8")) > MAX_BYTES for key in ("stdout", "stderr")):
            raise MeasurementProtocolError("invalid process streams")
        findings = [_value(key, capture[key]) for key in ("argv", "cwd", "startedAt", "finishedAt", "stdout", "stderr")]
        if capture["execution"] == "not_run":
            return {"execution": "not_run", "reason": _text(capture["reason"])}
        if capture["execution"] == "error":
            _object(capture["error"], {"code", "message"})
            _text(capture["error"]["code"])
            if not isinstance(capture["error"]["message"], str):
                raise MeasurementProtocolError("invalid process error")
            return {"execution": "error", "error": capture["error"], "partialFindings": findings}
        if type(capture["exitCode"]) is not int:
            raise MeasurementProtocolError("invalid process exit code")
        findings.append(_comparison("exit_code", params["expectedExitCode"], capture["exitCode"]))
        return {"execution": "completed", "findings": findings}
