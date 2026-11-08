from __future__ import annotations

import asyncio
import io
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from agents import (
    Agent,
    Model,
    RunConfig,
    Runner,
    UserError,
    function_tool,
    set_tracing_disabled,
)
from openai.types.responses import (
    Response,
    ResponseCompletedEvent,
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
    ResponseTextDeltaEvent,
)
from openai.types.responses.response_usage import ResponseUsage

from airline_support import agent as sample
from airline_support import main, trajectory
from airline_support.sessions import ChatMessage
from airline_support.trajectory import TrajectoryHooks, TrajectoryRecorder


def message(text):
    return ResponseOutputMessage(
        id="message",
        type="message",
        role="assistant",
        status="completed",
        content=[ResponseOutputText(type="output_text", text=text, annotations=[])],
    )


def call(call_id="call", name="lookup", arguments='{"code":"SKY123"}'):
    return ResponseFunctionToolCall(
        id=call_id,
        type="function_call",
        call_id=call_id,
        name=name,
        arguments=arguments,
    )


class FakeModel(Model):
    """Drive the real SDK runner with observed responses, without network access."""

    def __init__(self, outputs):
        self.outputs = iter(outputs)

    async def get_response(self, *args, **kwargs):
        raise AssertionError("The sample must retain its streaming execution path")

    async def stream_response(self, *args, **kwargs):
        output = next(self.outputs)
        if isinstance(output, BaseException):
            raise output
        for item in output:
            if item.type == "message":
                yield ResponseTextDeltaEvent(
                    type="response.output_text.delta",
                    item_id=item.id,
                    output_index=0,
                    content_index=0,
                    delta=item.content[0].text,
                    sequence_number=0,
                    logprobs=[],
                )
        response = Response.model_construct(
            id="response",
            created_at=0,
            object="response",
            model="fake",
            output=output,
            status="completed",
            usage=ResponseUsage(
                input_tokens=10,
                output_tokens=5,
                total_tokens=15,
                input_tokens_details={"cached_tokens": 2, "cache_write_tokens": 0},
                output_tokens_details={"reasoning_tokens": 0},
            ),
        )
        yield ResponseCompletedEvent(
            type="response.completed", response=response, sequence_number=1
        )


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch, tmp_path):
    monkeypatch.setattr(trajectory, "_recorders", {})
    monkeypatch.delenv("ATIF_TRAJECTORY_PATH", raising=False)
    monkeypatch.setenv("AIRLINE_SUPPORT_LOG_DIR", str(tmp_path))
    # No hosted tracing exporter or provider requests, even when the fake runner is used.
    monkeypatch.setenv("OPENAI_AGENTS_DISABLE_TRACING", "1")
    set_tracing_disabled(True)


def document(path):
    return json.loads(path.read_text())


