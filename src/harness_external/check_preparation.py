"""Framework-neutral input/schema bridge, injected by application composition.

The Domain defines criteria. Registered adapters own selector/reference syntax.
These callbacks only normalize data; they do not inspect or execute a Candidate.
"""
import copy
import hashlib
import inspect
from pathlib import Path
from typing import Protocol

from harness.common import canonical_bytes, canonical_hash
from .errors import fields, relative_path, require
from .registry import code_identity, effective_config


class CheckPreparationPort(Protocol):
    def identity(self) -> dict: ...
    def parameters(self, parameters: dict) -> dict: ...
    def selection(self, adapter_id: str, selector: dict, inputs: list, required_cases: list) -> dict: ...
    def reference(self, adapter: dict, reference: dict) -> None: ...


class AdapterCheckPreparation:
    def __init__(self, adapters=None):
        self.adapters = adapters

    def identity(self):
        methods, sources = {}, {}
        for name in ("parameters", "selection", "reference"):
            method = getattr(self, name)
            target = getattr(method, "__func__", method)
            source = inspect.getsourcefile(target)
            require(hasattr(target, "__code__") and source and Path(source).is_file(),
                    "DOMAIN_IDENTITY_UNAVAILABLE", "Preparation methods must expose implementation identity")
            methods[name] = canonical_hash(code_identity(target.__code__))
            sources[name] = hashlib.sha256(Path(source).read_bytes()).hexdigest()
        # Registry instances contain runtime objects. Used adapter implementations
        # are pinned separately; alias dispatch is part of this input bridge.
        return {"schema_version": "check-preparation-identity-v1", "methods": methods, "sources": sources,
                "configuration": effective_config(self, exclude={"adapters"}),
                "parameter_aliases": self.adapters.parameter_aliases() if self.adapters is not None else {}}

    def parameters(self, parameters):
        require(isinstance(parameters, dict), "INVALID_PARAMETERS", "Parameters must be an object")
        result = copy.deepcopy(parameters)
        legacy = []
        converted = False
        if "test_commands" in result:
            commands = result.pop("test_commands")
            require(isinstance(commands, list) and len(commands) <= 8, "INVALID_PARAMETERS", "At most eight legacy test commands are supported")
            for item in commands:
                require(isinstance(item, dict) and "kind" not in item, "INVALID_PARAMETERS", "Invalid legacy test command")
                legacy.append({"kind": "command", **item})
            converted = True
        if self.adapters is not None:
            for alias, adapter_id in self.adapters.parameter_aliases().items():
                if alias not in result:
                    continue
                values = result.pop(alias)
                require(isinstance(values, list) and len(values) <= 8, "INVALID_PARAMETERS", "Invalid legacy execution checks")
                method = getattr(self.adapters.resolve(adapter_id), "legacy_execution_check", None)
                require(callable(method), "ADAPTER_SCHEMA_UNAVAILABLE", "Adapter has no declared input converter")
                for value in values:
                    item = method(copy.deepcopy(value))
                    require(isinstance(item, dict) and item.get("kind") == "cases" and item.get("adapter_id") == adapter_id,
                            "ADAPTER_SCHEMA_INVALID", "Legacy converter changed its adapter selection")
                    legacy.append(item)
                converted = True
        if converted:
            require("execution_checks" not in result or not legacy, "INVALID_PARAMETERS", "Use execution_checks or legacy aliases, not both")
            result.setdefault("execution_checks", legacy)
        return result

    def selection(self, adapter_id, selector, inputs, required_cases):
        require(self.adapters is not None, "ADAPTER_SCHEMA_UNAVAILABLE", "Inject an adapter schema port for case-aware checks")
        method = getattr(self.adapters.resolve(adapter_id), "normalize_selection", None)
        require(callable(method), "ADAPTER_SCHEMA_UNAVAILABLE", "Selected adapter does not provide a case schema")
        selected = method(copy.deepcopy(selector), list(inputs), list(required_cases))
        fields(selected, {"selector", "source_paths"}, {"selector", "source_paths"})
        require(isinstance(selected["selector"], dict) and len(canonical_bytes(selected["selector"])) <= 64000,
                "ADAPTER_SCHEMA_INVALID", "Selector must be a bounded object")
        paths = selected["source_paths"]
        require(isinstance(paths, list) and len(paths) <= 128 and all(isinstance(p, str) for p in paths)
                and len(set(paths)) == len(paths), "ADAPTER_SCHEMA_INVALID", "Source paths must be a bounded unique list")
        for path in paths:
            relative_path(path)
            require(path in inputs, "CHECK_SCOPE_CONFLICT", "Case source is outside the admitted input scope")
        return copy.deepcopy(selected)

    def reference(self, adapter, reference):
        require(self.adapters is not None, "ADAPTER_SCHEMA_UNAVAILABLE", "Inject an adapter schema port for case references")
        method = getattr(self.adapters.resolve(adapter["id"]), "validate_reference", None)
        require(callable(method), "ADAPTER_SCHEMA_UNAVAILABLE", "Selected adapter cannot validate case references")
        method(copy.deepcopy(adapter["selector"]), copy.deepcopy(reference))
