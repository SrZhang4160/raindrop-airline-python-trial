from __future__ import annotations

import os
from dataclasses import dataclass

from agents import set_tracing_disabled


ANTHROPIC_DEFAULT_MODEL = "litellm/anthropic/claude-sonnet-5"


@dataclass(frozen=True)
class ModelSelection:
    provider: str
    model: str | None


def select_model() -> ModelSelection:
    """Select the runtime model provider from environment keys."""
    if os.environ.get("OPENAI_API_KEY"):
        return ModelSelection(provider="openai", model=None)

    if os.environ.get("ANTHROPIC_API_KEY"):
        set_tracing_disabled(disabled=not bool(os.environ.get("RAINDROP_WRITE_KEY")))
        return ModelSelection(provider="anthropic", model=ANTHROPIC_DEFAULT_MODEL)

    raise RuntimeError("Set OPENAI_API_KEY or ANTHROPIC_API_KEY before starting the agent.")
