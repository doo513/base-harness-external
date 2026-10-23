"""Disposable Host-side syntax cache. Never imported by the verification engine."""
import copy
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile

from .scanner import AnalysisError, EXCLUDED, regular_bytes

MAX_CACHE_BYTES = 8 * 1024 * 1024
MAX_DIRECTORY_BYTES = 64 * 1024 * 1024


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def fingerprint(info):
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_mode, info.st_nlink]


def unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate cache key")
        value[key] = item
    return value


def valid_parsed(value, path):
    if not isinstance(value, list) or len(value) != 3:
        return False
    module, imports, diagnostics = value
    if not isinstance(module, dict) or module.get("path") != path or not isinstance(imports, list) or not isinstance(diagnostics, list):
        return False
    if any(type(module.get(key)) is not bool for key in ("package", "parsed", "is_test", "entrypoint_candidate")):
        return False
    name = module.get("module")
    if name is not None and (not isinstance(name, str) or not all(p.isidentifier() for p in name.split("."))):
        return False
    if not isinstance(module.get("sha256"), str) or len(module["sha256"]) != 64 or type(module.get("size")) is not int or module["size"] < 0:
        return False
    symbols = module.get("symbols")
    if not isinstance(symbols, list) or len(symbols) > 100 or any(not isinstance(s, dict) or not isinstance(s.get("name"), str)
            or s.get("kind") not in {"FunctionDef", "AsyncFunctionDef", "ClassDef"} or type(s.get("line")) is not int for s in symbols):
        return False
    if len(imports) > 100000 or any(not isinstance(d, dict) or d.get("source") != path
            or d.get("kind") not in {"import", "from"} or not isinstance(d.get("module"), str)
            or not isinstance(d.get("names"), list) or any(not isinstance(n, str) for n in d["names"])
            or type(d.get("level")) is not int or d["level"] < 0 or type(d.get("line")) is not int for d in imports):
        return False
    return len(diagnostics) <= 100000 and all(isinstance(d, dict) and d.get("path") == path
        and d.get("code") in {"PARSE_ERROR", "DYNAMIC_IMPORT_CANDIDATE"}
        and (d.get("line") is None or type(d["line"]) is int) for d in diagnostics)


class DiscoveryCache:
    def __init__(self, root, directory, settings, *, refresh=False):
        self.root = Path(root).expanduser().absolute()
        self.directory = Path(directory).expanduser().absolute()
        if not self.root.is_dir():
            raise AnalysisError("Project path must be an existing directory")
        for value in (self.root, self.directory):
            if any(p.is_symlink() or (p.exists() and getattr(p.lstat(), "st_file_attributes", 0) & 0x400) for p in (value, *value.parents)):
                raise AnalysisError("Source/cache paths may not contain links or reparse points")
        if self.directory.is_relative_to(self.root) or self.root.is_relative_to(self.directory):
            raise AnalysisError("Exploration cache must be outside and independent of the source directory")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = self.directory.stat()
        if os.name == "posix" and (info.st_uid != os.getuid() or info.st_mode & 0o022):
            raise AnalysisError("Use a caller-owned cache directory without group/other write access")
        self.identity = {"schema_version": "change-lens-cache-v1", "workspace": str(self.root.resolve()),
                         "python": [sys.implementation.name, *sys.version_info[:3]], "settings": settings,
                         "excluded": sorted(EXCLUDED), "parser": hashlib.sha256(Path(__file__).with_name("parser.py").read_bytes()).hexdigest()}
        self.path = self.directory / ("index-" + digest(self.identity) + ".json")
        self.entries, self.next = {}, {}
        self.refresh = refresh
        self.stats = {"reused_files": 0, "metadata_reused_files": 0, "content_reused_files": 0,
                      "parsed_files": 0, "read_status": "absent", "write_status": "not_written", "verification_eligible": False}
        self._safe_target()
        if self.path.exists() and not refresh:
            try:
                def invalid(value):
                    raise ValueError("Nonfinite cache value")
                body = json.loads(regular_bytes(self.path, MAX_CACHE_BYTES).decode("utf-8"), object_pairs_hook=unique, parse_constant=invalid)
                if body.get("identity") != self.identity or not isinstance(body.get("entries"), dict) or len(body["entries"]) > 5000:
                    raise ValueError("Cache identity or inventory mismatch")
                for path, entry in body["entries"].items():
                    if not isinstance(entry, dict) or set(entry) != {"fingerprint", "parsed", "digest"} or entry["digest"] != digest({k: entry[k] for k in ("fingerprint", "parsed")}):
                        raise ValueError("Invalid cache record")
                    if not isinstance(entry["fingerprint"], list) or len(entry["fingerprint"]) != 7 or any(type(n) is not int for n in entry["fingerprint"]) or not valid_parsed(entry["parsed"], path):
                        raise ValueError("Invalid cached facts")
                self.entries = body["entries"]
                self.stats["read_status"] = "loaded"
            except (ValueError, TypeError, KeyError, AttributeError, RecursionError, UnicodeError, AnalysisError):
                self.stats["read_status"] = "invalid_rebuilt"

    def _safe_target(self):
        if self.path.exists() or self.path.is_symlink():
            value = self.path.lstat()
            if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1 or (os.name == "posix" and value.st_uid != os.getuid()):
                raise AnalysisError("Cache entry must be a caller-owned single-link regular file")

    def get(self, path, info):
        value = self.entries.get(path)
        if self.refresh or value is None or value["fingerprint"] != fingerprint(info):
            return None
        self.stats["reused_files"] += 1
        self.stats["metadata_reused_files"] += 1
        return copy.deepcopy(value["parsed"])

    def content(self, path, sha256):
        value = self.entries.get(path)
        if self.refresh or value is None or value["parsed"][0]["sha256"] != sha256:
            return None
        self.stats["reused_files"] += 1
        self.stats["content_reused_files"] += 1
        return copy.deepcopy(value["parsed"])

    def remember(self, file, parsed):
        old = self.entries.get(file["path"])
        facts = list(parsed)
        if old is not None and old["fingerprint"] == file["fingerprint"] and old["parsed"] == facts:
            self.next[file["path"]] = old
            return
        value = {"fingerprint": file["fingerprint"], "parsed": copy.deepcopy(facts)}
        self.next[file["path"]] = {**value, "digest": digest(value)}

    def save(self):
        self.stats["freshness"] = "filesystem_metadata_only" if self.stats["metadata_reused_files"] else "content_read"
        self._safe_target()
        if self.next == self.entries and self.stats["read_status"] == "loaded":
            self.stats["write_status"] = "unchanged"
            return
        body = canonical({"identity": self.identity, "entries": self.next})
        entries = list(self.directory.glob("index-*.json"))
        size = sum(p.lstat().st_size for p in entries if p != self.path)
        if len(body) > MAX_CACHE_BYTES or len(self.next) > 5000 or len(entries) >= 128 and not self.path.exists() or size + len(body) > MAX_DIRECTORY_BYTES:
            self.stats["write_status"] = "not_saved_size_limit"
            return
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.directory, prefix=".index-", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            self._safe_target()
            os.replace(temporary, self.path)
            temporary = None
            self.stats["write_status"] = "saved"
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
