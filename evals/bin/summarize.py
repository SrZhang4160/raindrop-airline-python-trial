#!/usr/bin/env python3
"""Summarize Harbor runs per test and evaluator, or compare a run with a baseline.

Usage:
  python3 evals/bin/summarize.py <job or run dir> [...] [--json out.json]
  python3 evals/bin/summarize.py <candidate dirs> --baseline <baseline dir> [...]

Reads every Harbor trial under the given directories: result.json (exceptions, rewards,
task checksum, usage), verifier/feedback.json and verifier/errors.json (code evaluators),
and judges/<evaluator-id>.json (judges, written after the trial). Each trial is classified
as scored, evaluator error, judges pending, runtime failure, or unscored; these are never
merged, and Harbor's own exit status is not trusted (it exits 0 when trials fail).

With --baseline, both sides are summarized and compared per test. The comparison is refused
(exit 2) when a test's task checksum differs between the two runs, because the test changed
(Harbor's checksum covers the whole task directory, README included). Writes nothing except --json.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    declared_evaluators, dir_digest, evals_root, frozen_state, load_json, load_toml, normalized,
    resolve_task_dir, test_key,
)

STATUSES = ("scored", "evaluator error", "judges pending", "runtime failure", "unscored", "task not found")


def find_results(paths: list[Path]):
    trials, jobs = [], []
    for root in paths:
        candidates = [root / "result.json"] + sorted(root.glob("*/result.json")) + sorted(root.glob("*/*/result.json")) \
            + sorted(root.glob("*/*/*/result.json"))
        for path in candidates:
            if not path.is_file():
                continue
            try:
                data = load_json(path)
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict) and "trial_name" in data:
                trials.append((path.parent, data))
            elif isinstance(data, dict) and "n_total_trials" in data:
                jobs.append((path.parent, data))
    unique = {str(d.resolve()): (d, r) for d, r in trials}
    return list(unique.values()), jobs


def read_json_dict(path: Path) -> dict:
    try:
        data = load_json(path)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def valid_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def trial_record(trial_dir: Path, result: dict, evals_dir: Path) -> dict:
    raw_path = str(((result.get("task_id") or {}).get("path")) or "")
    task_path = resolve_task_dir(raw_path, evals_dir, trial_dir.parent)
    exception = (result.get("exception_info") or {}).get("exception_type")
    usage = {k: (result.get("agent_result") or {}).get(k)
             for k in ("n_input_tokens", "n_cache_tokens", "n_output_tokens", "cost_usd")}
    base = {"trial": trial_dir.name, "dir": str(trial_dir), "checksum": result.get("task_checksum"),
            "exception": exception, "score": None, "evaluators": {}, "usage": usage}
    if task_path is None:
        return {**base, "test": Path(raw_path).name or result.get("task_name", "?"), "task_path": raw_path,
                "status": "task not found",
                "missing": f"task {raw_path!r} not found; Harbor records -p as typed, so run Harbor with absolute "
                           "paths or summarize from the directory Harbor ran in"}
    try:
        evaluators = declared_evaluators(load_toml(task_path / "task.toml"))
    except Exception:
        evaluators = []
    rewards = ((result.get("verifier_result") or {}).get("rewards")) or {}
    feedback = read_json_dict(trial_dir / "verifier" / "feedback.json")
    errors = read_json_dict(trial_dir / "verifier" / "errors.json")
    crashed = bool(exception) and result.get("verifier_result") is None
    has_trajectory = (trial_dir / "agent" / "trajectory.json").is_file()
    outcomes = {}
    for ev in evaluators:
        ev_id, low, high = ev.get("id"), ev.get("min_score", 0.0), ev.get("max_score", 1.0)
        outcome = {"type": ev.get("type"), "min": low, "max": high}
        if ev.get("type") == "judge":
            path = trial_dir / "judges" / f"{ev_id}.json"
            data = read_json_dict(path) if path.is_file() else None
            if data is None:
                outcome["state"] = "pending"
            elif valid_number(data.get("score")) and low <= data["score"] <= high:
                outcome.update(state="scored", score=data["score"], feedback=data.get("feedback", ""))
            else:
                outcome.update(state="error", message=data.get("error") or f"invalid judge score {data.get('score')!r}")
        else:
            value = rewards.get(ev_id)
            if valid_number(value) and low <= value <= high:
                outcome.update(state="scored", score=value, feedback=feedback.get(ev_id, ""))
                if not str(feedback.get(ev_id, "")).strip():
                    outcome["warning"] = "no feedback"
            elif ev_id in errors:
                outcome.update(state="error", message=str(errors[ev_id]))
            elif value is not None:
                outcome.update(state="error", message=f"score {value!r} outside [{low}, {high}]")
            else:
                outcome.update(state="error", message="no score in reward.json")
        outcomes[ev_id] = outcome

    states = {o["state"] for o in outcomes.values()}
    if crashed or not has_trajectory:
        status = "runtime failure"  # judge files written for a crashed run never make it scored
    elif not outcomes:
        status = "unscored"
    elif "error" in states:
        status = "evaluator error"
    elif "pending" in states:
        status = "judges pending"
    else:
        status = "scored"
    score = None
    if status == "scored":
        score = statistics.fmean(normalized(o["score"], o["min"], o["max"]) for o in outcomes.values())
    return {**base, "test": test_key(task_path, evals_dir), "task_path": str(task_path), "status": status,
            "score": score, "evaluators": outcomes}


def summarize(paths: list[Path], evals_dir: Path) -> dict:
    trials, jobs = find_results(paths)
    records = [trial_record(d, r, evals_dir) for d, r in trials]
    tests: dict[str, dict] = {}
    for rec in records:
        entry = tests.setdefault(rec["test"], {"trials": [], "checksums": set(), "task_path": rec["task_path"]})
        entry["trials"].append(rec)
        if rec["checksum"]:
            entry["checksums"].add(rec["checksum"])
    summary = {"tests": {}, "trials": len(records), "status_counts": {s: 0 for s in STATUSES}, "jobs": []}
    for rec in records:
        summary["status_counts"][rec["status"]] += 1
    for job_dir, job in jobs:
        stats = job.get("stats") or {}
        summary["jobs"].append({"dir": str(job_dir), "n_total_trials": job.get("n_total_trials"),
                                "n_errored_trials": stats.get("n_errored_trials"),
                                "n_completed_trials": stats.get("n_completed_trials"),
                                "cost_usd": stats.get("cost_usd")})
    usage = {"n_input_tokens": 0, "n_cache_tokens": 0, "n_output_tokens": 0, "cost_usd": 0.0}
    usage_seen = False
    for rec in records:
        for key, value in rec["usage"].items():
            if valid_number(value):
                usage[key] += value
                usage_seen = True
    summary["usage"] = usage if usage_seen else None

    for name, entry in sorted(tests.items()):
        recs = entry["trials"]
        scored = [r["score"] for r in recs if r["score"] is not None]
        per_eval: dict[str, dict] = {}
        for rec in recs:
            for ev_id, outcome in rec["evaluators"].items():
                agg = per_eval.setdefault(ev_id, {"type": outcome["type"], "scores": [], "errors": 0, "pending": 0})
                if outcome["state"] == "scored":
                    agg["scores"].append(normalized(outcome["score"], outcome["min"], outcome["max"]))
                elif outcome["state"] == "error":
                    agg["errors"] += 1
                else:
                    agg["pending"] += 1
        summary["tests"][name] = {
            "attempts": len(recs),
            "status_counts": {s: sum(r["status"] == s for r in recs) for s in STATUSES},
            "mean": statistics.fmean(scored) if scored else None,
            "stdev": statistics.stdev(scored) if len(scored) > 1 else 0.0,
            "scored": len(scored),
            "full_marks": sum(1 for s in scored if s >= 1.0),
            "checksums": sorted(entry["checksums"]),
            "task_path": entry["task_path"],
            "evaluators": {
                ev_id: {"type": agg["type"], "n": len(agg["scores"]), "errors": agg["errors"], "pending": agg["pending"],
                        "mean": statistics.fmean(agg["scores"]) if agg["scores"] else None,
                        "min": min(agg["scores"]) if agg["scores"] else None,
                        "max": max(agg["scores"]) if agg["scores"] else None}
                for ev_id, agg in sorted(per_eval.items())},
            "problems": [problem(r) for r in recs if r["status"] != "scored"],
            "notes": [f"{r['trial']}: scored, but Harbor recorded {r['exception']} (for example the agent timed out "
                      "and the verifier scored what it had done)" for r in recs if r["status"] == "scored" and r["exception"]],
        }
    return summary


def problem(rec: dict) -> str:
    details = [f"{k}: {o.get('message', o['state'])}" for k, o in rec["evaluators"].items() if o["state"] != "scored"]
    parts = [rec["status"]]
    if rec.get("missing"):
        parts.append(rec["missing"])
    if rec["exception"]:
        parts.append(f"exception {rec['exception']}")
    parts += details
    return f"{rec['trial']}: " + "; ".join(parts)


def fmt(value) -> str:
    return "-" if value is None else f"{value:.2f}"


def print_summary(summary: dict, label: str) -> None:
    counts = summary["status_counts"]
    print(f"## {label}")
    print(f"Trials: {summary['trials']} (" + ", ".join(f"{k} {v}" for k, v in counts.items() if v) + ")")
    for job in summary["jobs"]:
        if job.get("n_errored_trials"):
            print(f"Harbor job {Path(job['dir']).name}: {job['n_errored_trials']} errored trial(s)")
    if summary["usage"]:
        u = summary["usage"]
        print(f"Agent usage: {u['n_input_tokens']} input tokens ({u['n_cache_tokens']} cached), "
              f"{u['n_output_tokens']} output, ${u['cost_usd']:.4f} (as the agent reported it)")
    print()
    print("| Test | Attempts | Scored | Mean | Stdev | Full marks |")
    print("| --- | --- | --- | --- | --- | --- |")
    for name, t in summary["tests"].items():
        print(f"| {name} | {t['attempts']} | {t['scored']} | {fmt(t['mean'])} | {fmt(t['stdev'])} | {t['full_marks']} |")
    print()
    print("| Test | Evaluator | Type | Scored | Mean | Min | Max | Errors | Pending |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for name, t in summary["tests"].items():
        for ev_id, e in t["evaluators"].items():
            print(f"| {name} | {ev_id} | {e['type']} | {e['n']} | {fmt(e['mean'])} | {fmt(e['min'])} | {fmt(e['max'])} "
                  f"| {e['errors']} | {e['pending']} |")
    problems = [(name, p) for name, t in summary["tests"].items() for p in t["problems"]]
    if problems:
        print("\nNot scored cleanly (never count these as agent failures or passes):")
        for name, p in problems:
            print(f"- {name} / {p}")
    notes = [(name, n) for name, t in summary["tests"].items() for n in t["notes"]]
    for name, n in notes:
        print(f"note: {name} / {n}")


def frozen_warnings(evals_dir: Path, summary: dict) -> list[str]:
    warnings = []
    frozen = frozen_state(evals_dir).get("tests", {})
    for name, test in summary["tests"].items():
        if test["status_counts"]["task not found"] == test["attempts"]:
            continue
        if name.startswith("harness/"):  # the smoke test checks the harness and is never frozen
            continue
        entry = frozen.get(name)
        if not entry:
            warnings.append(f"{name} is not frozen; freeze tests before their first real run")
            continue
        path = evals_dir / name
        if path.is_dir() and dir_digest(path) != entry.get("digest"):
            warnings.append(f"{name} changed since it was frozen")
    return warnings


def compare(base: dict, cand: dict) -> tuple[int, dict]:
    rows, refused = [], []
    for name in sorted(set(base["tests"]) & set(cand["tests"])):
        b, c = base["tests"][name], cand["tests"][name]
        if b["checksums"] and c["checksums"] and set(b["checksums"]) != set(c["checksums"]):
            refused.append(name)
            continue
        if b["mean"] is None or c["mean"] is None:
            rows.append({"test": name, "baseline": b["mean"], "candidate": c["mean"], "delta": None, "verdict": "not comparable"})
            continue
        delta = c["mean"] - b["mean"]
        noise = math.sqrt((b["stdev"] ** 2) / max(b["scored"], 1) + (c["stdev"] ** 2) / max(c["scored"], 1))
        if min(b["scored"], c["scored"]) < 2:
            verdict = "too few attempts to tell" if delta else "unchanged"
        elif abs(delta) <= 2 * noise or delta == 0:
            verdict = "within noise" if delta else "unchanged"
        else:
            verdict = "improved" if delta > 0 else "regressed"
        rows.append({"test": name, "baseline": b["mean"], "candidate": c["mean"], "delta": delta,
                     "noise": noise, "verdict": verdict,
                     "attempts": f"{b['scored']}/{c['scored']}"})
    only = {"baseline only": sorted(set(base["tests"]) - set(cand["tests"])),
            "candidate only": sorted(set(cand["tests"]) - set(base["tests"]))}
    return (2 if refused else 0), {"rows": rows, "refused": refused, **only}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("runs", nargs="+", help="Harbor job directories or folders containing them")
    parser.add_argument("--baseline", nargs="+", help="baseline job directories to compare against")
    parser.add_argument("--evals-dir")
    parser.add_argument("--json", help="also write the summary as JSON to this path")
    args = parser.parse_args()
    evals_dir = evals_root(args.evals_dir)
    cand = summarize([Path(p) for p in args.runs], evals_dir)
    if not cand["trials"]:
        print("error: no Harbor trials found under the given directories")
        return 2
    print_summary(cand, "Run" if not args.baseline else "Candidate")
    output = {"run": cand}
    status = 1 if cand["status_counts"]["task not found"] else 0
    for warning in frozen_warnings(evals_dir, cand):
        print(f"warning: {warning}")
    if args.baseline:
        base = summarize([Path(p) for p in args.baseline], evals_dir)
        print()
        print_summary(base, "Baseline")
        compare_status, comparison = compare(base, cand)
        status = max(status, compare_status)
        output["baseline"], output["comparison"] = base, comparison
        print("\n## Comparison (mean normalized score per test)")
        if comparison["refused"]:
            print("Refused for tests that changed between the runs: " + ", ".join(comparison["refused"]))
        print("| Test | Baseline | Candidate | Delta | Noise (1 s.e.) | Attempts | Verdict |")
        print("| --- | --- | --- | --- | --- | --- | --- |")
        for row in comparison["rows"]:
            print(f"| {row['test']} | {fmt(row['baseline'])} | {fmt(row['candidate'])} | {fmt(row['delta'])} | "
                  f"{fmt(row.get('noise'))} | {row.get('attempts', '-')} | {row['verdict']} |")
        for key in ("baseline only", "candidate only"):
            if comparison[key]:
                print(f"{key}: {', '.join(comparison[key])}")
    if args.json:
        def default(value):
            return sorted(value) if isinstance(value, set) else str(value)
        Path(args.json).write_text(json.dumps(output, indent=2, default=default) + "\n", encoding="utf-8")
    return status


if __name__ == "__main__":
    sys.exit(main())
