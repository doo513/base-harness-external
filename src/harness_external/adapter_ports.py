"""Application-owned verifier ports. Adapters normalize facts, never decide gates.

Candidate commands are launched only by the common strict Sandbox transport.
An adapter stages trusted resources and describes a command; it is not given a
Run database, model client, checkpoint authority or an alternate process runner.
"""
from pathlib import Path
from typing import Protocol

from .errors import HarnessError


class AdapterUnavailable(HarnessError):
    def __init__(self, reason):
        super().__init__("ADAPTER_RUNTIME_UNAVAILABLE", reason)


class CaseObserver(Protocol):
    def feed(self, event: dict) -> dict | None: ...
    def finish(self, error: str | None = None) -> dict: ...


class ExecutionAdapter(Protocol):
    adapter_id: str
    revision: str
    report_path: str

    def runtime(self, state_root: Path, pinned: dict | None = None) -> dict: ...
    def command(self, check: dict) -> dict: ...
    def stage(self, state_root: Path, runtime: dict, destination: Path, token: str) -> None: ...
    def observer(self, token: str, runtime: dict) -> CaseObserver: ...
