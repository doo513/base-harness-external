"""AST facts only. A syntactic import is not proof of runtime execution."""
import ast
from pathlib import PurePosixPath


def module_name(path):
    parts = list(PurePosixPath(path).with_suffix("").parts)
    if parts[0] == "src":
        parts.pop(0)
    package = bool(parts and parts[-1] == "__init__")
    if package:
        parts.pop()
    return ".".join(parts) if parts and all(p.isidentifier() for p in parts) else None, package


def parse_file(file):
    path = file["path"]
    name, package = module_name(path)
    parts = PurePosixPath(path).parts
    item = {"path": path, "module": name, "package": package, "sha256": file["sha256"], "size": file["size"],
            "is_test": "tests" in parts or PurePosixPath(path).stem.startswith("test_") or PurePosixPath(path).stem.endswith("_test"),
            "entrypoint_candidate": PurePosixPath(path).name == "__main__.py", "symbols": [], "parsed": False}
    imports, diagnostics = [], []
    try:
        tree = ast.parse(file["text"], filename=path)
    except (SyntaxError, ValueError, RecursionError) as error:
        diagnostics.append({"path": path, "code": "PARSE_ERROR", "line": getattr(error, "lineno", None), "message": str(error)[:300]})
        return item, imports, diagnostics
    item["parsed"] = True
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            item["symbols"].append({"name": node.name, "kind": type(node).__name__, "line": node.lineno})
    item["omitted_symbols"] = max(0, len(item["symbols"]) - 100)
    item["symbols"] = item["symbols"][:100]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend({"source": path, "kind": "import", "module": a.name, "names": [], "level": 0, "line": node.lineno} for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append({"source": path, "kind": "from", "module": node.module or "", "names": [a.name for a in node.names], "level": node.level, "line": node.lineno})
        elif isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], ast.Eq) and len(node.comparators) == 1:
            left, right = node.left, node.comparators[0]
            if ((isinstance(left, ast.Name) and left.id == "__name__" and isinstance(right, ast.Constant) and right.value == "__main__") or
                (isinstance(right, ast.Name) and right.id == "__name__" and isinstance(left, ast.Constant) and left.value == "__main__")):
                item["entrypoint_candidate"] = True
        elif isinstance(node, ast.Call) and ((isinstance(node.func, ast.Name) and node.func.id == "__import__") or
                                            (isinstance(node.func, ast.Attribute) and node.func.attr == "import_module")):
            diagnostics.append({"path": path, "line": node.lineno, "code": "DYNAMIC_IMPORT_CANDIDATE"})
    return item, imports, diagnostics
