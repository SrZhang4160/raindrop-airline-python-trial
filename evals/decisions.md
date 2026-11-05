# Decisions

Newest last. Answers are the user's; assumptions are the coding agent's own choices.

- 2026-11-05 answer: judges run through the OpenAI API (`[judge] mode = "api"`, provider `openai`, model `gpt-5.2`).
- 2026-11-05 answer: `[harbor].pass_mark = 0.8`.
- 2026-11-05 answer: for the RELAI pull-request demo, only tests checked by code evaluators go on `main`; the judged CSV benchmark waits until RELAI's hosted runner scores judges.
- 2026-11-05 assumption: the harness reuses the adapter and Harbor agent class from the earlier, never-run `evals-demo` setup (agent-evals PR #1), rebuilt to the current `[harbor.image]` rules: the image builds from the repository root with only `pyproject.toml` and `uv.lock` in its context.
- 2026-11-05 assumption: `[harbor].secrets` lists `OPENAI_API_KEY` so RELAI's hosted runner passes the organization's saved key to each test.
- 2026-11-05 assumption: in `seat-change-invalid-code`, checking the code with either `lookup_booking` or `change_seat` counts as verifying it; `change_seat` itself refuses an unknown code, and the instructions do not require a lookup first.
- 2026-11-05 assumption: what each test's reply says is not checked until judges can run on RELAI; each test's README lists what is deferred.
- 2026-11-05 answer: integrate OpenAI, with `OPENAI_API_KEY` and the Agents SDK's default model (gpt-5.4-mini in openai-agents 0.17.7), not the Anthropic path `start.sh` also offers.
- 2026-11-05 assumption: the harness records with the project's own ATIF recorder (`src/airline_support/trajectory.py`, through its `ATIF_TRAJECTORY_PATH` option) instead of one under `evals/harness/`; it is an existing extension point, so no project file changes.
- 2026-11-05 assumption: the project's recorder writes a tool-only model response's raw output JSON as that step's message. Evaluators here read tool calls and their results, never a tool-only step's message, so this is left unchanged rather than editing the project.
- 2026-11-05 assumption: the smoke test's `changes-seat` scores 0, not an error, when the second user turn is missing, and `asks-for-code-first` scores an empty first reply as 1; `check_task.py --verifier` requires a score on its one-turn and empty-reply trajectories, and `changes-seat` still fails both.
- 2026-11-05 assumption: because tool-only steps carry raw JSON as their message, a test that reads the agent's reply must read the last agent step's message or skip agent steps that have tool calls; `evaluate.replies()` joins every agent step's message and would include that JSON.
- 2026-11-05 evidence: live files are live. In a scratch copy, `change_seat` in `src/airline_support/agent.py` was edited to return "Seat moved to …" instead of "Seat updated to …"; the next smoke trial from that copy returned "Seat moved to 3C for booking SKY123." and scored `changes-seat` 0.00 (1.00 unedited), so each trial runs the current source.
