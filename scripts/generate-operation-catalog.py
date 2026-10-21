#!/usr/bin/env python3
"""Generate/check the portable Skill's Core-owned syntax catalog."""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from harness.common import canonical_bytes
from harness_external.operation_schema import catalog

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    path = ROOT / "skills/harness-workflow/scripts/operation_catalog.json"
    expected = canonical_bytes(catalog()).decode("utf-8") + "\n"
    if args.check:
        if not path.is_file() or path.read_text(encoding="utf-8") != expected:
            raise SystemExit("Operation catalog is stale; regenerate from Core definitions")
    else:
        path.write_text(expected, encoding="utf-8")
