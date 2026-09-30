"""Shared input and path validation, independent of domain semantics."""
from __future__ import annotations

import re
from typing import Any
from harness.common import canonical_hash


class HarnessError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def require(condition: bool, code: str, message: str) -> None:
    if not condition:
        raise HarnessError(code, message)


def fields(value: Any, allowed: set[str], required: set[str] | None = None) -> dict:
    require(isinstance(value, dict) and set(value) <= allowed and (required or set()) <= set(value),
            "INVALID_PARAMETERS", "Unexpected or missing object fields")
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
