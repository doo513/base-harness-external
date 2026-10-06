"""Explicit application registrations, pinned identities and detached restoration.

Factory references/import roots are operator code configuration, not Check data.
No workspace discovery, pickle, eval or model-supplied plugin loading is used.
These are local-advisory consistency checks, not a same-user trust boundary.
"""
import copy
from dataclasses import asdict, dataclass, field
import hashlib
import inspect
from pathlib import Path
import re
import threading

from harness.common import canonical_bytes, canonical_hash
from .errors import fields, relative_path, require
from .registry import code_identity
from .adapter_loading import load_module

METHODS = ("runtime", "command", "stage", "observer")
SCHEMA_METHODS = ("normalize_selection", "validate_reference", "legacy_execution_check")
_IMPORT_LOCK = threading.RLock()


@dataclass(frozen=True)
class AdapterRegistration:
    adapter_id: str
    factory: str
    config: dict = field(default_factory=dict)
    import_root: str | None = None
    parameter_alias: str | None = None


def validate_binding(binding):
    require(isinstance(binding, dict) and binding.get("schema_version") == "adapter-binding-v1"
            and binding.get("binding_hash") == canonical_hash({k: v for k, v in binding.items() if k != "binding_hash"})
            and binding.get("status") in {"available", "unavailable"}
            and isinstance(binding.get("implementation"), dict)
            and binding.get("id") == binding["implementation"].get("id")
            and (isinstance(binding.get("runtime"), dict) and binding.get("reason") is None if binding.get("status") == "available"
                 else binding.get("runtime") is None and isinstance(binding.get("reason"), str)),
            "ADAPTER_BINDING_CORRUPT", "Invalid pinned adapter binding")


