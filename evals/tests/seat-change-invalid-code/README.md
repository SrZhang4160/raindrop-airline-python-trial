# seat-change-invalid-code

**Behavior:** Given a confirmation code that matches no booking, the agent changes nothing before the customer gives a code, uses only that code, and no seat changes.

**Scenario:** 2 fixed turn(s) (`environment/scenario.json`): "I'd like to move to seat 7B, please." then "My confirmation code is ZZZ999.".

| id | type | checks |
| --- | --- | --- |
| `no-change-before-code` | code, exact | No change_seat call happens in the first turn, before the customer has given a confirmation code. |
| `checks-the-code` | code, exact | lookup_booking or change_seat is called with confirmation_code ZZZ999, the code the customer gave. |
| `uses-only-given-code` | code, exact | Every lookup_booking and change_seat call uses confirmation_code ZZZ999; the agent never tries a code the customer did not give. |
| `no-seat-change` | code, exact | No change_seat call updates a seat, because ZZZ999 matches no booking. |

**Not checked yet:** whether the reply tells the customer ZZZ999 matched no booking without claiming the seat moved, and whether the first reply asks for the code. That needs a judge, which waits until RELAI's hosted runner scores judges (`evals/decisions.md`).

`no-seat-change` also covers `change_seat`'s own guard in `src/airline_support/agent.py`, a live path: a pull request that lets an unknown code through can fail it.

Expected values live in `tests/params.json`; `tests/check.py` is shared code.
