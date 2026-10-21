"""Shared input and path validation, independent of domain semantics."""
from __future__ import annotations

import re
from typing import Any
from harness.common import canonical_hash


# Reference existence checks and their advertised schemas share this bound.
# This is not a record-page size or a Domain's Check-admission limit.
OBSERVATION_REFERENCE_LIMIT = 128


class HarnessError(ValueError):
    def __init__(self, code: str, message: str, *, diagnostics=(), diagnostic_count=None):
        self.code = code
        self.diagnostics = tuple(dict(item) for item in diagnostics)
        self.diagnostic_count = len(self.diagnostics) if diagnostic_count is None else diagnostic_count
        super().__init__(message)

    def as_dict(self):
        result = {"code": self.code, "message": str(self)[:2000]}
        if self.diagnostics:
            result.update(diagnostics=[dict(item) for item in self.diagnostics],
                          diagnostic_count=self.diagnostic_count,
                          diagnostics_truncated=self.diagnostic_count > len(self.diagnostics))
        return result


class ValidationIssues:
    """Collect independent, data-only validation failures; never run side effects.

    The first error code remains compatible. Diagnostics carry bounded JSON
    pointers and no copies of supplied values. Authority/lifecycle checks still
    use require() and stop immediately.
    """
    LIMIT = 64

    def __init__(self):
        self.items = []
        self.count = 0
        self.first_code = None
        self.first_message = None

    def add(self, path, code, message):
        self.count += 1
        if self.first_code is None:
            self.first_code, self.first_message = code, message
        if len(self.items) < self.LIMIT:
            self.items.append({"path": path[:1024], "code": code, "message": message[:2000]})

    def check(self, path, condition, code, message):
        if not condition:
            self.add(path, code, message)
        return bool(condition)

    def capture(self, path, operation):
        try:
            return operation()
        except HarnessError as error:
            if self.first_code is None:
                self.first_code, self.first_message = error.code, str(error)
            if error.diagnostics:
                for item in error.diagnostics:
                    self.add(path + item["path"], item["code"], item["message"])
                self.count += error.diagnostic_count - len(error.diagnostics)
            else:
                self.add(path, error.code, str(error))
            return None

    def raise_if_any(self, *, code=None, message=None):
        if self.count:
            raise HarnessError(code or self.first_code, message or self.first_message,
                               diagnostics=self.items, diagnostic_count=self.count)


def require(condition: bool, code: str, message: str) -> None:
    if not condition:
        raise HarnessError(code, message)


def fields(value: Any, allowed: set[str], required: set[str] | None = None) -> dict:
    require(isinstance(value, dict) and all(isinstance(key, str) for key in value),
            "INVALID_PARAMETERS", "Expected an object with string field names")
    issues = ValidationIssues()
    pointer = lambda key: "/" + key.replace("~", "~0").replace("/", "~1")
    for key in sorted((required or set()) - value.keys()):
        issues.add(pointer(key), "FIELD_REQUIRED", "Required field is missing")
    for key in sorted(value.keys() - allowed):
        issues.add(pointer(key), "FIELD_UNEXPECTED", "Field is not supported by this operation")
    issues.raise_if_any(code="INVALID_PARAMETERS", message="Unexpected or missing object fields")
    return value


def relative_path(value: Any, *, directory: bool = False) -> str:
    if directory and value == ".":
        return value
    require(isinstance(value, str) and 0 < len(value) <= 512, "INVALID_PATH", "A relative path is required")
    parts = value.split("/")
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    require(not re.search(r"[\x00-\x1f\x7f\\:]", value) and all(
        part not in {"", ".", ".."} and not part.endswith((" ", ".")) and part.split(".")[0].upper() not in reserved
        for part in parts), "INVALID_PATH", "Paths must stay inside the explicit workspace (no links, devices or traversal)")
    return value


def integer(value: Any, low: int, high: int, name: str) -> int:
    require(type(value) is int and low <= value <= high, "INVALID_PARAMETERS", f"{name} must be {low}..{high}")
    return value


def limits(value: dict | None) -> dict:
    value = fields({} if value is None else value, {"max_actions", "max_verifications", "timeout_seconds"})
    return {"max_actions": integer(value.get("max_actions", 50), 1, 1000, "max_actions"),
            "max_verifications": integer(value.get("max_verifications", 3), 1, 20, "max_verifications"),
            "timeout_seconds": integer(value.get("timeout_seconds", 3600), 10, 86400, "timeout_seconds")}


def validate_contract(contract: dict) -> None:
    require(isinstance(contract, dict), "CONTRACT_CORRUPT", "Invalid contract record")
    body = {key: value for key, value in contract.items() if key != "contract_hash"}
    require(contract.get("contract_hash") == canonical_hash(body), "CONTRACT_CORRUPT", "Contract digest mismatch")
