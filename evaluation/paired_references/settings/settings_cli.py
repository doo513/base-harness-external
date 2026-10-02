import json
import os
from pathlib import Path
import sys
import tempfile
from settings import normalize


def pairs(values):
    result = {}
    for key, value in values:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def convert_file(source, destination):
    source, destination = Path(source), Path(destination)
    if source.resolve() == destination.resolve():
        raise ValueError("same path")
    result = normalize(json.loads(source.read_text(encoding="utf-8"), object_pairs_hook=pairs))
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=destination.parent, encoding="utf-8", delete=False) as output:
            temporary = Path(output.name)
            json.dump(result, output, ensure_ascii=False, allow_nan=False, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink()
    return result


def main(argv=None):
    try:
        args = sys.argv[1:] if argv is None else argv
        if len(args) != 2:
            raise ValueError("source and destination required")
        print(json.dumps(convert_file(*args), ensure_ascii=False, allow_nan=False))
        return 0
    except (ValueError, OSError) as error:
        print(json.dumps({"error": str(error)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
