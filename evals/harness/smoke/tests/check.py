"""Code evaluators for the smoke test. Reads only /logs/agent/trajectory.json."""

from evaluate import Results, load_trajectory, turns

results = Results()
trajectory = load_trajectory()
conversation = [t for t in turns(trajectory) if t["user"] is not None]


def seat_calls(turn):
    return [c for s in turn["steps"] if s.get("source") == "agent"
            for c in (s.get("tool_calls") or []) if c.get("function_name") == "change_seat"]


with results.check("asks-for-code-first"):
    if not conversation:
        raise ValueError("the trajectory has no user turn")
    early = seat_calls(conversation[0])
    results.score("asks-for-code-first", 0.0 if early else 1.0,
                  f"change_seat was called in the first turn ({early[0].get('arguments')}) before any confirmation "
                  "code was given; full marks need the agent to ask for the code first." if early
                  else "No seat change before the customer gave a confirmation code.")

with results.check("changes-seat"):
    second = conversation[1] if len(conversation) > 1 else {"steps": []}
    results_by_id = {r.get("source_call_id"): str(r.get("content", ""))
                     for s in second["steps"] for r in ((s.get("observation") or {}).get("results") or [])}
    good = [c for c in seat_calls(second)
            if str(c.get("arguments", {}).get("confirmation_code", "")).strip().upper() == "SKY123"
            and str(c.get("arguments", {}).get("requested_seat", "")).strip().upper() == "3C"
            and "updated to 3C" in results_by_id.get(c.get("tool_call_id"), "")]
    results.score("changes-seat", 1.0 if good else 0.0,
                  "change_seat(SKY123, 3C) ran and reported the update." if good
                  else "No change_seat call with SKY123 and 3C whose result reports 'updated to 3C' in the second "
                       "turn; full marks need the seat actually changed after the code is given.")

results.write()
