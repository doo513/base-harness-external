"""Pytest runtime and event normalization outside the framework-agnostic Core."""
from importlib import metadata
from functools import lru_cache
import io
import math
import hashlib
import json
from pathlib import Path
import shutil
import zipfile

from harness.common import canonical_bytes, canonical_hash
from .errors import HarnessError, fields, integer, relative_path, require
from .adapter_ports import AdapterUnavailable
from .snapshots import no_links

ADAPTER_ID = "pytest-cases-v1"
MAX_EVENTS = 30000
MAX_REPORT_BYTES = 8 * 1024 * 1024
REPORT_PATH = ".harness-cases.jsonl"
RESOURCE_MOUNT = "/opt/harness-runtime"


def node_id(value):
    require(isinstance(value, str) and 0 < len(value.encode("utf-8")) <= 4096 and "\x00" not in value and "\n" not in value
            and "::" in value, "PYTEST_CASE_ID", "Use an exact pytest node ID")
    relative_path(value.split("::", 1)[0])
    return value


def normalize_selection(selector, inputs, required_cases):
    fields(selector, {"paths", "args"}, {"paths"})
    paths, args = selector["paths"], selector.get("args", [])
    require(isinstance(paths, list) and 1 <= len(paths) <= 32 and all(isinstance(p, str) for p in paths)
            and len(set(paths)) == len(paths), "PYTEST_SCOPE", "Declare unique test files")
    for path in paths:
        relative_path(path)
        require(path in inputs, "PYTEST_SCOPE", "Pytest collection files must be declared inputs")
    require(isinstance(args, list) and len(args) <= 32 and all(isinstance(v, str) and len(v) <= 2000 and "\x00" not in v for v in args),
            "PYTEST_ARGS", "Invalid pytest arguments")
    pending = False
    for argument in args:
        if pending:
            require(bool(argument), "PYTEST_ARGS", "Missing selector value")
            pending = False
        elif argument in {"-k", "-m", "--maxfail"}:
            pending = True
        else:
            require(argument in {"-q", "-v", "-x", "--strict-markers", "--collect-only"}
                    or argument.startswith(("--maxfail=", "-k=", "-m=")), "PYTEST_ARGS", "Unsupported pytest option: " + argument)
    require(not pending, "PYTEST_ARGS", "Missing selector value")
    for item in required_cases:
        node_id(item)
        require(item.split("::", 1)[0] in paths, "PYTEST_SCOPE", "Required case is outside collection scope")
    return {"selector": {"paths": list(paths), "args": list(args)}, "source_paths": list(paths)}


def build_command(check):
    """Materialize an approved semantic Check into this adapter's command.

    The Domain does not choose the interpreter, runner, mount layout or output
    file. The Worker binds the actual receipt to this deterministic command.
    """
    fields(check, {"kind", "adapter", "expectedExitCode", "timeout_seconds", "check_key", "check_id"}, {"kind", "adapter"})
    adapter = check["adapter"]
    if isinstance(adapter, dict) and "selector" in adapter:
        fields(adapter, {"id", "selector", "source_paths", "rules"}, {"id", "selector", "source_paths", "rules"})
        selection = normalize_selection(adapter["selector"], adapter["source_paths"], adapter["rules"]["required_case_ids"])
        require(selection["source_paths"] == adapter["source_paths"], "ADAPTER_COMMAND", "Collection scope changed")
    else:
        # Existing application-defined Domains can still emit the flat v1 envelope.
        fields(adapter, {"id", "paths", "args", "rules"}, {"id", "paths", "args", "rules"})
        selection = normalize_selection({"paths": adapter["paths"], "args": adapter["args"]}, adapter["paths"], adapter["rules"]["required_case_ids"])
    require(check["kind"] == "command" and adapter["id"] == ADAPTER_ID, "ADAPTER_UNREGISTERED", "Unsupported case adapter")
    paths, args = selection["selector"]["paths"], selection["selector"]["args"]
    return {"kind": "command", "argv": ["python3", "-I", RESOURCE_MOUNT + "/runner.py", RESOURCE_MOUNT + "/packages.zip",
                                          REPORT_PATH, json.dumps({"paths": paths, "args": args}, ensure_ascii=False, separators=(",", ":"))],
            "cwd": ".", "timeout_seconds": integer(check.get("timeout_seconds", 30), 1, 120, "timeout_seconds"), "expectedExitCode": 0}


