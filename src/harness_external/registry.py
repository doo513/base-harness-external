"""Explicit Domain port. The core does not choose semantic success criteria."""
from __future__ import annotations

from typing import Protocol

from .errors import require


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


def builtin_registry():
    from .domain import DevelopModule
    return DomainRegistry([DevelopModule()])
