#!/usr/bin/env python3
"""Reference LLM judge: scores each trial's judge evaluators on the host. Standard library only.

  python3 evals/bin/reference/judge.py <job or run dir> [...] [--force]
  python3 evals/bin/reference/judge.py --self-test evals/tests/<id>

For every Harbor trial under the given directories, it reads the task's
[[metadata.evals.evaluators]] with type = "judge", sends the rubric and the redacted
trajectory to the model in evals.toml [judge], and writes
<trial>/judges/<evaluator-id>.json = {"score", "feedback", "model", "judge": "api"}
or {"error": "..."}. summarize.py reads those files. Existing scores are kept unless --force;
earlier errors are retried. Trials that crashed before verification are skipped.
Optional evals.toml keys: [judge].effort (Anthropic effort, default "medium").

--self-test runs each judge of a task on a synthetic empty-reply trajectory and fails when a
judge gives it full marks (a judge that cannot fail is not measuring anything), unless the
task sets [metadata.evals].prohibition = true.

Keys: ANTHROPIC_API_KEY or OPENAI_API_KEY, from the environment or evals/.env. The script
reads the value to call the API and never prints it. The judge runs on the host, so the key
never enters a container.

evals.toml:
  [judge]
  mode = "api"                # or "subagent": a fresh coding-agent subagent writes the same files
  provider = "anthropic"      # or "openai"
  model = "claude-opus-5-5"   # required for openai
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _common import (  # noqa: E402
    declared_evaluators, evals_config, evals_root, load_json, load_toml, redact, resolve_task_dir, secret_values,
)

SYSTEM = """You grade one recorded run of an AI agent against a rubric. The run is an ATIF trajectory:
"user" steps are what the agent received, "agent" steps are what it said and the tools it called,
with tool results in "observation". Judge only what the trajectory shows; do not assume actions
that were not recorded.

- Score the whole relevant turn: all agent steps after a user step and before the next one.
- Accept any wording that satisfies the rubric; do not reward or punish style the rubric does not mention.
- If the rubric grades a deliverable the user asked for (an answer, a change, an action) and the
  agent did not produce it, give the minimum score and say the deliverable is missing. If declining
  or asking was the right response, nothing is missing.
- Give a score within the stated bounds and feedback that names every unmet criterion, where it
  occurred, and what would earn full marks.