@pytest.mark.asyncio
async def test_monitoring_catalog_uses_the_actual_tools_without_changing_stream(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("RAINDROP_WRITE_KEY", "synthetic-write-key")

    @function_tool
    def policy(ticket_type: str) -> str:
        """Return the test policy."""
        return "One bag."

    target = Agent(name="catalog-agent", model=FakeModel([[message("Answer.")]]), tools=[policy])
    monkeypatch.setattr(sample, "create_airline_agent", lambda: target)
    original_run = Runner.run_streamed
    configs = []

    def observed_run(*args, **kwargs):
        configs.append(kwargs["run_config"])
        return original_run(*args, **kwargs)

    monkeypatch.setattr(Runner, "run_streamed", observed_run)
    answer = "".join([part async for part in sample.stream_agent_response(
        [ChatMessage("user", "Policy?", "now")], trajectory_path=tmp_path / "catalog.json"
    )])
    assert answer == "Answer."
    assert configs[0].workflow_name == target.name
    catalog = configs[0].trace_metadata["raindrop_tools"]
    assert catalog[0]["name"] == policy.name
    assert catalog[0]["inputSchema"] == policy.params_json_schema
    assert catalog[0]["description"] == policy.description


@pytest.mark.asyncio
async def test_real_sdk_three_turns_record_before_tool_dispatch(monkeypatch, tmp_path):
    path = tmp_path / "trajectory.json"
    monkeypatch.setenv("ATIF_TRAJECTORY_PATH", str(path))
    snapshots = []

    @function_tool
    def lookup(code: str) -> str:
        current = document(path)
        assert current["steps"][-1]["tool_calls"][0]["arguments"] == {"code": code}
        assert "observation" not in current["steps"][-1]
        return f"Booking {code} found"

    model = FakeModel(
        [
            [call()],
            [message("Found SKY123")],
            [message("Seat 12A")],
            [message("Goodbye")],
        ]
    )
    monkeypatch.setattr(
        sample,
        "create_airline_agent",
        lambda: Agent(
            name="SkyServe Airline Support",
            model=model,
            tools=[lookup],
        ),
    )
    history = []
    for user, expected in [
        ("Find SKY123", "Found SKY123"),
        ("Which seat?", "Seat 12A"),
        ("Thanks", "Goodbye"),
    ]:
        history.append(ChatMessage("user", user, "now"))
        output = "".join(
            [chunk async for chunk in sample.stream_agent_response(history)]
        )
        assert output == expected
        history.append(ChatMessage("assistant", output, "now"))
        current = document(path)
        if snapshots:
            prefix = snapshots[-1]["steps"]
            assert current["steps"][: len(prefix)] == prefix
        snapshots.append(current)
    steps = snapshots[-1]["steps"]
    assert [step["message"] for step in steps if step["source"] == "user"] == [
        "Find SKY123",
        "Which seat?",
        "Thanks",
    ]
    assert [step["step_id"] for step in steps] == list(range(1, len(steps) + 1))
    assert steps[1]["observation"]["results"] == [
        {"source_call_id": "call", "content": "Booking SKY123 found"}
    ]
    assert steps[1]["metrics"] == {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "cached_tokens": 2,
    }
    assert snapshots[-1]["schema_version"] == "ATIF-v1.7"
    assert snapshots[-1]["final_metrics"] == {
        "total_steps": 7,
        "total_prompt_tokens": 40,
        "total_completion_tokens": 20,
        "total_cached_tokens": 8,
    }


@pytest.mark.asyncio
async def test_parallel_tools_and_reused_call_ids_are_linked_to_their_run(tmp_path):
    path = tmp_path / "trajectory.json"
    recorder = TrajectoryRecorder(path)

    @function_tool
    async def lookup(code: str) -> str:
        await asyncio.sleep(0)
        return code

    for turn in range(2):
        recorder.user(f"turn {turn}")
        model = FakeModel([[call("a"), call("b")], [message("Done")]])
        await drain_run(recorder, model, lookup)
    steps = document(path)["steps"]
    for step in [steps[1], steps[4]]:
        assert {item["source_call_id"] for item in step["observation"]["results"]} == {
            "a",
            "b",
        }
        assert len(step["observation"]["results"]) == 2


async def drain_run(recorder, model, tool):
    result = Runner.run_streamed(
        Agent(name="sample", model=model, tools=[tool]),
        input="lookup",
        hooks=TrajectoryHooks(recorder),
        run_config=RunConfig(tracing_disabled=True),
    )
    async for _ in result.stream_events():
        pass


@pytest.mark.asyncio
async def test_tool_error_feedback_is_recorded_as_observed(tmp_path):
    path = tmp_path / "trajectory.json"
    recorder = TrajectoryRecorder(path)
    recorder.user("Find SKY123")

    @function_tool
    def lookup(code: str) -> str:
        raise ValueError("Booking service failed")

    await drain_run(
        recorder, FakeModel([[call()], [message("Unable to look up booking")]]), lookup
    )
    observation = document(path)["steps"][1]["observation"]["results"][0]
    assert observation["source_call_id"] == "call"
    assert "An error occurred while running the tool" in observation["content"]


@pytest.mark.asyncio
async def test_unhandled_tool_failure_retains_call_and_propagates(
    monkeypatch, tmp_path
):
    path = tmp_path / "trajectory.json"
    monkeypatch.setenv("ATIF_TRAJECTORY_PATH", str(path))
    failure = ValueError("Booking service failed")

    @function_tool(failure_error_function=None)
    def lookup(code: str) -> str:
        raise failure

    model = FakeModel([[call()]])
    monkeypatch.setattr(
        sample,
        "create_airline_agent",
        lambda: Agent(name="sample", model=model, tools=[lookup]),
    )
    with pytest.raises(UserError) as caught:
        _ = [
            chunk
            async for chunk in sample.stream_agent_response(
                [ChatMessage("user", "Find SKY123", "now")]
            )
        ]
    current = document(path)
    assert caught.value.__cause__ is failure
    assert current["steps"][1]["tool_calls"][0]["tool_call_id"] == "call"
    assert "observation" not in current["steps"][1]
    assert "Booking service failed" in current["extra"]["errors"][0]["message"]


@pytest.mark.asyncio
async def test_malformed_call_keeps_observed_output_without_fabricating_arguments(
    tmp_path,
):
    from agents.items import ModelResponse
    from agents.usage import Usage

    path = tmp_path / "trajectory.json"
    recorder = TrajectoryRecorder(path)
    recorder.user("Find booking")
    malformed = call(arguments="not-json")
    await TrajectoryHooks(recorder).on_llm_end(
        None,
        Agent(name="sample"),
        ModelResponse(output=[malformed], usage=Usage(), response_id=None),
    )
    step = document(path)["steps"][1]
    assert "not-json" in step["message"]
    assert step["extra"]["invalid_tool_calls"][0]["arguments"] == "not-json"
    assert "tool_calls" not in step
    assert "metrics" not in step
    assert "total_prompt_tokens" not in document(path)["final_metrics"]


def test_overlapping_writes_are_atomic_and_cumulative(monkeypatch, tmp_path):
    path = tmp_path / "trajectory.json"
    recorder = TrajectoryRecorder(path)
    replace = trajectory.os.replace
    destinations = []

    def checked_replace(source, target):
        current = document(source)
        assert source.parent == path.parent
        assert [s["step_id"] for s in current["steps"]] == list(
            range(1, len(current["steps"]) + 1)
        )
        destinations.append(source)
        replace(source, target)

    monkeypatch.setattr(trajectory.os, "replace", checked_replace)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(recorder.user, [f"input {i}" for i in range(20)]))
    assert len(document(path)["steps"]) == 20
    assert len(set(destinations)) == 20
    assert list(tmp_path.iterdir()) == [path]


