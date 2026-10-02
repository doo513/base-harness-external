import argparse
import json
from pathlib import Path
import sys
from planner import plan, impact


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("file")
    parser.add_argument("--completed", default="")
    parser.add_argument("--changed", default="")
    args = parser.parse_args(argv)
    try:
        tasks = json.loads(Path(args.file).read_text(encoding="utf-8"), object_pairs_hook=unique)
        completed = args.completed.split(",") if args.completed else []
        changed = args.changed.split(",") if args.changed else []
        print(json.dumps({"waves": plan(tasks, completed), "affected": impact(tasks, changed)}))
        return 0
    except (ValueError, OSError) as error:
        print(json.dumps({"error": str(error)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
