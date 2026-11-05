<!-- agent-evals 0.1.0 -->
# evals/

This directory holds the evals for the agent in this repository: tests that run the real agent
in containers, score what it did, and track whether changes help or hurt. It was set up by the
agent-evals plugin, but nothing here needs the plugin: any coding agent or person can read,
run and extend it with Harbor, Docker and Python.

## Fixed rules

Everything else (how the agent starts, adapters, Dockerfiles, test design, how Harbor is run)
is the choice of whoever builds the evals.

1. Every test is a Harbor task, run with Harbor 0.23.0.
2. Each trial yields an ATIF trajectory at `/logs/agent/trajectory.json`, and each declared
   evaluator a score within its bounds plus feedback, or an explicit error.
3. The agent under test runs only inside a container, never on the host.
4. Files whose edits change the agent's behavior (`[target].live_paths` in `evals.toml`) reach
   each trial from the current source, never baked into an image.
5. A frozen test does not change silently: a change needs a reason (`freeze.py --reason`), a
   line in `decisions.md`, and a fresh review. Work is reviewed by a reviewer with fresh context.
6. Secret values live only in `evals/.env`, written by the user. Coding agents never read or
   print them; files refer to them by name.
7. A mock replaces only a dependency's client boundary, never the agent, its model, or a
   tool's own logic, and the user approves each one (`decisions.md`).
8. Every task builds in the cloud, for linux/amd64, from the repository alone. Each `FROM` names
   either the harness image declared in `[harbor.image]` or an image a registry serves without
   credentials. Every other image a Dockerfile reads (`COPY --from`, `RUN --mount=from=`) is one
   a registry serves the same way. None of them is a tag that exists only on the machine that
   built it.

## Layout

| Path | What it holds | Committed |
| --- | --- | --- |
| `evals.toml` | Target and its configuration, live paths, judge settings | yes |
| `decisions.md` | The user's choices and the coding agent's stated assumptions, newest last | yes |
| `bin/` | Checkers (below), `reference/` examples, and `review/` briefs for fresh-context reviewers | yes |
| `harness/` | How the agent runs in a trial: adapter, recorder, image recipe; `mocks/` with `MOCKS.md`; `smoke/`, the smoke test | yes |
| `wiki/` | `requirements/`, `risks/`, `open-questions/`: one Markdown page per item; `index.md`, a short topical index; `attachments/` for cited trace excerpts | yes |
| `tests/<id>/` | One Harbor task per behavior | yes |
| `benchmarks/<id>/` | A CSV of cases and how each row becomes a task | yes |
| `evaluators/<id>/` | Criteria shared across tests | yes |
| `frozen.json` | Digests of frozen tests (written by `freeze.py`) | yes |
| `.env` | Secret values | never |
| `work/` | Runs, reviews, scratch | never |

## File contracts

### `evals.toml`

```toml
[target]
name = "retail customer-service agent"
configuration = "default retail domain"            # what was integrated
evidence = "README quick start runs --domain retail" # why it is the real configuration
live_paths = ["src/agent/prompts/**", "data/policy.md"]  # read live in every trial
entrypoints = ["python -m app.main"]               # how the agent starts; never run on the host

[judge]
mode = "api"                 # "api" (bin/reference/judge.py) or "subagent"
provider = "anthropic"       # or "openai" (then model is required)
model = "claude-opus-5-5"
effort = "medium"            # optional, Anthropic only

[runtime]
env = ["OPENAI_API_KEY"]     # names the agent needs in a trial; values in evals/.env

[harbor]                     # how any runner runs this suite, knowing nothing else about the agent
agent = "my_agent:MyEvalsAgent"                         # passed to `harbor run -a`
pythonpath = ["evals/harness", "evals/bin/reference"]   # where Harbor imports it from
agent_env = { EVALS_PASS_ENV = "OPENAI_API_KEY" }       # passed as --ae; never secret values
pass_mark = 0.8              # the user's answer at setup (there is no default), from 0 to 1

[harbor.image]               # the harness image tasks start FROM
tag = "my-agent-evals:deps"  # the name task Dockerfiles use in FROM
dockerfile = "evals/harness/Dockerfile"   # paths relative to the repository root
context = "."
```

