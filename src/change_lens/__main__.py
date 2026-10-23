"""python -m change_lens: deterministic read-only project analysis."""
import argparse
import html
import json
import sys

from .analysis import analyze, load_rules
from .scanner import AnalysisError


def mermaid(result):
    ids = {module["path"]: f"m{index}" for index, module in enumerate(result["modules"])}
    escape = lambda value: html.escape(value, quote=True).replace("[", "&#91;").replace("]", "&#93;").replace("\n", "&#10;")
    lines = ["flowchart LR"]
    lines += [f'  {key}["{escape(path)}"]' for path, key in ids.items()]
    lines += [f'  {ids[edge["source"]]} --> {ids[edge["target"]]}' for edge in result["imports"]]
    return "\n".join(dict.fromkeys(lines))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("inspect", "impact", "context"):
        cmd = sub.add_parser(name)
        cmd.add_argument("path")
        cmd.add_argument("--format", choices=["json", "text", "mermaid"] if name == "inspect" else ["json", "text"], default="text")
        cmd.add_argument("--max-files", type=int, default=5000)
        cmd.add_argument("--cache-dir", help="optional disposable syntax cache outside the analyzed workspace")
        cmd.add_argument("--refresh", action="store_true", help="bypass cache and reread source content")
        if name == "inspect":
            cmd.add_argument("--rules")
        else:
            cmd.add_argument("--changed", action="append", required=True)
        if name == "context":
            cmd.add_argument("--limit", type=int, default=12)
    args = parser.parse_args(argv)
    try:
        analysis = analyze(args.path, max_files=args.max_files, cache_dir=args.cache_dir, refresh=args.refresh)
        if args.command == "inspect":
            result = analysis.report()
            if args.rules:
                result["architecture"] = analysis.boundaries(load_rules(args.rules))
        else:
            result = analysis.impact(args.changed)
            if args.command == "context":
                if not 1 <= args.limit <= 100:
                    raise AnalysisError("Context limit must be 1..100")
                chosen = result["affected"][:args.limit]
                result = {**{k: v for k, v in result.items() if k != "affected"},
                          "selected": [{**item, "symbols": analysis.by_path[item["path"]]["symbols"][:5],
                                        "omitted_symbols": max(0, len(analysis.by_path[item["path"]]["symbols"]) - 5)
                                                           + analysis.by_path[item["path"]].get("omitted_symbols", 0)} for item in chosen],
                          "tests": result["tests"][:args.limit], "omitted_tests": max(0, len(result["tests"]) - args.limit),
                          "diagnostics": result["diagnostics"][:args.limit], "omitted_diagnostics": max(0, len(result["diagnostics"]) - args.limit),
                          "omitted_files": len(result["affected"]) - len(chosen), "affected_count": len(result["affected"]),
                          "details": {"command": "impact", "workspace": str(analysis.root), "changed": result["changed"], "format": "json"}}
        if args.format == "mermaid":
            print(mermaid(result))
        elif args.format == "json":
            print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False))
        else:
            if args.command == "inspect":
                print("Python files:", result["summary"]["python_files"], "| local imports:", len(result["imports"]))
                for module in result["modules"]:
                    print(f'{module["path"]} [{"test" if module["is_test"] else "source"}]' + (" [entry candidate]" if module["entrypoint_candidate"] else ""))
                for cycle in result["cycles"]:
                    print("CYCLE:", " <-> ".join(cycle))
                for violation in result.get("architecture", {}).get("violations", []):
                    print("BOUNDARY:", f'{violation["source"]}:{violation["line"]}', "->", violation["target"])
            else:
                for item in result.get("affected", result.get("selected", [])):
                    print(item["path"], "[test]" if item["is_test"] else "", "via", " -> ".join(item["via"]))
                if "omitted_files" in result:
                    print("Omitted files:", result["omitted_files"])
                    print("Omitted tests:", result["omitted_tests"], "| omitted diagnostics:", result["omitted_diagnostics"])
                if result["unresolved_changes"]:
                    print("Unresolved changes:", ", ".join(result["unresolved_changes"]))
            for diagnostic in result["diagnostics"]:
                print("NOTE:", diagnostic["path"], diagnostic["code"])
            print("Scope: static candidates; inspect unresolved imports and run relevant tests.")
            if analysis.cache_info:
                print("Cache:", analysis.cache_info["freshness"], "| reused files:", analysis.cache_info["reused_files"], "| never verification evidence")
        return 0
    except (AnalysisError, OSError, UnicodeError, ValueError, RecursionError) as error:
        print(json.dumps({"error": "ANALYSIS_INPUT", "message": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
