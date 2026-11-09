from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterable
from pathlib import Path

from agents import Agent, RunConfig, Runner, function_tool
from openai.types.responses import ResponseTextDeltaEvent

from airline_support.models import select_model
from airline_support.sessions import ChatMessage
from airline_support.trajectory import TrajectoryHooks, get_recorder

BOOKINGS: dict[str, dict[str, str]] = {
    "SKY123": {
        "passenger": "Maya Chen",
        "route": "JFK to SFO",
        "departure": "2026-07-14 09:30",
        "seat": "12A",
        "status": "confirmed",
    },
    "BAY777": {
        "passenger": "Noah Patel",
        "route": "LAX to SEA",
        "departure": "2026-07-18 16:45",
        "seat": "18C",
        "status": "confirmed",
    },
}


@function_tool
def lookup_booking(confirmation_code: str) -> str:
    """Look up a demo booking by confirmation code."""
    booking = BOOKINGS.get(confirmation_code.strip().upper())
    if booking is None:
        return "No booking was found for that confirmation code."
    return (
        f"{booking['passenger']} is booked on {booking['route']} departing "
        f"{booking['departure']}. Seat {booking['seat']}. Status: {booking['status']}."
    )


@function_tool
def baggage_policy(ticket_type: str = "standard") -> str:
    """Return the demo airline baggage policy for a ticket type."""
    normalized = ticket_type.strip().lower()
    if normalized in {"basic", "basic economy"}:
        return "Basic economy includes one personal item. Carry-on bags and checked bags require a fee."
    if normalized in {"business", "first", "premium"}:
        return "Premium cabins include one carry-on and two checked bags up to 70 pounds each."
    return "Standard tickets include one carry-on and one checked bag up to 50 pounds."


@function_tool
def change_seat(confirmation_code: str, requested_seat: str) -> str:
    """Change a seat for a demo booking when the confirmation code is valid."""
    code = confirmation_code.strip().upper()
    booking = BOOKINGS.get(code)
    if booking is None:
        return "Seat changes require a valid confirmation code."
    seat = requested_seat.strip().upper()
    return f"Seat updated to {seat} for booking {code}."


AIRLINE_AGENT_INSTRUCTIONS = (
    "You are a concise airline customer support agent for a demo airline. "
    "Help with booking lookups, baggage policy, seat changes, and flight-change guidance. "
    "Before changing seats or discussing a specific booking, ask for and verify the confirmation code. "
    "Do not invent refund amounts, flight availability, fees, or policy exceptions. "
    "Answer naturally and keep responses brief."
)


def create_airline_agent() -> Agent:
    selection = select_model()
    model_kwargs = {"model": selection.model} if selection.model else {}
    return Agent(
        name="SkyServe Airline Support",
        instructions=AIRLINE_AGENT_INSTRUCTIONS,
        tools=[lookup_booking, baggage_policy, change_seat],
        **model_kwargs,
    )


def _messages_to_agent_input(messages: Iterable[ChatMessage]) -> list[dict[str, str]]:
    return [
        {"role": message.role, "content": message.content}
        for message in messages
        if message.role in {"user", "assistant"}
    ]


async def stream_agent_response(
    messages: Iterable[ChatMessage], *, trajectory_path: Path | None = None
) -> AsyncIterator[str]:
    agent_input = _messages_to_agent_input(messages)
    recorder = get_recorder(trajectory_path)
    if recorder is not None and agent_input and agent_input[-1]["role"] == "user":
        # History is replay context; only the new input is a new trajectory step.
        recorder.user(agent_input[-1]["content"])
    kwargs = {"hooks": TrajectoryHooks(recorder)} if recorder is not None else {}
    result = None
    try:
        agent = create_airline_agent()
        if os.environ.get("RAINDROP_WRITE_KEY"):
            kwargs["run_config"] = RunConfig(
                workflow_name=agent.name,
                trace_metadata={
                    "raindrop_tools": [
                        {
                            "type": "function",
                            "name": tool.name,
                            "description": tool.description,
                            "inputSchema": tool.params_json_schema,
                        }
                        for tool in agent.tools
                    ]
                },
            )
        result = Runner.run_streamed(agent, input=agent_input, **kwargs)
        async for event in result.stream_events():
            if event.type == "raw_response_event" and isinstance(event.data, ResponseTextDeltaEvent):
                if event.data.delta:
                    yield event.data.delta
        # Older SDKs stop streaming instead of propagating caller cancellation.
        if recorder is not None and asyncio.current_task().cancelling():
            raise asyncio.CancelledError()
    except BaseException as error:
        if recorder is not None:
            if result is not None:
                result.cancel()
                try:
                    async for _ in result.stream_events():
                        pass
                except BaseException:
                    pass  # Cleanup must not replace the original target error.
            recorder.failure(error)
        raise
