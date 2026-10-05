"""Load explicit plugin roots in private packages without changing sys.path."""
import hashlib
import importlib
import importlib.util
from importlib.machinery import ModuleSpec
from pathlib import Path
import sys
from types import ModuleType

from .errors import require


def load_module(module_name, import_root=None, expected_source_hash=None):
    if import_root is None:
        qualified = module_name
        spec = importlib.util.find_spec(qualified)
        require(spec is not None and spec.origin is not None and Path(spec.origin).is_file(),
                "ADAPTER_UNAVAILABLE", "Registered factory source is unavailable")
        source = Path(spec.origin).resolve()
    else:
        root = Path(import_root).resolve(strict=True)
        target = root.joinpath(*module_name.split("."))
        source = target / "__init__.py" if (target / "__init__.py").is_file() else target.with_suffix(".py")
        require(source.is_file(), "ADAPTER_UNAVAILABLE", "Registered factory source is unavailable")
        source = source.resolve()
        require(source.is_relative_to(root), "ADAPTER_IMPORT_CONTEXT", "Factory source escaped its registered root")
        # The private package's __path__ supports relative imports, including
        # lazy ones. Neither ordinary module names nor the host search path move.
        prefix = "_harness_adapter_" + hashlib.sha256(str(root).encode()).hexdigest()
        if prefix not in sys.modules:
            package = ModuleType(prefix)
            package.__package__ = prefix
            package.__path__ = [str(root)]
            package.__spec__ = ModuleSpec(prefix, loader=None, is_package=True)
            package.__spec__.submodule_search_locations = package.__path__
            sys.modules[prefix] = package
        require(getattr(sys.modules[prefix], "__path__", None) == [str(root)],
                "ADAPTER_IMPORT_CONTEXT", "Private plugin namespace has a different root")
        qualified = prefix + "." + module_name
    if expected_source_hash is not None:
        require(hashlib.sha256(source.read_bytes()).hexdigest() == expected_source_hash,
                "ADAPTER_IMPLEMENTATION_CHANGED", "Registered factory changed before worker import")
    module = importlib.import_module(qualified)
    require(getattr(module, "__file__", None) and Path(module.__file__).resolve() == source,
            "ADAPTER_IMPORT_CONTEXT", "A different installation owns the factory module")
    return module
