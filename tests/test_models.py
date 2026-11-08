from __future__ import annotations

import pytest

from airline_support import models


def clear_api_keys(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def test_select_model_prefers_openai(monkeypatch):
    clear_api_keys(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")

    selection = models.select_model()

    assert selection.provider == "openai"
    assert selection.model is None


def test_select_model_uses_anthropic_when_openai_is_absent(monkeypatch):
    clear_api_keys(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
    calls = []
    monkeypatch.setattr(models, "set_tracing_disabled", lambda disabled: calls.append(disabled))

    selection = models.select_model()

    assert selection.provider == "anthropic"
    assert selection.model == "litellm/anthropic/claude-sonnet-5"
    assert calls == [True]


def test_select_model_prefers_openai_when_both_keys_are_set(monkeypatch):
    clear_api_keys(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")

    selection = models.select_model()

    assert selection.provider == "openai"
    assert selection.model is None


def test_anthropic_keeps_tracing_available_when_monitoring_is_configured(monkeypatch):
    clear_api_keys(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
    monkeypatch.setenv("RAINDROP_WRITE_KEY", "synthetic-write-key")
    calls = []
    monkeypatch.setattr(models, "set_tracing_disabled", lambda disabled: calls.append(disabled))

    selection = models.select_model()

    assert selection.provider == "anthropic"
    assert selection.model == models.ANTHROPIC_DEFAULT_MODEL
    assert calls == [False]


def test_select_model_requires_a_supported_api_key(monkeypatch):
    clear_api_keys(monkeypatch)

    with pytest.raises(RuntimeError, match="OPENAI_API_KEY or ANTHROPIC_API_KEY"):
        models.select_model()
