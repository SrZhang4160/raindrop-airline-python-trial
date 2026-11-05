#!/usr/bin/env python3
"""Drive the real SkyServe terminal chat from the launcher's JSON-lines protocol (in-container only).

`airline-support` (start.sh's entrypoint) runs `asyncio.run(airline_support.main.chat())`. This
adapter calls the same `chat()` with an input function fed by the launcher, so the session log,
history replay and the project's own ATIF recorder all run unchanged. Nothing here inspects the
scenario or the expected answer.

  stdin  <- {"turn": n, "message": "...", "history": [...]}   one line per user turn
  stdout -> {"reply": "..."}                                   one line per agent reply

`chat()` keeps its own history in its session log, so the launcher's `history` is unused. The
recorder writes to ATIF_TRAJECTORY_PATH, which is set here from EVALS_TRAJECTORY_PATH.

For test authors: chat()'s own prompt loop is on this path, as it is for real users. A blank
turn is skipped and a turn of exit, quit or q ends the session, so the launcher fails the trial;
never script such turns. Replies are read from what chat() prints after its "Agent:" prefix.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys

os.environ["ATIF_TRAJECTORY_PATH"] = os.environ["EVALS_TRAJECTORY_PATH"]
os.environ.setdefault("AIRLINE_SUPPORT_LOG_DIR", "/tmp/airline-sessions")
PROTOCOL = sys.stdout
sys.stdout = sys.stderr  # anything the app prints must not reach the protocol stream

from airline_support.main import chat  # noqa: E402  (after the environment is set)


class LauncherInput:
    """chat()'s input_func: when chat asks for the next line, the previous reply is complete."""

    def __init__(self, output: io.StringIO) -> None:
        self.output = output
        self.waiting_for_reply = False

    def __call__(self, _prompt: str) -> str:
        if self.waiting_for_reply:
            reply = self.output.getvalue().strip().removeprefix("Agent:").strip()
            PROTOCOL.write(json.dumps({"reply": reply}) + "\n")
            PROTOCOL.flush()
        line = sys.stdin.readline()
        if not line:
            raise EOFError  # chat() ends the session on EOF, as it does for a closed terminal
        self.output.seek(0)
        self.output.truncate()
        self.waiting_for_reply = True
        return json.loads(line)["message"]


def main() -> int:
    output = io.StringIO()
    asyncio.run(chat(input_func=LauncherInput(output), output_stream=output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