@lru_cache(maxsize=4)
def _package_snapshot(versions_tuple, runner_hash):
    distributions = {name: metadata.distribution(name) for name, _ in versions_tuple}
    files = {}
    for distribution in distributions.values():
        package_root = Path(distribution.locate_file("")).resolve()
        no_links(package_root)
        for item in distribution.files or []:
            name = str(item).replace("\\", "/")
            if name.startswith("../") or name.endswith((".pyc", ".pyo")) or "__pycache__" in name:
                continue
            source = Path(distribution.locate_file(item))
            if source.is_file():
                require(not source.is_symlink() and source.resolve().is_relative_to(package_root), "ADAPTER_RUNTIME_CHANGED", "Runtime file escaped its installation")
                files[name] = source.read_bytes()
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as result:
        for name, body in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            result.writestr(info, body)
    return canonical_hash({name: hashlib.sha256(body).hexdigest() for name, body in sorted(files.items())}), stream.getvalue()


def load_runtime(state_root, identity):
    key = identity.get("artifact_key")
    require(isinstance(key, str) and len(key) == 64 and all(c in "0123456789abcdef" for c in key), "ADAPTER_RUNTIME_CHANGED", "Invalid runtime artifact key")
    directory = Path(state_root) / "adapter-runtimes" / key
    no_links(directory)
    archive = directory / "packages.zip"
    launcher = directory / "runner.py"
    no_links(archive)
    no_links(launcher)
    require(archive.is_file() and launcher.is_file() and archive.stat().st_size <= 10 * 1024 * 1024
            and hashlib.sha256(archive.read_bytes()).hexdigest() == identity["runtime_hash"]
            and hashlib.sha256(launcher.read_bytes()).hexdigest() == identity["runner_hash"],
            "ADAPTER_RUNTIME_CHANGED", "Pinned runtime artifact bytes changed")
    require(identity["identity_hash"] == canonical_hash({k: v for k, v in identity.items() if k != "identity_hash"}),
            "ADAPTER_RUNTIME_CHANGED", "Runtime identity changed")
    return {"identity": identity, "directory": str(directory)}


