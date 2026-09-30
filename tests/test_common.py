import json

from harness.common import atomic_json, canonical_hash, redact


def test_canonical_hash_is_key_order_independent():
    assert canonical_hash({"a": 1, "b": 2}) == canonical_hash({"b": 2, "a": 1})
    assert canonical_hash({"a": 1}) != canonical_hash({"a": 2})


def test_atomic_record_roundtrip(tmp_path):
    target = tmp_path / "state" / "record.json"
    atomic_json(target, {"revision": 1})
    atomic_json(target, {"revision": 2, "text": "검증"})
    assert json.loads(target.read_text()) == {"revision": 2, "text": "검증"}
    assert list(target.parent.glob("*.tmp")) == []


def test_redaction_retains_public_values_and_masks_credentials():
    value = {"password": "fixture-only", "status": "passed", "message": "Bearer " + "a" * 32}
    assert redact(value) == {"password": "[REDACTED]", "status": "passed", "message": "[REDACTED]"}