class AdapterRegistry:
    def __init__(self, registrations):
        self._registrations, self._instances, self._factories, self._expected = {}, {}, {}, {}
        for value in registrations:
            require(isinstance(value, AdapterRegistration), "ADAPTER_REGISTRATION_INVALID", "Use an explicit AdapterRegistration")
            value = copy.deepcopy(value)
            require(isinstance(value.adapter_id, str) and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", value.adapter_id)),
                    "ADAPTER_REGISTRATION_INVALID", "Invalid adapter ID")
            require(value.adapter_id not in self._registrations, "ADAPTER_DUPLICATE", "Duplicate adapter ID")
            require(isinstance(value.factory, str) and bool(re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*", value.factory)),
                    "ADAPTER_REGISTRATION_INVALID", "Factory must be a top-level Python class reference")
            require(isinstance(value.config, dict) and len(canonical_bytes(value.config)) <= 32768,
                    "ADAPTER_REGISTRATION_INVALID", "Adapter configuration must be a bounded JSON object")
            require(value.import_root is None or isinstance(value.import_root, str) and Path(value.import_root).is_absolute()
                    and Path(value.import_root).is_dir(), "ADAPTER_REGISTRATION_INVALID", "Import root must be an existing absolute application directory")
            require(value.parameter_alias is None or isinstance(value.parameter_alias, str)
                    and bool(re.fullmatch(r"[a-z][a-z0-9_]{0,95}", value.parameter_alias))
                    and value.parameter_alias not in {"inputs", "artifacts", "expectations", "execution_checks", "test_commands", "profile", "validation_profile", "requirements", "coverage"},
                    "ADAPTER_REGISTRATION_INVALID", "Invalid legacy parameter alias")
            require(value.parameter_alias is None or value.parameter_alias not in self.parameter_aliases(),
                    "ADAPTER_DUPLICATE", "Duplicate legacy parameter alias")
            self._registrations[value.adapter_id] = value

    def parameter_aliases(self):
        return {entry.parameter_alias: key for key, entry in self._registrations.items() if entry.parameter_alias is not None}

    def resolve(self, adapter_id):
        # Serialize registry instance creation and private package initialization.
        with _IMPORT_LOCK:
            return self._resolve(adapter_id)

    def _resolve(self, adapter_id):
        require(isinstance(adapter_id, str) and adapter_id in self._registrations, "ADAPTER_UNREGISTERED", "Execution adapter is not registered")
        if adapter_id not in self._instances:
            entry = self._registrations[adapter_id]
            module_name, name = entry.factory.split(":")
            try:
                expected = self._expected.get(adapter_id)
                module = load_module(module_name, entry.import_root, expected["factory_source_hash"] if expected else None)
                factory = getattr(module, name, None)
                require(inspect.isclass(factory) and factory.__module__ == module.__name__ and factory.__name__ == name,
                        "ADAPTER_REGISTRATION_INVALID", "Factory must be defined in the registered module")
                adapter = factory(**copy.deepcopy(entry.config))
            except (ImportError, AttributeError) as error:
                require(False, "ADAPTER_UNAVAILABLE", "Cannot load registered adapter: " + str(error))
            require(getattr(adapter, "adapter_id", None) == adapter_id and isinstance(getattr(adapter, "revision", None), str)
                    and bool(adapter.revision) and all(callable(getattr(adapter, method, None)) for method in METHODS),
                    "ADAPTER_MODULE_INVALID", "Adapter does not implement the execution port")
            relative_path(adapter.report_path)
            self._instances[adapter_id], self._factories[adapter_id] = adapter, factory
        return self._instances[adapter_id]

    def identity(self, adapter_id):
        adapter = self.resolve(adapter_id)
        entry, factory = self._registrations[adapter_id], self._factories[adapter_id]
        arguments = inspect.signature(factory).bind(**copy.deepcopy(entry.config))
        arguments.apply_defaults()
        source = Path(inspect.getsourcefile(factory))
        sources, methods = {}, {}
        for name in (*METHODS, *(name for name in SCHEMA_METHODS if callable(getattr(adapter, name, None)))):
            function = getattr(adapter, name)
            target = getattr(function, "__func__", function)
            require(hasattr(target, "__code__"), "ADAPTER_IDENTITY_UNAVAILABLE", "Adapter methods must expose Python code identity")
            methods[name] = canonical_hash(code_identity(target.__code__))
            filename = inspect.getsourcefile(target)
            require(filename and Path(filename).is_file(), "ADAPTER_IDENTITY_UNAVAILABLE", "Adapter source is unavailable")
            sources[name] = hashlib.sha256(Path(filename).read_bytes()).hexdigest()
        identity_files = tuple(getattr(adapter, "identity_files", ()))
        file_ids = tuple(getattr(adapter, "identity_file_ids", [str(i) for i in range(len(identity_files))]))
        require(len(file_ids) == len(identity_files) and all(isinstance(key, str) for key in file_ids) and len(set(file_ids)) == len(file_ids),
                "ADAPTER_IDENTITY_UNAVAILABLE", "Identity files need unique logical IDs")
        for key, filename in zip(file_ids, identity_files):
            sources["dependency:" + key] = hashlib.sha256(Path(filename).read_bytes()).hexdigest()
        config = getattr(adapter, "identity_config", None)
        if config is None:
            effective = {}
            for cls in reversed(type(adapter).__mro__):
                effective.update(vars(cls))
            effective.update(vars(adapter))
            config = {key: value for key, value in effective.items() if not key.startswith("__")
                      and key not in {"identity_files", "identity_file_ids", "identity_config"} and not callable(value)
                      and not isinstance(value, (property, classmethod, staticmethod))}
        body = {"schema_version": "execution-adapter-identity-v1", "id": adapter_id, "revision": adapter.revision,
                "factory": entry.factory, "factory_source_hash": hashlib.sha256(source.read_bytes()).hexdigest(),
                "implementation_hash": canonical_hash({"sources": sources, "methods": methods}),
                "configuration_hash": canonical_hash({"registration": arguments.arguments, "effective": config, "identity_file_ids": file_ids,
                                                      "parameter_alias": entry.parameter_alias})}
        result = {**body, "identity_hash": canonical_hash(body)}
        if adapter_id in self._expected:
            require(result == self._expected[adapter_id], "ADAPTER_IMPLEMENTATION_CHANGED", "Pinned adapter implementation or configuration changed")
        return result

    def validate_bindings(self, bindings):
        for binding in bindings.values():
            validate_binding(binding)
            require(self.identity(binding["id"]) == binding["implementation"],
                    "ADAPTER_IMPLEMENTATION_CHANGED", "Adapter differs from its Run binding")

    def pin_implementations(self, checks, pinned=None):
        """Pin code/config when a Check is admitted, before runtime preparation."""
        result = copy.deepcopy(pinned or {})
        for adapter_id, identity in result.items():
            require(self.identity(adapter_id) == identity, "ADAPTER_IMPLEMENTATION_CHANGED", "Adapter changed after Check admission")
        for check in checks:
            if check.get("adapter"):
                adapter_id = check["adapter"]["id"]
                identity = self.identity(adapter_id)
                require(adapter_id not in result or result[adapter_id] == identity,
                        "ADAPTER_IMPLEMENTATION_CHANGED", "Adapter changed while pinning Checks")
                result[adapter_id] = identity
        return result

    def export(self, bindings):
        self.validate_bindings(bindings)
        return {adapter_id: {"registration": asdict(self._registrations[adapter_id]), "identity": self.identity(adapter_id)}
                for adapter_id in sorted({binding["id"] for binding in bindings.values()})}

    @classmethod
    def restore(cls, manifest):
        require(isinstance(manifest, dict) and len(manifest) <= 256, "ADAPTER_REGISTRATION_INVALID", "Invalid worker registrations")
        entries, expected = [], {}
        for key, item in manifest.items():
            fields(item, {"registration", "identity"}, {"registration", "identity"})
            fields(item["registration"], {"adapter_id", "factory", "config", "import_root", "parameter_alias"}, {"adapter_id", "factory", "config", "import_root"})
            require(item["registration"]["adapter_id"] == key and item["identity"].get("id") == key,
                    "ADAPTER_REGISTRATION_INVALID", "Worker registration identity mismatch")
            entries.append(AdapterRegistration(**item["registration"]))
            expected[key] = copy.deepcopy(item["identity"])
        result = cls(entries)
        result._expected = expected
        return result


def builtin_adapter_registry():
    # Registration is lazy: read-only CLI calls do not import verifier plugins.
    return AdapterRegistry([AdapterRegistration("pytest-cases-v1", "harness_external.pytest_adapter:PytestAdapter", parameter_alias="pytest_checks")])
