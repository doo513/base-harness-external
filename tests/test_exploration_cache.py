"""Exploration caches are optional data accelerators, never verification evidence."""
import json
import os
from pathlib import Path

import pytest

from change_lens import analyze
from change_lens.scanner import AnalysisError


def source(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "a.py").write_text("value = 1\n", encoding="utf-8")
    (root / "b.py").write_text("from a import value\n", encoding="utf-8")
    return root


def test_warm_cache_reuses_ast_without_reopening_unchanged_sources(tmp_path, monkeypatch):
    from change_lens import scanner
    root = source(tmp_path)
    cache = tmp_path / "cache"
    first = analyze(root, cache_dir=cache)
    reads = []
    original = scanner.regular_bytes
    def tracked(path, maximum):
        if Path(path).is_relative_to(root):
            reads.append(str(path))
        return original(path, maximum)
    monkeypatch.setattr(scanner, "regular_bytes", tracked)
    second = analyze(root, cache_dir=cache)
    assert not reads
    assert second.report()["imports"] == first.report()["imports"]
    assert second.cache_info["reused_files"] == 2
    assert second.cache_info["parsed_files"] == 0
    assert second.cache_info["freshness"] == "filesystem_metadata_only"
    assert second.cache_info["verification_eligible"] is False


def test_edit_add_remove_and_rename_refresh_graph(tmp_path):
    root = source(tmp_path)
    cache = tmp_path / "cache"
    analyze(root, cache_dir=cache)
    (root / "b.py").write_text("import c\n", encoding="utf-8")
    (root / "a.py").rename(root / "c.py")
    current = analyze(root, cache_dir=cache)
    assert {m["path"] for m in current.modules} == {"b.py", "c.py"}
    assert {(e["source"], e["target"]) for e in current.edges} == {("b.py", "c.py")}
    (root / "c.py").unlink()
    removed = analyze(root, cache_dir=cache)
    assert not removed.edges and removed.unresolved


def test_same_size_and_restored_mtime_does_not_hide_an_edit(tmp_path):
    root = source(tmp_path)
    cache = tmp_path / "cache"
    first = analyze(root, cache_dir=cache)
    file = root / "a.py"
    before = file.stat()
    file.write_text("value = 2\n", encoding="utf-8")
    os.utime(file, ns=(before.st_atime_ns, before.st_mtime_ns))
    second = analyze(root, cache_dir=cache)
    assert second.by_path["a.py"]["sha256"] != first.by_path["a.py"]["sha256"]


def test_refresh_bypasses_cache_and_rechecks_content(tmp_path):
    root = source(tmp_path)
    cache = tmp_path / "cache"
    analyze(root, cache_dir=cache)
    refreshed = analyze(root, cache_dir=cache, refresh=True)
    assert refreshed.cache_info["reused_files"] == 0
    assert refreshed.cache_info["parsed_files"] == 2
    assert refreshed.cache_info["freshness"] == "content_read"


def test_omitted_and_explicit_default_limits_share_the_same_cache(tmp_path):
    root = source(tmp_path)
    cache = tmp_path / "cache"
    analyze(root, cache_dir=cache)
    explicit = analyze(root, cache_dir=cache, max_files=5000, max_file_bytes=2_000_000, max_total_bytes=32_000_000)
    assert explicit.cache_info["reused_files"] == 2
    assert len(list(cache.glob("index-*.json"))) == 1


def test_cache_is_not_shared_across_roots_or_scan_limits(tmp_path):
    one = source(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    (other / "a.py").write_text("different = True\n", encoding="utf-8")
    cache = tmp_path / "cache"
    first = analyze(one, cache_dir=cache)
    second = analyze(other, cache_dir=cache)
    assert first.by_path["a.py"]["sha256"] != second.by_path["a.py"]["sha256"]
    limited = analyze(one, cache_dir=cache, max_files=1)
    assert len(limited.modules) == 1 and not limited.report()["complete_local_scan"]


def test_corrupt_cache_is_rebuilt_and_reported(tmp_path):
    root = source(tmp_path)
    cache = tmp_path / "cache"
    analyze(root, cache_dir=cache)
    stored = next(cache.glob("index-*.json"))
    stored.write_text("not json", encoding="utf-8")
    recovered = analyze(root, cache_dir=cache)
    assert recovered.cache_info["reused_files"] == 0
    assert recovered.cache_info["read_status"] == "invalid_rebuilt"
    assert len(recovered.modules) == 2
    json.loads(stored.read_text(encoding="utf-8"))


def test_cache_cannot_write_into_analyzed_workspace(tmp_path):
    root = source(tmp_path)
    with pytest.raises(AnalysisError):
        analyze(root, cache_dir=root / ".cache")
    assert not (root / ".cache").exists()


def test_cache_cannot_read_or_replace_symlink_entries(tmp_path):
    root = source(tmp_path)
    cache = tmp_path / "cache"
    analyze(root, cache_dir=cache)
    stored = next(cache.glob("index-*.json"))
    victim = tmp_path / "unrelated.json"
    victim.write_text("preserved", encoding="utf-8")
    stored.unlink()
    stored.symlink_to(victim)
    with pytest.raises(AnalysisError):
        analyze(root, cache_dir=cache)
    assert victim.read_text(encoding="utf-8") == "preserved"


def test_new_symlink_is_skipped_even_after_cached_regular_file(tmp_path):
    root = source(tmp_path)
    cache = tmp_path / "cache"
    analyze(root, cache_dir=cache)
    (root / "a.py").unlink()
    (root / "a.py").symlink_to(root / "b.py")
    current = analyze(root, cache_dir=cache)
    assert "a.py" not in current.by_path
    assert any(item["code"] == "LINK_SKIPPED" for item in current.diagnostics)
