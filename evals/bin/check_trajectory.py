#!/usr/bin/env python3
"""Check an ATIF trajectory: Harbor's own validator plus the agent-evals turn rules.

Usage:
  python3 evals/bin/check_trajectory.py <trajectory.json> [--turns N] [--previous earlier.json]
                                         [--no-harbor] [--json]

Rules on top of Harbor's schema: every user step except a closing one is answered by at least
one agent step before the next user step; every tool call has a result in the same step; with --turns,
the number of user steps matches; with --previous (an earlier snapshot of the same run),
the earlier steps are an exact prefix. Warns when nothing shows a real model call.
Harbor's validator runs through `uv tool run` (pinned Harbor); --no-harbor skips it.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import HARBOR_PYTHON, HARBOR_VERSION, Report  # noqa: E402


def message_text(message) -> str:
    if isinstance(message, str):
        return message
    if isinstance(message, list):
        return " ".join(str(part.get("text", "")) for part in message if isinstance(part, dict))
    return ""


def harbor_validate(path: Path, report: Report) -> None:
    if not shutil.which("uv"):
        report.warn("uv not found, so Harbor's schema validator did not run")
        return
    command = ["uv", "tool", "run", "--python", HARBOR_PYTHON, "--from", f"harbor=={HARBOR_VERSION}",
               "python", "-m", "harbor.utils.trajectory_validator", str(path), "--no-validate-images"]
    try:
        ran = subprocess.run(command, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        report.warn("Harbor's validator timed out")
        return
    if ran.returncode:
        detail = (ran.stdout + ran.stderr).strip()[-2000:]
        report.error(f"Harbor's ATIF validator rejected it:\n{detail}")


def check(doc: dict, report: Report, turns: int | None, previous: dict | None) -> None:
    if not isinstance(doc, dict):
        report.error("not a JSON object")
        return
    version = str(doc.get("schema_version", ""))
    if not version.startswith("ATIF-v"):
        report.error(f"schema_version is {version!r}; expected an ATIF version such as 'ATIF-v1.7'")
    agent = doc.get("agent") or {}
    if not agent.get("name") or not agent.get("version"):
        report.error("agent.name and agent.version are required")
    steps = doc.get("steps")
    if not isinstance(steps, list) or not steps:
        report.error("steps must be a non-empty list")
        return
    for index, step in enumerate(steps, start=1):
        if step.get("step_id") != index:
            report.error(f"step {index}: step_id is {step.get('step_id')!r}; ids run 1, 2, 3, ...")
            break

    user_steps = [s for s in steps if s.get("source") == "user"]
    if not user_steps:
        report.error("no user step; record each input the agent received as a user step")
    if turns is not None and len(user_steps) != turns:
        report.error(f"expected {turns} user turns, found {len(user_steps)}")

    answered = True
    for index, step in enumerate(steps):
        if step.get("source") != "user":
            continue
        following = []
        for later in steps[index + 1:]:
            if later.get("source") == "user":
                break
            following.append(later)
        if not any(s.get("source") == "agent" for s in following):
            preview = message_text(step.get("message"))[:80]
            if index == len(steps) - 1 or not any(s.get("source") == "user" for s in steps[index + 1:]):
                # A closing user message (a goodbye or a simulator's stop signal) needs no reply.
                report.note(f"the last user step ({preview!r}) has no reply; fine if it ends the conversation")
                continue
            answered = False
            report.error(f"user step {step.get('step_id')} ({preview!r}) has no agent step after it")
    agent_steps = [s for s in steps if s.get("source") == "agent"]
    if answered and agent_steps and not any(message_text(s.get("message")).strip() or s.get("tool_calls")
                                            for s in agent_steps):
        report.warn("every agent step is empty; check the recorder captures the agent's messages")

    for step in agent_steps:
        calls = step.get("tool_calls") or []
        results = {r.get("source_call_id") for r in ((step.get("observation") or {}).get("results") or [])}
        for call in calls:
            call_id = call.get("tool_call_id")
            if not call_id:
                report.error(f"step {step.get('step_id')}: a tool call has no tool_call_id")
            elif call_id not in results:
                report.warn(f"step {step.get('step_id')}: tool call {call.get('function_name')} ({call_id}) "
                            "has no result in the same step")

    metrics = doc.get("final_metrics") or {}
    real_model = (agent.get("model_name") or any(s.get("model_name") or s.get("metrics") for s in agent_steps)
                  or any((metrics.get(k) or 0) > 0 for k in ("total_prompt_tokens", "total_completion_tokens")))
    if not real_model:
        report.warn("nothing shows a real model call (no model_name, step metrics or token totals); "
                    "record what the model reports so a stubbed model would stand out")

    if previous is not None:
        earlier = previous.get("steps") or []
        if steps[:len(earlier)] != earlier:
            report.error("the earlier snapshot's steps are not an exact prefix; the recorder rewrote history")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("trajectory")
    parser.add_argument("--turns", type=int, help="expected number of user turns")
    parser.add_argument("--previous", help="an earlier snapshot of the same trajectory")
    parser.add_argument("--no-harbor", action="store_true", help="skip Harbor's schema validator")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    path = Path(args.trajectory)
    report = Report()
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        report.error(f"cannot read JSON: {exc}")
        return report.emit(f"trajectory {path}", args.json)
    previous = json.loads(Path(args.previous).read_text(encoding="utf-8")) if args.previous else None
    check(doc, report, args.turns, previous)
    if not args.no_harbor:
        harbor_validate(path, report)
    return report.emit(f"trajectory {path}", args.json)


if __name__ == "__main__":
    sys.exit(main())