def test_redacts_all_fields_and_declared_secret_values(monkeypatch, tmp_path):
    path = tmp_path / "trajectory.json"
    monkeypatch.setenv("OPENAI_API_KEY", "opaque-openai-value")
    monkeypatch.setenv("CUSTOM_KEY", "opaque-custom-value")
    monkeypatch.setenv("ATIF_REDACT_ENV_VARS", "CUSTOM_KEY")
    recorder = TrajectoryRecorder(path)
    recorder.record(
        lambda: recorder.append(
            "agent",
            "opaque-openai-value opaque-custom-value",
            extra={
                "nested": [
                    {
                        "api_key": "hidden",
                        "Authorization": "hidden",
                        "refreshToken": "hidden",
                        "user_token": "hidden",
                    }
                ],
                "serialized": '{"password":"hidden"}',
                "text": 'Authorization: Bearer other-secret; OPENAI_API_KEY="another-secret" sk-example-secret',
                "private": "-----BEGIN PRIVATE KEY-----\nprivate-data\n-----END PRIVATE KEY-----",
                "prompt_tokens": 10,
            },
        )
    )
    saved = path.read_text()
    for secret in [
        "opaque-openai-value",
        "opaque-custom-value",
        "hidden",
        "other-secret",
        "another-secret",
        "sk-example-secret",
        "private-data",
    ]:
        assert secret not in saved
    assert document(path)["steps"][0]["extra"]["prompt_tokens"] == 10


@pytest.mark.asyncio
async def test_model_failure_preserves_prior_turn_and_original_error(
    monkeypatch, tmp_path
):
    path = tmp_path / "trajectory.json"
    monkeypatch.setenv("ATIF_TRAJECTORY_PATH", str(path))
    failure = RuntimeError("Authorization: Bearer secret-value")
    model = FakeModel([[message("First response")], failure])
    monkeypatch.setattr(
        sample, "create_airline_agent", lambda: Agent(name="sample", model=model)
    )
    first = ChatMessage("user", "first", "now")
    assert [chunk async for chunk in sample.stream_agent_response([first])] == [
        "First response"
    ]
    prefix = document(path)["steps"]
    with pytest.raises(RuntimeError) as caught:
        _ = [
            chunk
            async for chunk in sample.stream_agent_response(
                [first, ChatMessage("user", "second", "now")]
            )
        ]
    assert caught.value is failure
    current = document(path)
    assert current["steps"][: len(prefix)] == prefix
    assert current["steps"][-1]["message"] == "second"
    assert "secret-value" not in path.read_text()


