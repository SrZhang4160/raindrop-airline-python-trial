"""Code evaluators driven by tests/params.json. Reads only /logs/agent/trajectory.json.

Each entry in params.json is one evaluator, by "kind":
  requires        some call to one of "tools", with each named argument equal (case-insensitive,
                  trimmed) to one of its accepted values, whose result contains every string in
                  "result_contains"
  forbids_result  no call to one of "tools" whose result contains "result_contains"
  only_args       every call to one of "tools" has each named argument among its accepted values
  none_in_turns   no call to one of "tools" in the listed user turns (1 is the first)
"""

import json
from pathlib import Path

from evaluate import Results, load_trajectory, turns

params = json.loads((Path(__file__).parent / "params.json").read_text())
results = Results()
trajectory = load_trajectory()
conversation = [t for t in turns(trajectory) if t["user"] is not None]


def norm(value):
    return str(value).strip().upper()


def calls():
    for number, turn in enumerate(conversation, 1):
        outputs = {r.get("source_call_id"): str(r.get("content", ""))
                   for s in turn["steps"] for r in ((s.get("observation") or {}).get("results") or [])}
        for step in turn["steps"]:
            if step.get("source") != "agent":
                continue
            for call in step.get("tool_calls") or []:
                yield number, call, outputs.get(call.get("tool_call_id"), "")


def args_match(call, rule):
    args = call.get("arguments") or {}
    return all(norm(args.get(name, "")) in {norm(v) for v in accepted}
               for name, accepted in rule.get("args", {}).items())


def describe(number, call, output):
    return f"turn {number}: {call.get('function_name')}({call.get('arguments')}) -> {output[:120]!r}"


made = list(calls())
seen = "; ".join(describe(*entry) for entry in made) or "none"

for rule in params["evaluators"]:
    with results.check(rule["id"]):
        if not conversation:
            raise ValueError("the trajectory has no user turn")
        mine = [(n, c, out) for n, c, out in made if c.get("function_name") in rule["tools"]]
        kind = rule["kind"]
        if kind == "requires":
            wanted = rule.get("result_contains", [])
            if not isinstance(wanted, list):
                raise ValueError(f"{rule['id']}: result_contains must be a list of strings for requires")
            good = [c for _, c, out in mine if args_match(c, rule) and all(text in out for text in wanted)]
            bad = [] if good else [None]
        elif kind == "forbids_result":
            if not isinstance(rule["result_contains"], str):
                raise ValueError(f"{rule['id']}: result_contains must be a string for forbids_result")
            bad = [entry for entry in mine if rule["result_contains"] in entry[2]]
        elif kind == "only_args":
            bad = [entry for entry in mine if not args_match(entry[1], rule)]
        elif kind == "none_in_turns":
            bad = [entry for entry in mine if entry[0] in rule["turns"]]
        else:
            raise ValueError(f"unknown evaluator kind {kind!r} in params.json")
        if not bad:
            results.score(rule["id"], 1.0, rule["pass"])
        elif bad == [None]:
            results.score(rule["id"], 0.0, f"{rule['fail']} Calls made: {seen}.")
        else:
            results.score(rule["id"], 0.0, f"{rule['fail']} Seen: {'; '.join(describe(*entry) for entry in bad)}.")
results.write()
