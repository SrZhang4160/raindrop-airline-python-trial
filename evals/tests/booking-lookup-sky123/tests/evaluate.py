"""Reference helper for code evaluators. Copy it into a task's tests/ and import it from your checks
(name your own script anything but evaluate.py, which is this helper).

Writes the score files every agent-evals verifier produces:
  /logs/verifier/reward.json    {"<evaluator-id>": <number>}      one key per declared code evaluator
  /logs/verifier/feedback.json  {"<evaluator-id>": "<text>"}      for every score, pass or fail
  /logs/verifier/errors.json    {"<evaluator-id>": "<message>"}   when a check could not run

Example tests/check.py, run by tests/test.sh as `cd /tests && python3 check.py`:

    from evaluate import Results, load_trajectory, turns, tool_calls

    results = Results()
    trajectory = load_trajectory()
    with results.check("looks-up-order"):
        calls = [c for c in tool_calls(trajectory) if c["function_name"] == "get_order_details"]
        ok = bool(calls)
        results.score("looks-up-order", 1.0 if ok else 0.0,
                      "Called get_order_details." if ok else "Never called get_order_details; it must look the order up.")
    results.write()

A check that raises becomes an error for that evaluator, not a zero: a crash says nothing
about the agent. Score the agent's real failures as low scores, with feedback that says what
full marks need.
"""

from __future__ import annotations

import json
import math
import traceback
from contextlib import contextmanager
from pathlib import Path

TRAJECTORY = Path("/logs/agent/trajectory.json")
VERIFIER = Path("/logs/verifier")


def load_trajectory(path: Path = TRAJECTORY) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def text(message) -> str:
    if isinstance(message, str):
        return message
    if isinstance(message, list):
        return " ".join(str(part.get("text", "")) for part in message if isinstance(part, dict))
    return ""


def turns(trajectory: dict) -> list[dict]:
    """Each user step with every agent and system step up to the next user step."""
    result: list[dict] = []
    for step in trajectory.get("steps", []):
        if step.get("source") == "user" or not result:
            result.append({"user": step if step.get("source") == "user" else None, "steps": []})
            if step.get("source") == "user":
                continue
        result[-1]["steps"].append(step)
    return result


def replies(turn: dict) -> str:
    return "\n".join(text(s.get("message")) for s in turn["steps"] if s.get("source") == "agent")


def tool_calls(trajectory: dict) -> list[dict]:
    """Every tool call, with its result (if recorded) under "result"."""
    calls = []
    for step in trajectory.get("steps", []):
        results = {r.get("source_call_id"): r.get("content") for r in ((step.get("observation") or {}).get("results") or [])}
        for call in step.get("tool_calls") or []:
            calls.append({**call, "result": results.get(call.get("tool_call_id"))})
    return calls


class Results:
    def __init__(self) -> None:
        self.rewards: dict[str, float] = {}
        self.feedback: dict[str, str] = {}
        self.errors: dict[str, str] = {}

    def score(self, evaluator_id: str, value: float, feedback: str) -> None:
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{evaluator_id}: score must be a finite number")
        if not feedback.strip():
            raise ValueError(f"{evaluator_id}: feedback is required for every score")
        self.rewards[evaluator_id] = float(value)
        self.feedback[evaluator_id] = feedback

    @contextmanager
    def check(self, evaluator_id: str):
        try:
            yield
        except Exception:
            self.errors[evaluator_id] = traceback.format_exc(limit=3)[-1500:]

    def write(self, directory: Path = VERIFIER) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "reward.json").write_text(json.dumps(self.rewards))
        (directory / "feedback.json").write_text(json.dumps(self.feedback))
        if self.errors:
            (directory / "errors.json").write_text(json.dumps(self.errors))
