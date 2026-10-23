"""Combine syntax facts, explicit architecture rules and dependency queries."""
from collections import defaultdict
import importlib.util
import json
from pathlib import PurePosixPath
import sys

from .graph import DependencyGraph
from .parser import parse_file
from .scanner import AnalysisError, regular_bytes, scan


class Analysis:
    def __init__(self, scanned, cache=None):
        self.root = scanned["root"]
        self.modules, declarations = [], []
        self.diagnostics = list(scanned["diagnostics"])
        self.directories = scanned["directories"]
        self.read_bytes = scanned["read_bytes"]
        self.cache_info = None
        indexed = defaultdict(list)
        for file in scanned["files"]:
            parsed = file.get("cached_parse")
            if parsed is None:
                parsed = parse_file(file)
                if cache is not None:
                    cache.stats["parsed_files"] += 1
            module, imports, diagnostics = parsed
            if cache is not None:
                cache.remember(file, parsed)
            self.modules.append(module)
            declarations.extend(imports)
            self.diagnostics.extend(diagnostics)
            if module["module"]:
                indexed[module["module"]].append(module["path"])
        self.by_path = {m["path"]: m for m in self.modules}
        self.unresolved, self.edges = [], []
        seen = set()
        for name, paths in indexed.items():
            if len(paths) > 1:
                self.diagnostics.append({"path": paths[0], "code": "AMBIGUOUS_MODULE", "module": name, "candidates": paths})
        for declaration in sorted(declarations, key=lambda d: (d["source"], d["line"], d["module"])):
            source = self.by_path[declaration["source"]]
            name = declaration["module"]
            if declaration["level"]:
                package = source["module"] if source["package"] else (source["module"] or "").rpartition(".")[0]
                try:
                    name = importlib.util.resolve_name("." * declaration["level"] + name, package)
                except (ImportError, ValueError):
                    self.unresolved.append({**declaration, "reason": "invalid_relative_import"})
                    continue
            requested = [name]
            if declaration["kind"] == "from":
                requested += [name + "." + n for n in declaration["names"] if n != "*" and name + "." + n in indexed]
            targets = set()
            ambiguous = False
            for requested_name in requested:
                parts = requested_name.split(".")
                for length in range(1, len(parts) + 1):
                    candidates = indexed.get(".".join(parts[:length]), [])
                    if len(candidates) == 1:
                        targets.add(candidates[0])
                    elif len(candidates) > 1:
                        ambiguous = True
            missing_tail = bool(targets) and name not in indexed and not any(k.startswith(name + ".") for k in indexed)
            if ambiguous or not targets or missing_tail:
                category = "ambiguous_local" if ambiguous else "missing_local_module" if missing_tail else "stdlib" if name.partition(".")[0] in sys.stdlib_module_names else "external_or_unresolved"
                self.unresolved.append({**declaration, "reason": category})
            for target in sorted(targets):
                if target == source["path"] and declaration["kind"] == "from":
                    continue
                key = (source["path"], target, declaration["line"])
                if key not in seen:
                    seen.add(key)
                    self.edges.append({"source": source["path"], "target": target, "line": declaration["line"]})
        self.edges.sort(key=lambda e: (e["source"], e["target"], e["line"]))
        self.graph = DependencyGraph(self.by_path, [(e["source"], e["target"]) for e in self.edges])
        self.diagnostics.sort(key=lambda d: (d["path"], d.get("line") or 0, d["code"]))
        if cache is not None:
            cache.save()
            self.cache_info = {**cache.stats, "source_bytes": scanned["source_bytes"], "bytes_read": self.read_bytes,
                               "meaning": "advisory_syntax_cache_not_a_verified_snapshot"}

    def report(self):
        cycles = self.graph.cycles()
        return {"schema_version": "change-lens-v1", "summary": {"python_files": len(self.modules),
            "parsed_files": sum(m["parsed"] for m in self.modules), "local_import_edges": len(self.edges),
            "test_files": sum(m["is_test"] for m in self.modules), "cycle_groups": len(cycles), "read_bytes": self.read_bytes},
            "modules": self.modules, "directories": self.directories, "imports": self.edges, "cycles": cycles,
            "unresolved_imports": self.unresolved, "diagnostics": self.diagnostics,
            "complete_local_scan": not self.diagnostics,
            **({"cache": self.cache_info} if self.cache_info is not None else {}),
            "limitations": ["Static import relationships are candidates, not proof of runtime impact.",
                            "Dynamic imports, attributes/re-exports and runtime sys.path changes may hide dependencies.",
                            "External dependencies are not installed or executed."]}

    def impact(self, changed):
        normalized = []
        for value in changed:
            p = PurePosixPath(value.replace("\\", "/"))
            if p.is_absolute() or ".." in p.parts or not p.parts:
                raise AnalysisError("Changed paths must stay relative to the project")
            normalized.append(p.as_posix())
        distance, parent = self.graph.impact(normalized)
        affected = []
        for path in sorted(distance, key=lambda p: (distance[p], p)):
            chain, current = [], path
            while current is not None:
                chain.append(current)
                current = parent[current]
            affected.append({"path": path, "distance": distance[path], "via": list(reversed(chain)), "is_test": self.by_path[path]["is_test"]})
        unknown = sorted(set(normalized) - self.by_path.keys())
        return {"schema_version": "change-lens-impact-v1", "changed": sorted(set(normalized)), "affected": affected,
                "tests": [item["path"] for item in affected if item["is_test"]], "unresolved_changes": unknown,
                "complete_local_scan": not self.diagnostics and not unknown, "diagnostics": self.diagnostics,
                "unresolved_import_count": len(self.unresolved),
                "runtime_dependency_completeness": "not_established",
                **({"cache": self.cache_info} if self.cache_info is not None else {}),
                "meaning": "static_candidates_not_runtime_coverage_or_permission_to_skip_tests"}

    def boundaries(self, rules):
        if not isinstance(rules, dict) or set(rules) != {"layers", "allow"}:
            raise AnalysisError("Rules require layers and allow objects")
        layers, allow = rules["layers"], rules["allow"]
        if not isinstance(layers, dict) or not isinstance(allow, dict) or set(allow) != set(layers) or len(layers) > 32:
            raise AnalysisError("Declare at most 32 layers and an allow list for each")
        for name, prefixes in layers.items():
            if not isinstance(name, str) or not isinstance(prefixes, list) or not prefixes or any(not isinstance(p, str) or not p or not all(x.isidentifier() for x in p.split(".")) for p in prefixes):
                raise AnalysisError("Layers use nonempty Python module prefixes")
            if not isinstance(allow[name], list) or any(not isinstance(t, str) or t not in layers for t in allow[name]):
                raise AnalysisError("Allow lists must name declared layers")
        assigned = {}
        for module in self.modules:
            matches = [(len(p), layer) for layer, prefixes in layers.items() for p in prefixes
                       if module["module"] and (module["module"] == p or module["module"].startswith(p + "."))]
            if matches:
                longest = max(n for n, _ in matches)
                selected = {layer for n, layer in matches if n == longest}
                if len(selected) != 1:
                    raise AnalysisError("A module matches equally specific layers")
                assigned[module["path"]] = selected.pop()
        violations = [{**edge, "source_layer": assigned[edge["source"]], "target_layer": assigned[edge["target"]]}
                      for edge in self.edges if edge["source"] in assigned and edge["target"] in assigned
                      and assigned[edge["source"]] != assigned[edge["target"]]]
        violations = [v for v in violations if v["target_layer"] not in allow[v["source_layer"]]]
        return {"violations": violations, "classified_files": len(assigned),
                "unclassified_files": sorted(self.by_path.keys() - assigned.keys()),
                "meaning": "declared_import_rules_only_not_architecture_correctness"}


def load_rules(path):
    def unique(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                raise AnalysisError("Duplicate rule key")
            out[key] = value
        return out
    return json.loads(regular_bytes(path, 64000).decode("utf-8"), object_pairs_hook=unique)


def analyze(root, *, cache_dir=None, refresh=False, **limits):
    defaults = {"max_files": 5000, "max_file_bytes": 2_000_000, "max_total_bytes": 32_000_000}
    if set(limits) - defaults.keys() or any(type(v) is not int or v <= 0 for v in limits.values()):
        raise AnalysisError("Use supported positive integer scan limits")
    limits = {**defaults, **limits}
    cache = None
    if cache_dir is not None:
        from .cache import DiscoveryCache
        cache = DiscoveryCache(root, cache_dir, limits, refresh=refresh)
    elif refresh:
        raise AnalysisError("refresh requires an explicit cache directory")
    return Analysis(scan(root, cache=cache, **limits), cache)
