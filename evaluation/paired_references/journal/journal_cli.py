import json
import sys
from journal import Journal, unique


def main(argv=None):
    try:
        args = sys.argv[1:] if argv is None else argv
        if len(args) == 2 and args[0] == "list":
            result = Journal(args[1]).read_all()
        elif len(args) == 4 and args[0] == "put":
            result = {"created": Journal(args[1]).append(args[2], json.loads(args[3], object_pairs_hook=unique))}
        else:
            raise ValueError("usage")
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except (ValueError, OSError) as error:
        print(json.dumps({"error": str(error)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
