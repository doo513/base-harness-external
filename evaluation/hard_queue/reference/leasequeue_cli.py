import json
import sys
from leasequeue import Queue


def pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def main(argv=None):
    try:
        argv = sys.argv[1:] if argv is None else argv
        if len(argv) != 2:
            raise ValueError("DB and JSON_REQUEST required")
        request = json.loads(argv[1], object_pairs_hook=pairs, parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
        if type(request) is not dict or set(request) != {"op", "args"} or request["op"] not in ("add", "claim", "renew", "finish", "get", "events") or type(request["args"]) is not dict:
            raise ValueError("invalid request")
        value = getattr(Queue(argv[0]), request["op"])(**request["args"])
        print(json.dumps({"ok": True, "value": value}, ensure_ascii=False, allow_nan=False))
        return 0
    except Exception as error:
        print(json.dumps({"ok": False, "error": str(error) or type(error).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
