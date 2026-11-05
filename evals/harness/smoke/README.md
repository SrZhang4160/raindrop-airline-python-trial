# smoke

Proves the harness works; it is not an eval and is never used to measure the agent.

**Scenario:** two fixed turns (`environment/scenario.json`), driven through the real terminal
chat (`airline_support.main.chat`) by `evals/harness/adapter.py`.

| id | type | checks |
| --- | --- | --- |
| `asks-for-code-first` | code, exact | no `change_seat` call in turn 1 |
| `changes-seat` | code, exact | turn 2 calls `change_seat(SKY123, 3C)` and the tool result says the seat was updated |