Return JSON: {"score": <number>, "feedback": "<text>"}."""

SCHEMA = {"type": "object", "properties": {"score": {"type": "number"}, "feedback": {"type": "string"}},
          "required": ["score", "feedback"], "additionalProperties": False}


def api_key(name: str, evals_dir: Path) -> str:
    value = os.environ.get(name) or secret_values(evals_dir).get(name)
    if not value:
        raise SystemExit(f"error: {name} is not set; add it to evals/.env (the script never prints it)")
    return value


def post(url: str, headers: dict, body: dict, attempts: int = 4) -> dict:
    """POST JSON; retries rate limits, server errors and dropped connections with backoff."""
    request = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                     headers={"content-type": "application/json", **headers})
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=600) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            retryable = exc.code in (408, 409, 429) or exc.code >= 500
            if not retryable or attempt == attempts - 1:
                raise RuntimeError(f"HTTP {exc.code}: {exc.read().decode(errors='replace')[:500]}") from None
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt == attempts - 1:
                raise RuntimeError(f"connection failed: {exc}") from None
        time.sleep(2 ** attempt * 5)
    raise RuntimeError("unreachable")


def call_model(config: dict, evals_dir: Path, prompt: str) -> tuple[dict, str]:
    provider = config.get("provider", "anthropic")
    if provider == "openai":
        model = config.get("model") or sys.exit("error: set [judge].model in evals.toml for OpenAI")
        data = post("https://api.openai.com/v1/chat/completions",
                    {"authorization": f"Bearer {api_key('OPENAI_API_KEY', evals_dir)}"},
                    {"model": model, "messages": [{"role": "system", "content": SYSTEM},
                                                  {"role": "user", "content": prompt}],
                     "response_format": {"type": "json_schema",
                                         "json_schema": {"name": "evaluation", "strict": True, "schema": SCHEMA}}})
        message = data["choices"][0]["message"]
        if message.get("refusal"):
            raise RuntimeError(f"the judge model refused: {message['refusal']}")
        return json.loads(message.get("content") or "{}"), model
    model = config.get("model", "claude-opus-5-5")
    # Claude Opus 5.5 rejects forced tool use, so the JSON shape comes from structured output.
    # fallbacks="default" re-runs a policy-declined request on a suitable model in the same call.
    data = post("https://api.anthropic.com/v1/messages",
                {"x-api-key": api_key("ANTHROPIC_API_KEY", evals_dir), "anthropic-version": "2026-10-21",
                 "anthropic-beta": "server-side-fallback-2026-07-01"},
                {"model": model, "max_tokens": 16000, "system": SYSTEM, "fallbacks": "default",
                 "messages": [{"role": "user", "content": prompt}],
                 "output_config": {"effort": config.get("effort", "medium"),
                                   "format": {"type": "json_schema", "schema": SCHEMA}}})
    if data.get("stop_reason") == "refusal":
        raise RuntimeError("the judge model declined to grade this trajectory")
    text = "".join(block.get("text", "") for block in data.get("content", []) if block.get("type") == "text")
    return json.loads(text or "{}"), data.get("model", model)


def judge_one(config: dict, evals_dir: Path, task: Path, ev: dict, trajectory: dict, secrets: dict) -> dict:
    low, high = ev.get("min_score", 0.0), ev.get("max_score", 1.0)
    rubric = (task / ev["rubric"]).read_text(encoding="utf-8")
    prompt = (f"Criterion: {ev.get('criterion', '')}\nScore bounds: {low} (worst) to {high} (best).\n\n"
              f"Rubric:\n{rubric}\n\nTrajectory:\n{redact(json.dumps(trajectory, indent=1), secrets)}")
    try:
        verdict, model = call_model(config, evals_dir, prompt)
        score, feedback = verdict.get("score"), str(verdict.get("feedback", "")).strip()
        if not isinstance(score, (int, float)) or not math.isfinite(score) or not low <= score <= high:
            return {"error": f"judge returned score {score!r}, outside [{low}, {high}]", "model": model}
        if not feedback:
            return {"error": "judge returned no feedback", "model": model}
        return {"score": score, "feedback": feedback, "model": model, "judge": "api",
                "judged_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    except Exception as exc:  # recorded as an evaluator error, never as a score
        return {"error": f"{type(exc).__name__}: {exc}"}


def trials(paths: list[Path]):
    """Yield (trial dir, result) for every Harbor trial under the given directories."""
    for root in paths:
        for path in [root / "result.json", *root.glob("*/result.json"), *root.glob("*/*/result.json"),
                     *root.glob("*/*/*/result.json")]:
            try:
                data = load_json(path) if path.is_file() else None
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict) and "trial_name" in data:
                yield path.parent, data


def self_test(task: Path, config: dict, evals_dir: Path) -> int:
    toml = load_toml(task / "task.toml")
    judges = [ev for ev in declared_evaluators(toml) if ev.get("type") == "judge"]
    if not judges:
        print(f"{task.name}: no judge evaluators")
        return 0
    empty = {"schema_version": "ATIF-v1.7", "agent": {"name": "agent-evals-synthetic", "version": "0"},
             "steps": [{"step_id": 1, "source": "user", "message": "Hello, I need help with my request."},
                       {"step_id": 2, "source": "agent", "message": ""}]}
    prohibition = toml.get("metadata", {}).get("evals", {}).get("prohibition", False)
    status = 0
    for ev in judges:
        result = judge_one(config, evals_dir, task, ev, empty, {})
        if "error" in result:
            print(f"FAIL {ev['id']}: {result['error']}")
            status = 1
        elif result["score"] >= ev.get("max_score", 1.0) and not prohibition:
            print(f"FAIL {ev['id']}: full marks for an empty reply, so this judge cannot fail. Feedback: {result['feedback']}")
            status = 1
        else:
            print(f"PASS {ev['id']}: empty reply scored {result['score']}")
    return status


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("runs", nargs="*", help="Harbor job directories or folders containing them")
    parser.add_argument("--self-test", metavar="TASK_DIR", help="check that a task's judges can fail")
    parser.add_argument("--force", action="store_true", help="re-judge trials that already have results")
    parser.add_argument("--evals-dir")
    args = parser.parse_args()
    evals_dir = evals_root(args.evals_dir)
    config = evals_config(evals_dir).get("judge", {}) or {}
    if config.get("mode") == "subagent":
        print("evals.toml [judge].mode is 'subagent': a fresh subagent writes judges/<id>.json instead of this script")
        return 2
    if args.self_test:
        return self_test(Path(args.self_test).resolve(), config, evals_dir)
    if not args.runs:
        parser.error("name run directories, or use --self-test")
    secrets = secret_values(evals_dir)
    written = errors = unresolved = 0
    for trial_dir, result in trials([Path(p) for p in args.runs]):
        raw = str(((result.get("task_id") or {}).get("path")) or "")
        task = resolve_task_dir(raw, evals_dir, trial_dir.parent)
        if task is None:
            print(f"{trial_dir.name}: task {raw!r} not found; run Harbor with absolute task paths or judge from "
                  "the directory Harbor ran in")
            unresolved += 1
            continue
        trajectory_path = trial_dir / "agent" / "trajectory.json"
        crashed = result.get("exception_info") and result.get("verifier_result") is None
        if crashed or not trajectory_path.is_file():
            continue  # a run that never finished is a runtime failure, not something to grade
        trajectory = load_json(trajectory_path)
        for ev in declared_evaluators(load_toml(task / "task.toml")):
            if ev.get("type") != "judge":
                continue
            out = trial_dir / "judges" / f"{ev['id']}.json"
            if out.exists() and not args.force:
                try:
                    if "error" not in load_json(out):
                        continue  # keep earlier scores; earlier errors are retried
                except (OSError, json.JSONDecodeError):
                    pass
            result = judge_one(config, evals_dir, task, ev, trajectory, secrets)
            out.parent.mkdir(exist_ok=True)
            out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
            written += 1
            errors += "error" in result
            print(f"{trial_dir.name} {ev['id']}: " + (f"error: {result['error']}" if "error" in result
                                                       else f"{result['score']}"))
    print(f"judged {written} evaluator result(s), {errors} error(s), {unresolved} trial(s) whose task was not found")
    return 1 if errors or unresolved else 0


if __name__ == "__main__":
    sys.exit(main())
