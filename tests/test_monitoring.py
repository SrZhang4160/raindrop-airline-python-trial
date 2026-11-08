from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from airline_support import main


@pytest.fixture
def monitoring(monkeypatch, tmp_path):
    monkeypatch.setenv("AIRLINE_SUPPORT_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("RAINDROP_WRITE_KEY", "synthetic-write-key")
    monkeypatch.setenv("RAINDROP_PROJECT_ID", "trial-project")
    monkeypatch.setenv("RAINDROP_DEBUG", "true")
    monkeypatch.setattr(main.getpass, "getuser", lambda: "local-operator")
    client_factory = Mock()
    wrapper_factory = Mock()
    monkeypatch.setattr(main, "Raindrop", client_factory)
    monkeypatch.setattr(main, "RaindropOpenAIAgents", wrapper_factory)
    return client_factory, wrapper_factory


@pytest.mark.asyncio
async def test_monitoring_uses_real_session_identity_and_preserves_multi_turn_chat(
    monkeypatch, monitoring
):
    client_factory, wrapper_factory = monitoring
    histories = []

    async def stream(messages, *, trajectory_path):
        histories.append([(m.role, m.content) for m in messages])
        yield "Answer "
        yield messages[-1].content

    monkeypatch.setattr(main, "stream_agent_response", stream)
    for name in ("first-chat", "second-chat"):
        inputs = iter(["one", "two", "exit"])
        log = Path(await main.chat(
            log_name=name,
            input_func=lambda _: next(inputs),
            output_stream=io.StringIO(),
        ))
        rows = [json.loads(line) for line in log.read_text().splitlines()]
        assert wrapper_factory.call_args.kwargs["convo_id"] == rows[0]["id"] == name
        assert wrapper_factory.call_args.kwargs["user_id"] == "local-operator"
        assert [r["content"] for r in rows if r.get("role") == "assistant"] == ["Answer one", "Answer two"]
    assert histories[1] == [("user", "one"), ("assistant", "Answer one"), ("user", "two")]
    assert wrapper_factory.call_count == 2
    assert wrapper_factory.return_value.shutdown.call_count == 2
    assert client_factory.call_args.kwargs["project_id"] == "trial-project"
    assert client_factory.call_args.kwargs["redact_pii"] is True
    assert client_factory.call_args.kwargs["auto_instrument"] is False
    assert client_factory.call_args.kwargs["debug_logs"] is True


@pytest.mark.asyncio
async def test_without_write_key_no_monitoring_client_is_created(monkeypatch, tmp_path):
    monkeypatch.setenv("AIRLINE_SUPPORT_LOG_DIR", str(tmp_path))
    factory = Mock(side_effect=AssertionError("must not initialize monitoring"))
    monkeypatch.setattr(main, "Raindrop", factory)
    await main.chat(input_func=lambda _: "exit", output_stream=io.StringIO())
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_monitoring_start_failure_does_not_change_answer_or_expose_secrets(
    monkeypatch, monitoring, caplog
):
    client_factory, _ = monitoring
    client_factory.side_effect = RuntimeError("sensitive-canary")

    async def stream(*args, **kwargs):
        yield "The answer."

    monkeypatch.setattr(main, "stream_agent_response", stream)
    inputs = iter(["Hello", "exit"])
    output = io.StringIO()
    await main.chat(input_func=lambda _: next(inputs), output_stream=output)
    assert "The answer." in output.getvalue()
    assert "continuing the chat" in caplog.text
    assert "sensitive-canary" not in caplog.text + output.getvalue()
    assert "synthetic-write-key" not in caplog.text + output.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("agent failure"), asyncio.CancelledError()])
async def test_agent_failure_and_cancellation_drain_monitoring_and_preserve_original_error(
    monkeypatch, monitoring, caplog, failure
):
    _, wrapper_factory = monitoring
    wrapper_factory.return_value.shutdown.side_effect = RuntimeError("sensitive-canary")

    async def stream(*args, **kwargs):
        raise failure
        yield  # Make this the same async-generator interface as the real stream.

    monkeypatch.setattr(main, "stream_agent_response", stream)
    with pytest.raises(type(failure)) as caught:
        await main.chat(input_func=lambda _: "Hello", output_stream=io.StringIO())
    assert caught.value is failure
    wrapper_factory.return_value.shutdown.assert_called_once()
    assert "sensitive-canary" not in caplog.text
