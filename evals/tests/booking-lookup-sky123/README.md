# booking-lookup-sky123

**Behavior:** Asked to look up booking SKY123, the agent looks it up with that code and changes nothing.

**Scenario:** 1 fixed turn(s) (`environment/scenario.json`): "Can you look up booking SKY123?".

| id | type | checks |
| --- | --- | --- |
| `looks-up-booking` | code, exact | lookup_booking is called with confirmation_code SKY123 and its result returns Maya Chen's booking. |
| `changes-nothing` | code, exact | No change_seat call updates a seat, since the customer only asked for a lookup. |

**Not checked yet:** whether the reply gives the customer the booking's details. That needs a judge, which waits until RELAI's hosted runner scores judges (`evals/decisions.md`).

Expected values live in `tests/params.json`; `tests/check.py` is shared code.