@pytest.mark.asyncio
async def test_recording_failure_does_not_change_response_or_expose_error(
    monkeypatch, tmp_path, capsys
):
    path = tmp_path / "trajectory.json"
    monkeypatch.setenv("ATIF_TRAJECTORY_PATH", str(path))
    model = FakeModel([[message("Response")]])
    monkeypatch.setattr(
        sample, "create_airline_agent", lambda: Agent(name="sample", model=model)
    )

    def fail(*args):
        raise OSError("sensitive filesystem error")

    monkeypatch.setattr(trajectory.os, "replace", fail)
    assert [
        chunk
        async for chunk in sample.stream_agent_response(
            [ChatMessage("user", "Hello", "now")]
        )
    ] == ["Response"]
    stderr = capsys.readouterr().err
    assert "recording failed" in stderr
    assert "sensitive filesystem error" not in stderr
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_default_recorder_records_without_configuration(monkeypatch, tmp_path):
    model = FakeModel([[message("Response")]])
    monkeypatch.setattr(
        sample, "create_airline_agent", lambda: Agent(name="sample", model=model)
    )
    assert [
        chunk
        async for chunk in sample.stream_agent_response(
            [ChatMessage("user", "Hello", "now")]
        )
    ] == ["Response"]
    paths = list(tmp_path.glob("session-*.atif.json"))
    assert len(paths) == 1
    assert [step["message"] for step in document(paths[0])["steps"]] == [
        "Hello",
        "Response",
    ]


@pytest.mark.asyncio
async def test_cancelled_run_preserves_evidence_and_propagates(monkeypatch, tmp_path):
    path = tmp_path / "trajectory.json"
    monkeypatch.setenv("ATIF_TRAJECTORY_PATH", str(path))
    entered = asyncio.Event()

    class WaitingModel(FakeModel):
        async def stream_response(self, *args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
            yield  # The model remains in progress until cancelled.

    monkeypatch.setattr(
        sample,
        "create_airline_agent",
        lambda: Agent(name="sample", model=WaitingModel([])),
    )

    async def consume():
        return [
            chunk
            async for chunk in sample.stream_agent_response(
                [ChatMessage("user", "Pending input", "now")]
            )
        ]

    task = asyncio.create_task(consume())
    await entered.wait()
    prefix = document(path)["steps"]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert document(path)["steps"] == prefix
    assert document(path)["extra"]["errors"][0]["type"] == "CancelledError"


@pytest.mark.asyncio
@pytest.mark.parametrize("log_name", [None, "booking-help"])
@pytest.mark.parametrize("override", [False, True])
async def test_terminal_records_turns_and_starts_fresh_sessions(
    monkeypatch, tmp_path, log_name, override
):
    override_path = tmp_path / "integration" / "trajectory.json"
    if override:
        monkeypatch.setenv("ATIF_TRAJECTORY_PATH", str(override_path))
    model = FakeModel(
        [
            [message("First answer")],
            [message("Second answer")],
            [message("New session")],
        ]
    )
    monkeypatch.setattr(
        sample, "create_airline_agent", lambda: Agent(name="sample", model=model)
    )
    inputs = iter(["First question", "Second question", "exit"])
    output = io.StringIO()
    first_log = Path(
        await main.chat(
            log_name=log_name,
            input_func=lambda _prompt: next(inputs),
            output_stream=output,
        )
    )
    first_path = override_path if override else first_log.with_suffix(".atif.json")
    assert f"Trajectory: {first_path}" in output.getvalue()
    assert [step["message"] for step in document(first_path)["steps"]] == [
        "First question",
        "First answer",
        "Second question",
        "Second answer",
    ]
    inputs = iter(["Fresh question", "exit"])
    second_log = Path(
        await main.chat(
            log_name=log_name,
            input_func=lambda _prompt: next(inputs),
            output_stream=io.StringIO(),
        )
    )
    second_path = override_path if override else second_log.with_suffix(".atif.json")
    assert [step["message"] for step in document(second_path)["steps"]] == [
        "Fresh question",
        "New session",
    ]
    if not override and log_name is None:
        assert first_path != second_path
        assert len(document(first_path)["steps"]) == 4
    elif override:
        assert not first_log.with_suffix(".atif.json").exists()
