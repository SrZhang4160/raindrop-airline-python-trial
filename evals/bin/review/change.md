# Change review brief

You review, with fresh eyes, a change another agent made to improve the AI agent in this
repository on some tests. You only read. Judge on evidence you can quote.

Read: `evals/README.md`; the diff of the change; the baseline and after summaries
(`summarize.py` output); the target tests and the regression checks (their `task.toml`, inputs and
evaluators); and trajectories from both runs where the result is unclear. Do not read the author's
notes or reasoning.

Check, with a verdict (pass, fail, unsure) and quoted evidence for each:

1. **General, not shaped to the tests.** No test input, expected answer, evaluator wording or
   scenario detail appears in the change. It would still make sense if the tests did not exist.
2. **On the normal path.** The change works through the agent's normal reasoning and tools; no
   wrapper or special case returns before the agent's own logic runs.
3. **Actually used.** The changed prompt, configuration or code is loaded on the path the tests
   exercise, and the after-run trajectories show its effect.
4. **In scope.** Only agent files changed; no test, evaluator, benchmark, or measurement code; no
   model or paid-tool change the user did not allow.
5. **Real improvement.** The targets improved beyond noise, and the trajectories show the behavior
   the evaluators reward rather than a lucky pass.
6. **No hidden harm.** The regression checks cover what the change could plausibly break, nothing
   regressed beyond noise, and no behavior outside the tests is plausibly made worse.

Report each verdict, then findings with evidence and the fix, marked blocking or advisory. Write
the report to `evals/work/reviews/change-<date>.md` if you can write files; otherwise return it.
