import importlib.util
from pathlib import Path

import pytest


def wrapper():
    path = Path(__file__).resolve().parents[1] / "scripts/luna_study.py"
    spec = importlib.util.spec_from_file_location("luna_wrapper_test", path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_luna_variant_cannot_silently_call_astra(tmp_path):
    module = wrapper()
    argv = module.luna_argv("codex", tmp_path, tmp_path, "test task")
    assert argv[argv.index("-m") + 1] == "gpt-6-luna"
    assert argv[argv.index("-c") + 1] == 'model_reasoning_effort="medium"'
    with pytest.raises(ValueError):
        module.luna_argv("other", tmp_path, tmp_path, "test task")


def test_provider_rejection_is_not_an_artifact_repair_attempt(monkeypatch):
    module = wrapper()
    monkeypatch.setattr(module, "original_invoke", lambda *a, **k: {"errors": [{"message": "quota"}], "tool_calls": 0, "claim": None})
    with pytest.raises(RuntimeError, match="before task execution"):
        module.invoke()
