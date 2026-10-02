"""Explicit Domain port. The core does not choose semantic success criteria."""
from __future__ import annotations

from typing import Protocol
import hashlib
import inspect
from types import CodeType
from pathlib import Path

from harness.common import canonical_hash

from .errors import require


def code_identity(value):
    # marshal includes object-sharing/interning details; it is not a stable
    # cross-process implementation digest. Hash semantic code attributes instead.
    if isinstance(value, CodeType):
        return {"bytecode": value.co_code.hex(), "constants": [code_identity(c) for c in value.co_consts],
                "names": value.co_names, "variables": value.co_varnames, "free": value.co_freevars,
                "cells": value.co_cellvars, "flags": value.co_flags, "args": value.co_argcount,
                "posonly": value.co_posonlyargcount, "kwonly": value.co_kwonlyargcount,
                "exceptions": getattr(value, "co_exceptiontable", b"").hex()}
    if isinstance(value, (tuple, frozenset)):
        items = [code_identity(c) for c in value]
        return {type(value).__name__: sorted(items, key=canonical_hash) if isinstance(value, frozenset) else items}
    return {"type": type(value).__name__, "value": repr(value)}


class DomainModule(Protocol):
    domain_id: str
    revision: str

    def prepare(self, goal: str, parameters: dict, verifier: dict, *, exploratory: bool, intent: dict | None = None) -> dict: ...
    def normalize_check(self, parameters: dict, contract: dict) -> dict: ...


class DomainRegistry:
    def __init__(self, modules):
        self._modules = {}
        for module in modules:
            require(isinstance(module.domain_id, str) and isinstance(module.revision, str) and bool(module.revision)
                    and callable(module.prepare) and callable(module.normalize_check), "DOMAIN_MODULE_INVALID", "Invalid Domain module")
            require(module.domain_id not in self._modules, "DOMAIN_MODULE_DUPLICATE", "Duplicate Domain registration")
            self._modules[module.domain_id] = module

    def resolve(self, domain_id: str):
        require(isinstance(domain_id, str) and domain_id in self._modules, "DOMAIN_UNSUPPORTED", "Domain module is not registered")
        return self._modules[domain_id]

    def identity(self, domain_id):
        module = self.resolve(domain_id)
        functions = [module.prepare, module.normalize_check]
        if callable(getattr(module, "prepare_acceptance", None)):
            functions.append(module.prepare_acceptance)
        sources, code = {}, {}
        for function in functions:
            target = getattr(function, "__func__", function)
            require(hasattr(target, "__code__"), "DOMAIN_IDENTITY_UNAVAILABLE", "Domain methods must expose Python implementation identity")
            code[target.__name__] = canonical_hash(code_identity(target.__code__))
            filename = inspect.getsourcefile(target)
            if filename and Path(filename).is_file():
                sources[str(Path(filename).resolve())] = hashlib.sha256(Path(filename).read_bytes()).hexdigest()
        for filename in getattr(module, "identity_files", ()):
            path = Path(filename).resolve(strict=True)
            sources[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        config = getattr(module, "identity_config", None)
        if config is None:
            config = {"instance": vars(module), "class": {key: value for key, value in vars(type(module)).items()
                       if not key.startswith("__") and not callable(value) and not isinstance(value, (property, classmethod, staticmethod))}}
        try:
            configuration_hash = canonical_hash(config)
        except (TypeError, ValueError) as error:
            raise ValueError("Domain configuration must be JSON data or expose identity_config") from error
        body = {"id": module.domain_id, "revision": module.revision,
                "implementation_hash": canonical_hash({"files": sources, "methods": code}),
                "configuration_hash": configuration_hash}
        return {**body, "identity_hash": canonical_hash(body)}


def builtin_registry():
    from .domain import DevelopModule
    return DomainRegistry([DevelopModule()])
