import copy
import fcntl
import json
import math
from pathlib import Path


def validate(value):
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for child in value:
            validate(child)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for child in value.values():
            validate(child)
        return
    raise ValueError("invalid JSON")


def canonical(value):
    validate(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def parse(raw):
    end = raw.rfind(b"\n") + 1
    rows, values = [], {}
    for line in raw[:end].decode("utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line, object_pairs_hook=unique)
        if type(row) is not dict or set(row) != {"key", "value"} or type(row["key"]) is not str or not row["key"]:
            raise ValueError("record")
        serialized = canonical(row["value"])
        if row["key"] in values:
            if values[row["key"]] != serialized:
                raise ValueError("conflict")
        else:
            rows.append(row)
            values[row["key"]] = serialized
    return rows, values, end


class Journal:
    def __init__(self, path):
        self.path = Path(path)

    def read_all(self):
        try:
            with self.path.open("rb") as stream:
                fcntl.flock(stream, fcntl.LOCK_SH)
                return copy.deepcopy(parse(stream.read())[0])
        except FileNotFoundError:
            return []

    def append(self, key, value):
        if type(key) is not str or not key:
            raise ValueError("key")
        encoded = canonical(value)
        with self.path.open("a+b") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            stream.seek(0)
            _, values, end = parse(stream.read())
            if key in values:
                if values[key] != encoded:
                    raise ValueError("conflict")
                return False
            stream.truncate(end)
            stream.seek(0, 2)
            stream.write((canonical({"key": key, "value": value}) + "\n").encode())
            stream.flush()
            __import__("os").fsync(stream.fileno())
            return True
