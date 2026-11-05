# Harness review brief

You review, with fresh eyes, a harness another agent built so the agent in this repository can
be evaluated. You only read; you change nothing. The harness is correct only when a trial is the
real agent doing its real work and the evaluators see what it actually did. Fail a property only
on evidence you can quote, never on speculation.

Read: `evals/README.md`, `evals/evals.toml`, `evals/decisions.md`, everything under
`evals/harness/` (including the smoke test in `evals/harness/smoke/`), and the project files the
harness changed (`git diff`). Establish from the repository yourself how the real application
starts and uses the agent before reading the harness's claims.

## Passes

The review can run in up to three passes; you are told which one this is.

- **Code pass.** No trajectory yet (the smoke trial may still be running). Judge every property
  from the code; mark one unsure only where its answer depends on what a run records.
- **Trajectory pass.** You are given the smoke run's trajectory. Check that it shows the run the
  code implies: the real agent's model and tool events, the user input on every turn, and the
  deliverable where the evaluators read it. Settle every property left unsure, and change a
  verdict when the trajectory contradicts the code.
- **Recheck pass.** After fixes, you are given your earlier report and the diff since it. Check
  that each finding is fixed at its evidence and that the diff breaks no property that passed.
  Re-read beyond the diff only where it touches a property's evidence.

Judge each property, with a verdict of pass, fail or unsure, and quoted evidence (file:line,
or a trajectory step):

1. **Real agent.** The trial runs the same entrypoint and production path the application uses,
   from current source, with real model calls, prompts, tool selection and tool code. Files whose
   edits change behavior are read live, not baked into an image.
2. **Real configuration.** The integrated configuration is the one the repository presents as
   real use (README, defaults), not a test fixture; `evals.toml` names it and the evidence.
3. **Real input.** Input reaches the agent the way a user's does, including any preprocessing the
   application applies, and multi-turn state is kept the way the application keeps it.
4. **Unchanged behavior.** The harness and any project edits change nothing the agent does:
   prompts, tool policies, model choice, data and control flow are as in production. Recording
   only observes. Permissions come from the settings the agent's real users run it with, or, for
   interactive prompts, from the user's answer recorded in `decisions.md`.
5. **Declared stand-ins.** Anything not run for real is replaced only at a dependency's client
   boundary, is listed in `evals/harness/mocks/MOCKS.md`, and the user approved it.
6. **Truthful trajectory.** Every recorded step happened, in order, and nothing that matters is
   missing: user input, model messages, tool calls with arguments, results tied to their calls.
7. **Output reaches evaluators.** What the agent produces (a reply, a file, a record) is visible
   to the evaluators, and the smoke test's evaluators check a real outcome rather than activity
   and could fail. The smoke test is not frozen, so a check loosened until it passes fails this.
8. **Containment.** The agent runs only in containers; nothing in the harness or its instructions
   runs it on the host; no secret value appears in any committed file.

Report each property's verdict, then findings: for each failed or unsure property, what is wrong,
the quoted evidence, and the change that would fix it, marked blocking or advisory. Write the
report to `evals/work/reviews/harness-<date>-<pass>.md` if you can write files; otherwise return
it.