**Which commit a hosted runner measures.** It measures a combined tree:

- `evals/` comes from `evalsSha`, the default-branch commit the test selection was built from.
  That includes `evals.toml`, the harness and its Dockerfile, and the tests.
- Everything else comes from the pull request's `headSha`: the agent's source and its
  dependency files.

So a pull request is measured with its own agent and dependencies, but with the default
branch's tests, harness and `[harbor]`. Its own edits under `evals/` are not used.

A harness context that spans both trees builds from both. For example, with context `.` and a
Dockerfile that copies `pyproject.toml` and `uv.lock`, the recipe comes from `evalsSha` and the
dependency files from `headSha`. Keep every harness file under `evals/`, and have the Dockerfile
copy from outside it only what the agent itself owns. Keep live paths outside `evals/` too, or a
pull request's edits to them are never measured. Locally, both trees are the one checkout.

From the root of that tree, a runner needs nothing but `[harbor]`:

1. Build the harness image: `docker build --platform linux/amd64 -f <dockerfile> -t <tag> <context>`.
   Nothing else may prepare the context first. To keep a wide context small, put a
   `<dockerfile name>.dockerignore` beside the Dockerfile; Docker and Modal both read it.
2. Run each task: `PYTHONPATH=<pythonpath, joined with :> HARBOR_TELEMETRY=0
   DOCKER_DEFAULT_PLATFORM=linux/amd64 uv tool run --python 3.12 --from harbor==0.23.0 harbor run
   -p <absolute task dir> -a <agent> --ae NAME=value ...`, with one `--ae` per `agent_env` entry
   and each `[runtime].env` name set in Harbor's environment. Without `DOCKER_DEFAULT_PLATFORM`,
   an arm64 machine builds each task for arm64 and cannot find the amd64 harness tag.
3. Score the judges. After the trials, score every judge evaluator the way `[judge]` says, and
   write `<trial dir>/judges/<id>.json` (see Scores below). `bin/reference/judge.py` implements
   this; a runner that runs no script from the repository implements the same contract itself.
   A hosted runner needs `[judge] mode = "api"`. In `"subagent"` mode there is no coding agent
   to judge with, so those judges stay pending and their tests can never pass.
4. Apply the pass rule.

**The pass rule.** An attempt (one trial) is scored when all of these hold:

- it has a trajectory;
- Harbor did not end it before its verifier ran;
- every evaluator the test declares has a score within its bounds. Code evaluators' scores are
  in `result.json` under `verifier_result.rewards`; judges' scores are in `judges/<id>.json`.

The attempt's score is the mean, over those evaluators, of
`(score - min_score) / (max_score - min_score)`. A test passes when the mean of its scored
attempts' scores is at or above `pass_mark`.

A test with no scored attempt has not passed. Report it apart, as a runtime failure, evaluator
error or pending judge, never as a zero.

`summarize.py` computes exactly this: it is each test's `mean`. A runner may also compute it
itself from Harbor's `result.json`, the `verifier/` files and `judges/<id>.json`.

