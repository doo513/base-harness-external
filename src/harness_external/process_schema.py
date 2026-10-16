"""Discoverable selector syntax for the external process observer."""


def schema():
    def obj(properties, required):
        return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}
    text = {"type": "string"}
    output = obj({"id": text, "stream": {"enum": ["stdout", "stderr"]},
                  "operator": {"enum": ["equals", "contains", "json_equals"]}, "expected": {},
                  "json_path": {"type": "array", "maxItems": 16, "items": {"oneOf": [text, {"type": "integer", "minimum": 0}]}}},
                 ["id", "stream", "operator", "expected"])
    file = obj({"kind": {"const": "file"}, "id": text, "path": text,
                "operator": {"enum": ["equals", "json_equals", "sha256", "unchanged", "absent"]}, "expected": {}},
               ["kind", "id", "path", "operator"])
    file["description"] = "expected is required for equals/json_equals/sha256, forbidden for unchanged/absent. Paths are workspace-relative, including when cwd differs."
    return obj({"source_paths": {"type": "array", "items": text, "minItems": 1, "maxItems": 128},
                "argv": {"type": "array", "items": text, "minItems": 1, "maxItems": 64}, "cwd": text,
                "expected_exit_code": {"type": "integer", "minimum": 0, "maximum": 255, "default": 0},
                "fixtures": {"type": "array", "maxItems": 32, "items": obj({"path": text, "text": text}, ["path", "text"]),
                             "description": "Disposable UTF-8 files; cannot overlap submitted inputs. At most 64 KB each / 128 KB total."},
                "cases": {"type": "array", "minItems": 1, "maxItems": 128, "items": {"oneOf": [output, file]}}},
               ["source_paths", "argv", "cases"])
