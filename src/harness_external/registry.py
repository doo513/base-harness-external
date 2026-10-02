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
        identity_files = tuple(getattr(module, "identity_files", ()))
        identity_file_ids = getattr(module, "identity_file_ids", None)
        if identity_file_ids is None:
            identity_file_ids = tuple(str(index) for index in range(len(identity_files)))
        require(isinstance(identity_file_ids, (list, tuple)) and len(identity_file_ids) == len(identity_files)
                and all(isinstance(value, str) and 0 < len(value) <= 128 for value in identity_file_ids)
                and len(set(identity_file_ids)) == len(identity_file_ids),
                "DOMAIN_IDENTITY_UNAVAILABLE", "identity_file_ids must uniquely name each Domain identity file")
        functions = [module.prepare, module.normalize_check]
        if callable(getattr(module, "prepare_acceptance", None)):
            functions.append(module.prepare_acceptance)
        sources, code = {}, {}
        for function in functions:
            target = getattr(function, "__func__", function)
            require(hasattr(target, "__code__"), "DOMAIN_IDENTITY_UNAVAILABLE", "Domain methods must expose Python implementation identity")
            method_id = module.domain_id + ":" + target.__qualname__
            code[method_id] = canonical_hash(code_identity(target.__code__))
            filename = inspect.getsourcefile(target)
            if filename and Path(filename).is_file():
                sources["domain-method:" + method_id] = hashlib.sha256(Path(filename).read_bytes()).hexdigest()
        for logical_id, filename in zip(identity_file_ids, identity_files):
            path = Path(filename).resolve(strict=True)
            sources["identity-file:" + logical_id] = hashlib.sha256(path.read_bytes()).hexdigest()
        config = getattr(module, "identity_config", None)
        if config is None:
            config = {"instance": {key: value for key, value in vars(module).items()
                                    if key not in {"identity_files", "identity_file_ids"}},
                      "class": {key: value for key, value in vars(type(module)).items()
                                if key not in {"identity_files", "identity_file_ids"} and not key.startswith("__") and not callable(value)
                                and not isinstance(value, (property, classmethod, staticmethod))},
                      "identity_file_ids": list(identity_file_ids)}
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
