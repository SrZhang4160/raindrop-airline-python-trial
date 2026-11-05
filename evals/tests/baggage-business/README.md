# baggage-business

**Behavior:** A business-class traveller asks about checked bags, and the agent calls baggage_policy with a ticket type that returns the premium-cabin policy.

**Scenario:** 1 fixed turn(s) (`environment/scenario.json`): "I'm flying business class. How many checked bags can I bring?".

| id | type | checks |
| --- | --- | --- |
| `gets-premium-policy` | code, exact | baggage_policy is called with a ticket type that returns the premium-cabin policy (two checked bags, 70 pounds each). |

**Not checked yet:** whether the reply tells the customer two checked bags up to 70 pounds each. That needs a judge, which waits until RELAI's hosted runner scores judges (`evals/decisions.md`).

Expected values live in `tests/params.json`; `tests/check.py` is shared code.