How a runner supplies the harness image without editing a frozen test: a task Dockerfile that
uses the harness starts `FROM <tag>`. This can be `environment/Dockerfile`, or `tests/Dockerfile`
for a separate verifier. Locally, step 1 puts that tag in Docker's image store. A cloud builder
has no such store, and Harbor 0.23.0 gives it no way to resolve the tag. On Modal, Harbor builds
the Dockerfile with its own directory as the only context and passes no build arguments. It
passes registry credentials only for a `docker_image`, and then, unless forced to build, it
ignores the Dockerfile. So a hosted runner calls `harbor run -e <module:Class>` with its own
subclass of Harbor's Modal environment. That subclass builds `[harbor.image]` in the cloud from
the combined tree, then applies the rest of the task's Dockerfile on top of it, after the `FROM`
line and with the same context (Modal's `Image.dockerfile_commands`). Tests stay byte-identical,
so frozen digests and Harbor's task checksums match local runs.

For the substitution to be exact, a Dockerfile that starts FROM the harness image has no other
`FROM`, no instruction before it, and no parser directive (`# syntax=`, `# escape=`). Comments
are fine. `check_task.py` checks that, `[harbor]` itself, and every `FROM`, `COPY --from` and
`RUN --mount=from=` image:

- no build argument, and no platform other than linux/amd64;
- no registry on this machine or its private network;
- that the image's registry serves it anonymously, which it asks the way a cloud builder pulls.

It cannot tell whether a public image has a linux/amd64 variant. When a registry cannot be
reached, it notes that rather than failing, so run it with network access before relying on it.

### A test: `tests/<id>/task.toml`

A Harbor task with this table added. A test's identity is its path relative to `evals/`
(`tests/<id>`, `benchmarks/<id>/rows/<n>`), which `freeze.py` and `summarize.py` use.

```toml
[metadata.evals]
id = "<id>"                          # equals the directory name
behavior = "One sentence: the behavior this test checks."
covers = ["refund-policy"]           # wiki page ids, optional
prohibition = false                  # true only if doing nothing is a pass

[[metadata.evals.evaluators]]
id = "looks-up-order"
type = "code"                        # scored by the verifier
method = "exact"                     # code can decide it with certainty
criterion = "Calls get_order_details with the order id the user gave."
min_score = 0.0
max_score = 1.0

[[metadata.evals.evaluators]]
id = "explains-refund"
type = "judge"                       # scored after the trial from the trajectory
method = "semantic"                  # open-ended meaning: always a judge
criterion = "Explains the refund timeline in a way the policy supports."
rubric = "tests/judges/explains-refund.md"   # relative to the test directory
min_score = 0.0
max_score = 1.0
```

### The smoke test: `harness/smoke/`

A test in the same format whose only job is to prove the harness works: the real agent ran,
was recorded truthfully, and its output reached the evaluators. It is not an eval. Never select
it for a measurement, regression check, benchmark, optimization or pull-request gate, and never
report its scores as agent quality. Rerun it after any harness change, and when a test fails at
runtime: if the smoke test fails too, repair the harness (the `setup` skill); if it passes, the
problem is in that test. Because it is not an eval it is not frozen: fix its checks as the
harness needs, and the harness review confirms they check a real outcome and can fail.

### Scores

- Code evaluators (the verifier, `tests/test.sh`) write `/logs/verifier/reward.json`
  `{"<id>": number}` for every declared code evaluator, `/logs/verifier/feedback.json`
  `{"<id>": "text"}` for every score, and `/logs/verifier/errors.json` `{"<id>": "message"}`
  when a check could not run. `bin/reference/evaluate.py` writes all three.
- Judges write `<trial dir>/judges/<id>.json` after the trial:
  `{"score": n, "feedback": "...", "model": "...", "judge": "api"|"subagent"}` or
  `{"error": "..."}`.
- A low score is evidence about the agent. An error, a missing score, or a failed trial is not:
  report them apart, never as zeros.
- A separate verifier sees only `tests/` and the files `task.toml` lists in `artifacts`, so list
  every file under `/logs/agent/` that a check reads.

**Subagent judges.** With `[judge] mode = "subagent"`, judges are fresh subagents that have not
seen the run being judged or the conversation around it; one subagent may judge several trials.
Give each the rubric file, the criterion, the score bounds, the trajectory path and the output
path `<trial dir>/judges/<id>.json`, plus the rules `bin/reference/judge.py` gives its model:

- Judge only what the trajectory shows; do not assume actions that were not recorded.
- Score the whole relevant turn: all agent steps after a user step and before the next one.
- Accept any wording that satisfies the rubric; do not reward or punish style it does not mention.
- If the rubric grades a deliverable the user asked for and the agent did not produce it, give the
  minimum and say it is missing. If declining or asking was the right response, nothing is missing.
- Give a score within the bounds and feedback that names every unmet criterion, where it occurred,
  and what would earn full marks.

### Mocks

Each mock is listed in `harness/mocks/MOCKS.md` with the dependency it replaces, its real hosts or
functions, why, and the date the user approved it. A mock appends one JSON line per call to
`/logs/agent/mock-journal.jsonl`: `{"target", "operation", "input", "outcome", "response"}`, where
`outcome` is `matched`, `unmatched` or `error`. A call that changes the mock's state also records
the change as `state_change`. No journal means no calls.

### `frozen.json`

Written only by `freeze.py`: each test's digest (all files except `README.md`), when it was
frozen, and the reason for every re-freeze.

### Wiki pages

`wiki/<requirements|risks|open-questions>/<id>.md`, where the file name is the id. Each starts
with a `# ` title. A requirement states one behavior the agent must show; a risk states its
mechanism, trigger and observable failure; an open question states what is unclear and why it
matters. Tests cite pages in `covers`.

A risk page also carries short labelled lines, so risks can be ranked and tracked:

```markdown
# Refund promised before the order lookup

Mechanism: the refund policy sits early in the prompt, so the agent answers from policy alone.
Trigger: a user asks "can I get a refund?" without giving an order id.
Observable failure: the agent promises a refund without calling get_order_details.
Impact: high. Confidence: medium. Triggerability: high.
Status: admitted
Threatens: refund-policy
Found at: 3f2a9c1 (agent: support-bot)
Evidence: src/agent/prompts/refunds.md:12; attachments/ingest-2026-11/42.json

## Probes
- tests/refund-without-order-id (2026-11-04): triggered
```

`Status` is one of `proposed`, `admitted`, `confirmed`, `refined`, `rejected` or `retired`.
Ratings are `low`, `medium`, `high` or `critical`. `Threatens` lists requirement ids.

## Checkers

All read-only except `freeze.py`. Python 3.11+, standard library; the Harbor-based ones run the
pinned Harbor through `uv`.

| Command | Use it |
| --- | --- |
| `python3 evals/bin/check_task.py evals/tests/<id> [--verifier [--fixture DIR]]` | After writing or changing a test. Includes Harbor's own task loading. `--verifier` runs the verifier in Docker on a synthetic normal and empty-reply trajectory; `--fixture DIR` adds files (a database dump, a mock journal) to `/logs/agent` |
| `python3 evals/bin/check_trajectory.py <trajectory.json> [--turns N]` | On a trajectory from a trial |
| `python3 evals/bin/check_wiki.py` | After changing the wiki or a test's `covers` |
| `python3 evals/bin/summarize.py <run dir> [--baseline <dir>]` | After every run; with `--baseline` to compare two runs. Harbor records `-p` as typed, so run Harbor with absolute task paths. Harbor's checksum covers the whole task directory, so even a README edit makes runs incomparable |
| `python3 evals/bin/freeze.py evals/tests/<id>` | Before a test's first real run |

`bin/review/` holds the briefs for fresh-context reviews: `harness.md`, `test.md`, `change.md`.
`bin/reference/` holds examples to use or replace: a Harbor agent class and launcher
(`harbor_agent.py`, `launcher.py`), a judge (`judge.py`), a code-evaluator helper
(`evaluate.py`), and `harbor-notes.md`, the Harbor behaviors that fail silently.

## Working with Claude Code and Codex

- Ask the user with the host's question tool (`AskUserQuestion` in Claude Code,
  `request_user_input` in Codex); if neither is available, stop and print the question. Append
  every answer to `decisions.md`, and every consequential choice you made yourself as an
  assumption.
- Measuring another checkout (a worktree for a change, or the pre-change revision for a baseline):
  it gets the same `evals/` (copy it if it is not committed, and link `evals/.env` without reading
  it), and the harness runs that checkout's source.
- Run long jobs (image builds, Harbor runs) in the background and check on them when notified,
  but only in an interactive session. In a headless one (`claude -p`, `codex exec`) nothing wakes
  you after your turn ends and background jobs are killed with it: run them in the foreground with
  a long timeout, and do not end your turn while one is running.
- A review needs a fresh subagent that has not seen the work being reviewed: in Claude Code a
  general-purpose subagent, in Codex a subagent spawned without forked history. Give it the
  review brief and the paths, never your own conclusions.
- Codex's sandbox blocks the network and the Docker socket; Harbor, `uv` and Docker commands
  need escalated permissions there.
