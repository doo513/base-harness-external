#!/usr/bin/env python3
"""Copy the bundled generic Harness Skill to an explicitly selected Host directory."""
import argparse
import json
from pathlib import Path
import shutil


def install(destination):
    source = Path(__file__).resolve().parents[1] / "skills" / "harness-workflow"
    target = Path(destination).expanduser().absolute() / source.name
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"Refusing to overwrite existing Skill: {target}")
    if any(path.is_symlink() for path in source.rglob("*")):
        raise ValueError("Bundled Skill must not contain symbolic links")
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return {"ok": True, "skill": str(target), "configured": False,
            "note": "Configure harness_client.py separately; no Host settings or Run state were changed."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", required=True, help="explicit Host skill search directory; a harness-workflow subfolder is created")
    args = parser.parse_args()
    try:
        result = install(args.destination)
    except (OSError, ValueError) as error:
        print(json.dumps({"ok": False, "error": str(error)}))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
