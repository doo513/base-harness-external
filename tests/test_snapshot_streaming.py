import copy
import hashlib
import json
import os
import tracemalloc

import pytest

from harness import measurement_v5
from harness.snapshot_io import CHUNK_BYTES, iter_snapshot_chunks, read_snapshot
from harness_external import snapshots
from harness_external.identity import EXECUTION_FILES


def payload(root, files, operator="sha256", expected=None):
    manifest = {"kind": "candidate", "files": [
        {"path": name, "size": (root / name).stat().st_size,
         "sha256": hashlib.sha256((root / name).read_bytes()).hexdigest()} for name in files], "dependencies": []}
    encoded = json.dumps(manifest, ensure_ascii=False, separators=(",", ":"))
    return {"subject": {"kind": "candidate", "id": "candidate", "revision": 1,
                         "sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest()},
            "manifestJson": encoded, "environmentHash": "b" * 64,
            "check": {"schemaVersion": "check-spec-v1", "ref": {"id": "check", "revision": 1, "sha256": "a" * 64},
                      "author": "model", "executorId": "python-measurement", "supportedSubjects": ["candidate"],
                      "parameters": {"kind": "file", "path": files[0], "operator": operator,
                                     "expected": expected if expected is not None else manifest["files"][0]["sha256"]},
                      "requiredCapabilities": ["read"], "timeoutMs": 1000}}


def engine(root):
    instance = measurement_v5.MeasurementEngine()
    instance.handle({"version": 5, "id": "open", "runId": "run", "scopeId": "scope", "type": "run.open",
                     "payload": {"snapshotRoot": str(root)}})
    return instance


def measure(instance, value, request="measure"):
    return instance.handle({"version": 5, "id": request, "runId": "run", "scopeId": "scope", "type": "measure", "payload": value})


def test_streaming_capture_never_uses_whole_file_compatibility_reader(tmp_path, monkeypatch):
    workspace, state = tmp_path / "workspace", tmp_path / "state"
    workspace.mkdir()
    state.mkdir()
    (workspace / "data").write_bytes(b"chunk" * CHUNK_BYTES)
    monkeypatch.setattr(snapshots, "read_file", lambda *args: pytest.fail("whole-file capture read"))
    result = snapshots.capture(workspace, ["data"], state)
    assert (state / result["candidate_id"] / "payload/data").read_bytes() == (workspace / "data").read_bytes()
    snapshots.validate_candidate(result, state / result["candidate_id"] / "payload")
    assert "harness/snapshot_io.py" in EXECUTION_FILES


def test_stream_reader_rejects_changes_after_a_chunk(tmp_path):
    target = tmp_path / "data"
    target.write_bytes(b"x" * (CHUNK_BYTES * 2))
    chunks = iter_snapshot_chunks(tmp_path, "data")
    assert len(next(chunks)) == CHUNK_BYTES
    target.write_bytes(b"y" * (CHUNK_BYTES * 2))
    with pytest.raises(OSError, match="changed"):
        list(chunks)


def test_stream_reader_close_releases_descriptors(tmp_path):
    if not os.path.isdir("/proc/self/fd"):
        pytest.skip("descriptor counters require procfs")
    (tmp_path / "data").write_bytes(b"x" * CHUNK_BYTES * 2)
    count = len(os.listdir("/proc/self/fd"))
    for _ in range(40):
        chunks = iter_snapshot_chunks(tmp_path, "data")
        next(chunks)
        chunks.close()
    assert len(os.listdir("/proc/self/fd")) == count


@pytest.mark.parametrize("name", ["../data", "/data", "data//x", "data\\x", "data:x"])
def test_stream_reader_rejects_unsafe_paths(tmp_path, name):
    with pytest.raises(ValueError, match="path"):
        read_snapshot(tmp_path, name)


def test_manifest_reused_but_inputs_always_revalidated(tmp_path, monkeypatch):
    (tmp_path / "data").write_bytes(b"valid")
    instance, value = engine(tmp_path), payload(tmp_path, ["data"])
    calls = []
    original = measurement_v5.decode
    def counted(text):
        calls.append(text)
        return original(text)
    monkeypatch.setattr(measurement_v5, "decode", counted)
    assert measure(instance, value)["result"]["execution"] == "completed"
    assert measure(instance, copy.deepcopy(value), "next")["result"]["execution"] == "completed"
    assert len(calls) == 1
    (tmp_path / "data").write_bytes(b"changed")
    result = measure(instance, value, "changed")["result"]
    assert result["execution"] == "error"
    assert "SUBJECT_BYTES_CHANGED" in result["error"]["message"]
    assert len(calls) == 1
    assert len(instance.requests["measure"][0]) == 64


def test_manifest_cache_is_bounded_and_subject_specific(tmp_path):
    (tmp_path / "data").write_bytes(b"valid")
    instance, value = engine(tmp_path), payload(tmp_path, ["data"])
    for index in range(24):
        changed = copy.deepcopy(value)
        changed["subject"]["id"] = str(index)
        measure(instance, changed, str(index))
    assert len(instance.manifests) == 16
    assert instance.manifest_bytes <= 1024 * 1024
    assert all(isinstance(item[0], tuple) for item in instance.manifests.values())
    bad = copy.deepcopy(value)
    bad["manifestJson"] += " "
    with pytest.raises(measurement_v5.MeasurementProtocolError, match="digest"):
        measure(instance, bad, "wrong-digest")


def test_unselected_files_do_not_accumulate_in_memory(tmp_path):
    files = ["first", "second", "third"]
    for name in files:
        (tmp_path / name).write_bytes(b"x" * 3 * 1024 * 1024)
    instance, value = engine(tmp_path), payload(tmp_path, files)
    tracemalloc.start()
    try:
        report = measure(instance, value)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert report["result"]["execution"] == "completed"
    assert peak < 1024 * 1024
    (tmp_path / "third").write_bytes(b"bad")
    assert measure(instance, value, "tampered-unselected")["result"]["execution"] == "error"


def test_utf8_chunk_boundaries_and_invalid_trailing_bytes(tmp_path):
    text = "a" * (CHUNK_BYTES - 1) + "한글" + "z"
    (tmp_path / "data").write_text(text, encoding="utf-8")
    value = payload(tmp_path, ["data"], "contains", "한글")
    assert measure(engine(tmp_path), value)["result"]["findings"][-1]["observed"] == text
    (tmp_path / "data").write_bytes(text.encode("utf-8") + b"\xff")
    value = payload(tmp_path, ["data"], "contains", "한글")
    assert measure(engine(tmp_path), value)["result"]["execution"] == "error"


def test_digest_mismatch_precedes_text_error_as_in_whole_file_reader(tmp_path):
    (tmp_path / "data").write_bytes(b"valid")
    value = payload(tmp_path, ["data"], "contains", "valid")
    (tmp_path / "data").write_bytes(b"\xffchanged")
    result = measure(engine(tmp_path), value)["result"]
    assert "SUBJECT_BYTES_CHANGED" in result["error"]["message"]


def test_large_reply_cache_is_bounded_lossless_and_not_recomputed_after_change(tmp_path):
    text = "retained observation " * 20000
    (tmp_path / "data").write_text(text, encoding="utf-8")
    instance, value = engine(tmp_path), payload(tmp_path, ["data"], "contains", "observation")
    first = measure(instance, value)
    for index in range(8):
        measure(instance, value, f"next-{index}")
    assert instance.reply_bytes <= measurement_v5.MAX_CACHED_REPLY_BYTES
    assert instance.reply_file is not None
    (tmp_path / "data").write_text("later changed bytes", encoding="utf-8")
    assert measure(instance, copy.deepcopy(value)) == first
    first["result"]["findings"][-1]["observed"] = "caller mutation"
    assert measure(instance, copy.deepcopy(value))["result"]["findings"][-1]["observed"] == text
    stream = instance.reply_file
    instance.close()
    assert stream.closed


def test_spooled_reply_corruption_is_not_returned_as_an_observation(tmp_path):
    (tmp_path / "data").write_bytes(b"x" * 300000)
    instance, value = engine(tmp_path), payload(tmp_path, ["data"], "contains", "x")
    measure(instance, value)
    instance.reply_file.seek(0)
    instance.reply_file.write(b"!")
    with pytest.raises(measurement_v5.MeasurementProtocolError, match="CACHE_CORRUPT"):
        measure(instance, copy.deepcopy(value))
    instance.close()
