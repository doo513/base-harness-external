#!/usr/bin/env python3
"""Prepare the optional pytest runtime snapshot using the Harness interpreter."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from harness_external.errors import HarnessError
from harness_external.pytest_adapter import prepare_runtime
from harness_external.store import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", required=True, help="the configured Harness store outside the workspaces")
    parser.add_argument("--refresh", action="store_true", help="snapshot newly installed package bytes; existing Runs keep their pinned runtime")
    args = parser.parse_args()
    try:
        store = Store(args.state_dir)
        runtime = prepare_runtime(store.root, refresh=args.refresh)
        print(json.dumps({"ok": True, "runtime": runtime, "assurance": "local-advisory", "execution_probe": "not_run"}))
        return 0
    except (HarnessError, OSError, ValueError) as error:
        print(json.dumps({"ok": False, "error": {"code": getattr(error, "code", "RUNTIME_PREPARATION_ERROR"), "message": str(error)}}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
