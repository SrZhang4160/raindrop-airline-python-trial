#!/usr/bin/env python3
"""Reference in-container launcher: drives one conversation with your adapter. Standard library only.

  python3 launcher.py --scenario /app/scenario.json --trajectory /logs/agent/trajectory.json \
      --adapter "python3 /app/adapter.py"

The adapter is started once and kept alive for the whole conversation. For each user turn
the launcher writes one JSON line to its stdin and reads one JSON line from its stdout:

  -> {"turn": 0, "message": "<user text>", "history": [{"role": "user"|"assistant", "content": "..."}]}
  <- {"reply": "<the agent's reply>"}

The adapter's stdout carries only these lines; diagnostics go to stderr. The adapter (or the
recorder it attaches) writes the cumulative ATIF trajectory to $EVALS_TRAJECTORY_PATH. After
each turn the launcher checks that the trajectory gained a user step and an agent step.

Scenario file:
  {"turns": ["first message", "second message"]}                       fixed turns
  {"user": {"instructions": "You are a customer who ...", "max_turns": 6,
            "opening": "optional first message", "model": "anthropic:claude-opus-5-5"}}
                                                                        a model plays the user
A simulated user needs its key in EVALS_USER_SIM_KEY (Anthropic or OpenAI, per the model
prefix). The launcher removes that key, and every name listed in EVALS_HIDE_ENV, from the
adapter's environment. Note the trade-off: the key still enters the container; a host-side
relay keeps it out if that matters for the agent under test.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import urllib.request

DONE = "###DONE###"


def log(message: str) -> None:
    print(f"[launcher] {message}", file=sys.stderr, flush=True)


def step_counts(path: str):
    try:
        with open(path, encoding="utf-8") as handle:
            steps = json.load(handle).get("steps") or []
    except (OSError, ValueError):
        return None
    return (sum(1 for s in steps if s.get("source") == "user"), sum(1 for s in steps if s.get("source") == "agent"))


def post_json(url: str, headers: dict, body: dict, timeout: int = 300) -> dict:
    request = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                     headers={"content-type": "application/json", **headers})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def simulated_user_turn(spec: dict, history: list, key: str) -> tuple[str, bool]:
    """Ask a model for the simulated user's next message. Roles are swapped: the model plays the user."""
    provider, _, model = spec.get("model", "anthropic:claude-opus-5-5").partition(":")
    system = (spec["instructions"] + f"\n\nYou are the user in this conversation, not the assistant. Write only your "
              f"next message. When your goal is met or the conversation should end, reply with exactly {DONE}.")
    turns = [{"role": "assistant" if m["role"] == "user" else "user", "content": m["content"]} for m in history]
    if not turns:
        turns = [{"role": "user", "content": "(The conversation starts. Write your first message.)"}]
    if provider == "openai":
        data = post_json("https://api.openai.com/v1/chat/completions", {"authorization": f"Bearer {key}"},
                         {"model": model, "messages": [{"role": "system", "content": system}] + turns})
        text = data["choices"][0]["message"].get("content") or ""
    else:
        data = post_json("https://api.anthropic.com/v1/messages",
                         {"x-api-key": key, "anthropic-version": "2026-10-21"},
                         {"model": model, "max_tokens": 4000, "system": system, "messages": turns,
                          "output_config": {"effort": "low"}})
        if data.get("stop_reason") == "refusal":
            raise RuntimeError("the simulated user's model declined to continue")
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    text = text.strip()
    return text, (not text or text == DONE)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--trajectory", required=True)
    parser.add_argument("--adapter", required=True, help="shell command that starts the adapter")
    args = parser.parse_args()
    with open(args.scenario, encoding="utf-8") as handle:
        scenario = json.load(handle)

    sim_key = os.environ.get("EVALS_USER_SIM_KEY", "")
    hidden = {n for n in os.environ.get("EVALS_HIDE_ENV", "").split(",") if n} | {"EVALS_USER_SIM_KEY", "EVALS_HIDE_ENV"}
    child_env = {k: v for k, v in os.environ.items() if k not in hidden}
    child_env["EVALS_TRAJECTORY_PATH"] = args.trajectory

    adapter = subprocess.Popen(args.adapter, shell=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=None, text=True, bufsize=1, env=child_env, start_new_session=True)
    history: list[dict] = []
    problems = 0
    user_spec = scenario.get("user")
    fixed = scenario.get("turns")
    if not user_spec and not isinstance(fixed, list):
        log("scenario needs 'turns' (a list of messages) or 'user' (a simulated user)")
        return 2
    max_turns = int(user_spec.get("max_turns", 6)) if user_spec else len(fixed)
    try:
        for turn in range(max_turns):
            if user_spec:
                if turn == 0 and user_spec.get("opening"):
                    message, done = user_spec["opening"], False
                else:
                    message, done = simulated_user_turn(user_spec, history, sim_key)
                if done:
                    break
            else:
                message = fixed[turn]
            before = step_counts(args.trajectory) or (0, 0)
            adapter.stdin.write(json.dumps({"turn": turn, "message": message, "history": history}) + "\n")
            adapter.stdin.flush()
            line = read_line(adapter, timeout=float(os.environ.get("EVALS_TURN_TIMEOUT", "1800")))
            if line is None:
                log(f"turn {turn}: the adapter exited or sent nothing")
                return 3
            try:
                reply = json.loads(line)["reply"]
            except (ValueError, KeyError, TypeError):
                log(f"turn {turn}: adapter stdout must be one JSON object with 'reply'; got {line[:200]!r}")
                return 3
            after = step_counts(args.trajectory)
            if after is None:
                log(f"turn {turn}: no readable trajectory at {args.trajectory}")
                problems += 1
            elif after[0] <= before[0] or after[1] <= before[1]:
                log(f"turn {turn}: the trajectory gained no new user step or agent step")
                problems += 1
            history += [{"role": "user", "content": message}, {"role": "assistant", "content": str(reply)}]
    finally:
        shutdown(adapter)
    return 4 if problems else 0


def read_line(process: subprocess.Popen, timeout: float):
    result: list = []
    reader = threading.Thread(target=lambda: result.append(process.stdout.readline()), daemon=True)
    reader.start()
    reader.join(timeout)
    if not result or not result[0]:
        return None
    return result[0].strip()


def shutdown(process: subprocess.Popen) -> None:
    try:
        process.stdin.close()
    except OSError:
        pass
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