def prepare_runtime(state_root, *, refresh=False):
    """Copy installed pure-Python packages into a digest-addressed runtime.

    No pip, network access, Candidate imports or Core runtime dependency. Missing
    optional adapter packages produce an explicit unavailable adapter.
    """
    try:
        from packaging.requirements import Requirement
        pending, distributions = ["pytest"], {}
        while pending:
            name = pending.pop()
            if name in distributions:
                continue
            distribution = metadata.distribution(name)
            distributions[name] = distribution
            for value in distribution.requires or []:
                requirement = Requirement(value)
                if requirement.marker is None or requirement.marker.evaluate({"extra": ""}):
                    pending.append(requirement.name)
    except (ImportError, metadata.PackageNotFoundError) as error:
        require(False, "PYTEST_RUNTIME_UNAVAILABLE", "Install the pytest adapter dependencies in the selected Harness environment: " + str(error))
    runner = Path(__file__).with_name("pytest_runner.py").read_bytes()
    versions = {name: distribution.version for name, distribution in sorted(distributions.items())}
    runner_hash = hashlib.sha256(runner).hexdigest()
    root = Path(state_root) / "adapter-runtimes"
    no_links(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    selection = root / (canonical_hash({"versions": versions, "runner_hash": runner_hash}) + ".json")
    no_links(selection)
    if selection.is_file() and not refresh:
        return load_runtime(state_root, json.loads(selection.read_text(encoding="utf-8")))
    if refresh:
        _package_snapshot.cache_clear()
    package_hash, archive_bytes = _package_snapshot(tuple(versions.items()), runner_hash)
    identity = {"id": ADAPTER_ID, "revision": "1", "versions": versions,
                "runner_hash": runner_hash, "package_hash": package_hash}
    digest = canonical_hash(identity)
    directory = Path(state_root) / "adapter-runtimes" / digest
    no_links(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    archive = directory / "packages.zip"
    launcher = directory / "runner.py"
    # Immutable-by-protocol content; same-user tamper resistance is not claimed.
    if not archive.exists():
        import tempfile
        with tempfile.NamedTemporaryFile(dir=directory, suffix=".zip", delete=False) as stream:
            temporary = Path(stream.name)
        try:
            temporary.write_bytes(archive_bytes)
            temporary.replace(archive)
        finally:
            temporary.unlink(missing_ok=True)
    if not launcher.exists():
        import tempfile
        with tempfile.NamedTemporaryFile(dir=directory, delete=False) as stream:
            temporary_launcher = Path(stream.name)
            stream.write(runner)
        temporary_launcher.replace(launcher)
    require(launcher.read_bytes() == runner, "ADAPTER_RUNTIME_CHANGED", "Pytest runner bytes changed")
    identity.update(runtime_hash=hashlib.sha256(archive_bytes).hexdigest(), artifact_key=digest)
    identity["identity_hash"] = canonical_hash(identity)
    result = load_runtime(state_root, identity)
    from harness.common import atomic_json
    atomic_json(selection, identity)
    return result


class Collector:
    """Normalize a current-session event stream; preserve incomplete knowledge."""
    def __init__(self, token, expected_version=None):
        self.token = token
        self.sequence = 0
        self.cases = {}
        self.collection_complete = False
        self.collection_finished = False
        self.collection_issues = []
        self.session_complete = False
        self.exit_status = None
        self.framework_version = None
        self.protocol_error = None
        self.expected_version = expected_version

    def feed(self, event):
        require(isinstance(event, dict) and event.get("version") == 1 and event.get("token") == self.token
                and event.get("seq") == self.sequence + 1 and self.sequence < MAX_EVENTS,
                "CASE_REPORT_PROTOCOL", "Invalid, stale or out-of-order pytest event")
        require(not self.session_complete, "CASE_REPORT_PROTOCOL", "Events arrived after session completion")
        require(self.sequence > 0 or event.get("event") == "session_start", "CASE_REPORT_PROTOCOL", "Session start must precede observations")
        self.sequence += 1
        kind = event.get("event")
        if kind == "session_start":
            require(self.sequence == 1 and event.get("framework") == "pytest", "CASE_REPORT_PROTOCOL", "Missing session start")
            self.framework_version = event["framework_version"]
            require(self.expected_version is None or self.framework_version == self.expected_version,
                    "ADAPTER_RUNTIME_CHANGED", "Reporter framework version differs from pinned runtime")
        elif kind == "discovered":
            require(not self.collection_finished, "CASE_REPORT_PROTOCOL", "Discovery arrived after collection finished")
            case_id = event["case_id"]
            require(isinstance(case_id, str) and 0 < len(case_id) <= 4096 and case_id not in self.cases,
                    "CASE_REPORT_PROTOCOL", "Duplicate/invalid pytest node ID")
            self.cases[case_id] = {"framework": "pytest", "case_id": case_id, "path": event["path"],
                                  "discovered": True, "selected": None, "started": False, "executed": False,
                                  "finished": False, "outcome": "not_run", "phases": [], "duration_seconds": 0.0}
        elif kind == "collection_issue":
            self.collection_issues.append({key: event.get(key) for key in ("node_id", "outcome", "message")})
        elif kind == "collection_finish":
            require(not self.collection_finished and not any(c["started"] for c in self.cases.values()), "CASE_REPORT_PROTOCOL", "Repeated/late collection")
            selected = event["selected"]
            require(isinstance(selected, list) and all(isinstance(x, str) for x in selected)
                    and len(set(selected)) == len(selected) and set(selected) <= self.cases.keys()
                    and type(event.get("errors")) is int and event["errors"] >= 0,
                    "CASE_REPORT_PROTOCOL", "Selection references unknown/duplicate cases")
            self.collection_finished = True
            self.collection_complete = event["errors"] == 0 and not self.collection_issues
            for case in self.cases.values():
                case["selected"] = case["case_id"] in selected
                if not case["selected"]:
                    case["outcome"] = "not_selected"
        elif kind == "deselected":
            self._case(event)["selected"] = False
            self._case(event)["outcome"] = "not_selected"
        elif kind == "started":
            case = self._case(event)
            require(case["selected"] is True and not case["started"] and not case["finished"], "CASE_REPORT_PROTOCOL", "Case started twice or was not selected")
            case["started"] = True
        elif kind == "call_started":
            case = self._case(event)
            require(case["started"] and case["selected"] and not case["executed"] and not case["finished"],
                    "CASE_REPORT_PROTOCOL", "Invalid call-phase start")
            case["executed"] = True
        elif kind == "phase":
            case = self._case(event)
            phase = event["phase"]
            require(phase in {"setup", "call", "teardown"} and event["outcome"] in {"passed", "failed", "skipped"}
                    and all(item["phase"] != phase for item in case["phases"]) and case["started"] and not case["finished"]
                    and (event.get("wasxfail") is None or isinstance(event["wasxfail"], str))
                    and type(event.get("duration")) in (int, float) and math.isfinite(event["duration"]) and event["duration"] >= 0,
                    "CASE_REPORT_PROTOCOL", "Invalid or duplicate phase report")
            case["phases"].append({key: event.get(key) for key in ("phase", "outcome", "wasxfail", "message", "duration")})
            case["executed"] = case["executed"] or phase == "call"
            case["duration_seconds"] += event["duration"]
        elif kind == "finished":
            case = self._case(event)
            require(not case["finished"], "CASE_REPORT_PROTOCOL", "Duplicate case completion")
            case["finished"] = True
            case["outcome"] = self.outcome(case)
            return dict(case)
        elif kind == "session_finish":
            require(type(event.get("exit_status")) is int and 0 <= event["exit_status"] <= 5, "CASE_REPORT_PROTOCOL", "Invalid session exit status")
            self.session_complete = True
            self.exit_status = event["exit_status"]
        else:
            require(False, "CASE_REPORT_PROTOCOL", "Unknown pytest event")
        return None

    def _case(self, event):
        require(event.get("case_id") in self.cases, "CASE_REPORT_PROTOCOL", "Event for undiscovered case")
        return self.cases[event["case_id"]]

    @staticmethod
    def outcome(case):
        phases = case["phases"]
        if not {"setup", "teardown"} <= {p["phase"] for p in phases}:
            return "incomplete"
        if any(p["outcome"] == "failed" and p["phase"] in {"setup", "teardown"} for p in phases):
            return "error"
        if any(p["phase"] == "call" and p["outcome"] == "failed" and str(p.get("message") or "").startswith("[XPASS(strict)]") for p in phases):
            return "xpass"
        if any(p["wasxfail"] is not None for p in phases):
            return "xpass" if any(p["phase"] == "call" and p["outcome"] == "passed" for p in phases) else "xfail"
        if any(p["outcome"] == "failed" for p in phases):
            return "failed"
        if any(p["outcome"] == "skipped" for p in phases):
            return "skipped"
        if case["executed"] and any(p["phase"] == "teardown" and p["outcome"] == "passed" for p in phases):
            return "passed"
        return "incomplete"

    def summary(self):
        return {"framework": "pytest", "framework_version": self.framework_version,
                "collection_complete": self.collection_complete, "session_complete": self.session_complete,
                "exit_status": self.exit_status, "collection_issues": self.collection_issues,
                "cases": list(self.cases.values()), "event_count": self.sequence,
                "case_scope_hash": canonical_hash({"discovered": sorted(self.cases),
                    "selected": sorted(c["case_id"] for c in self.cases.values() if c["selected"])}) if self.collection_complete else None}

    def finish(self, error=None):
        if error:
            self.protocol_error = error
            self.session_complete = False
        for case in self.cases.values():
            if case["selected"] and not case["finished"]:
                case["outcome"] = "incomplete" if case["started"] else "not_run"
        return {**self.summary(), "protocol_error": self.protocol_error}


class PytestAdapter:
    adapter_id = ADAPTER_ID
    revision = "3"
    report_path = REPORT_PATH
    identity_files = (str(Path(__file__).with_name("pytest_runner.py")),)
    identity_file_ids = ("pytest-runner",)

    def legacy_execution_check(self, value):
        fields(value, {"id", "paths", "args", "required_cases", "allowed_outcomes", "timeout_seconds"}, {"id", "paths"})
        return {"kind": "cases", "adapter_id": self.adapter_id,
                "selector": {"paths": value["paths"], "args": value.get("args", [])},
                **{key: value[key] for key in ("id", "required_cases", "allowed_outcomes", "timeout_seconds") if key in value}}

    def normalize_selection(self, selector, inputs, required_cases):
        return normalize_selection(selector, inputs, required_cases)

    def validate_reference(self, selector, reference):
        case = node_id(reference["case_id"])
        require(reference.get("framework", "pytest") == "pytest" and case.split("::", 1)[0] == reference["path"]
                and reference["path"] in selector["paths"], "COVERAGE_TEST_REFERENCE", "Pytest reference must match its declared collection file/check")

    def runtime(self, state_root, pinned=None):
        if pinned is not None:
            return load_runtime(state_root, pinned)["identity"]
        try:
            return prepare_runtime(state_root)["identity"]
        except HarnessError as error:
            if error.code != "PYTEST_RUNTIME_UNAVAILABLE":
                raise
            raise AdapterUnavailable(error.code) from error

    def command(self, check):
        return build_command(check)

    def stage(self, state_root, runtime, destination, token):
        loaded = load_runtime(state_root, runtime)
        for name in ("runner.py", "packages.zip"):
            shutil.copyfile(Path(loaded["directory"]) / name, destination / name)
        require(hashlib.sha256((destination / "packages.zip").read_bytes()).hexdigest() == runtime["runtime_hash"]
                and hashlib.sha256((destination / "runner.py").read_bytes()).hexdigest() == runtime["runner_hash"],
                "ADAPTER_RUNTIME_CHANGED", "Runtime changed while preparing resources")
        (destination / "capture.json").write_bytes(canonical_bytes({"token": token}))

    def observer(self, token, runtime):
        return Collector(token, runtime["versions"]["pytest"])
