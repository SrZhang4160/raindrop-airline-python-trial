# Test review brief

You review, with fresh eyes, an agent test that another agent wrote, before it is frozen. You
only read. A test is good when it fails if the agent lacks the behavior and passes when it has it.
Judge on evidence you can quote.

Read: `evals/README.md`; the test directory (`task.toml`, `README.md`, scenario and input files,
everything under `tests/`); any wiki pages it covers; and the request, run log or feedback it was
built from, if given. Do not read the author's notes or reasoning.

Check, with a verdict (pass, fail, unsure) and quoted evidence for each:

1. **Measures the stated behavior.** The scenario actually exercises it, and the evaluators reward
   exactly it, not something adjacent or easier.
2. **No answer in the input.** The agent-facing input holds only the original request and its
   context: no expected answer, diagnosis, root cause, or suggested fix. A simulated user's
   instructions hold the user's goal and knowledge, never the expected agent behavior.
3. **Not made easy.** Ambiguity, competing cues and constraints that make the behavior hard are
   kept; the input was not simplified so the right answer is obvious.
4. **Right method.** Exact criteria use code; anything with many valid phrasings uses a judge.
   No keyword or regex check stands in for meaning, no judge decides a fact code could check, and
   independent criteria are not merged into one score where one failure could hide another. A
   benchmark scorer that its `README.md` and `decisions.md` record as user-supplied or approved
   passes this check as long as it is applied exactly.
5. **Can fail, fairly.** An agent without the behavior would score low; a missing required
   deliverable scores the minimum; a structurally broken recording errors instead of scoring
   zero; nothing rewards activity in place of outcome.
6. **Useful feedback.** Every score below full marks says what was missing and what full marks need.
7. **Faithful to its source.** A test from a run keeps the original input; one from a requirement
   matches the requirement's text.
8. **Detects the risk.** For a hard case, the evaluators named for the risk would fail on its
   observable failure, and nothing in the scenario or criteria rules out the risk's mechanism.

Report each verdict, then findings: what is wrong, the evidence, and the change that would fix it,
marked blocking or advisory. Write the report to `evals/work/reviews/test-<id>-<date>.md` if you
can write files; otherwise return it.
