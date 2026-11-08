from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import os
import sys
from collections.abc import Callable
from typing import TextIO

from dotenv import load_dotenv
from raindrop import Raindrop
from raindrop_openai_agents import RaindropOpenAIAgents

from airline_support.agent import stream_agent_response
from airline_support.sessions import (
    append_message,
    create_session,
    read_messages,
    session_id_from_log_name,
    session_path,
)
from airline_support.trajectory import get_recorder

EXIT_COMMANDS = {"exit", "quit", "q"}


async def chat(
    log_name: str | None = None,
    input_func: Callable[[str], str] = input,
    output_stream: TextIO | None = None,
) -> str:
    load_dotenv()
    output = output_stream or sys.stdout
    session_id = session_id_from_log_name(log_name) if log_name else None
    session = create_session(session_id=session_id, overwrite=log_name is not None)
    log_path = session_path(session.id)
    trajectory_path = log_path.with_suffix(".atif.json")
    recorder = get_recorder(trajectory_path, new_session=True)
    monitoring = None
    if os.environ.get("RAINDROP_WRITE_KEY"):
        try:
            debug = os.environ.get("RAINDROP_DEBUG", "false").lower() == "true"
            monitoring = RaindropOpenAIAgents(
                client=Raindrop(
                    api_key=os.environ["RAINDROP_WRITE_KEY"],
                    project_id=os.environ.get("RAINDROP_PROJECT_ID") or None,
                    tracing_enabled=True,
                    auto_instrument=False,
                    bypass_otel_for_tools=True,
                    redact_pii=True,
                    debug_logs=debug,
                ),
                user_id=getpass.getuser(),
                convo_id=session.id,
                debug=debug,
            )
        except Exception:
            logging.getLogger(__name__).warning(
                "Raindrop monitoring could not start; continuing the chat."
            )

    print("SkyServe Airline Support", file=output)
    print(f"Session: {session.id}", file=output)
    print(f"Log: {log_path}", file=output)
    if recorder is not None:
        print(f"Trajectory: {recorder.path}", file=output)
    print("Type exit, quit, or q to end the chat.", file=output)

    try:
        while True:
            try:
                user_text = input_func("\nYou: ")
            except EOFError:
                print(file=output)
                break

            user_text = user_text.strip()
            if not user_text:
                continue
            if user_text.lower() in EXIT_COMMANDS:
                break

            append_message(session.id, "user", user_text)
            messages = read_messages(session.id)
            chunks: list[str] = []

            print("\nAgent: ", end="", file=output, flush=True)
            async for delta in stream_agent_response(
                messages, trajectory_path=trajectory_path
            ):
                chunks.append(delta)
                print(delta, end="", file=output, flush=True)
            print(file=output)

            assistant_text = "".join(chunks).strip()
            if assistant_text:
                append_message(session.id, "assistant", assistant_text)
    finally:
        if monitoring is not None:
            try:
                await asyncio.to_thread(monitoring.shutdown)
            except Exception:
                logging.getLogger(__name__).warning(
                    "Raindrop monitoring could not flush; local chat logs are retained."
                )

    print(f"\nSaved conversation log to {log_path}", file=output)
    return str(log_path)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Chat with the SkyServe airline support agent."
    )
    parser.add_argument(
        "log_name",
        nargs="?",
        help="Optional session log file name, saved as logs/<name>.jsonl. Existing named logs are overwritten.",
    )
    return parser.parse_args(argv)


def run() -> None:
    args = parse_args()
    asyncio.run(chat(log_name=args.log_name))


def main() -> None:
    run()


if __name__ == "__main__":
    main()
