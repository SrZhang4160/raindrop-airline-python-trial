# Harbor 0.23.0: things that fail silently

Run Harbor pinned: `uv tool run --python 3.12 --from harbor==0.23.0 harbor ...`.
Everything below was observed in Harbor's source or in RELAI's CLI; none of it raises an
error that explains itself.

- **Exit status lies.** `harbor run` exits 0 even when every trial errored. Read each
  trial's `result.json` (`exception_info`, `verifier_result`), or run `summarize.py`.
- **Telemetry is on by default.** Set `HARBOR_TELEMETRY=0` in Harbor's environment.
- **Host values leak into Harbor.** Harbor loads `.env.local` from its working directory and
  every value in `--env-file` into its own process, and `${VAR}` in `task.toml` copies host
  values into containers (after an interactive prompt that `-y` skips). Run Harbor from a
  directory without `.env.local`, pass only the variables the agent needs (`--ae NAME=...`
  or Harbor's environment), and keep `${...}` out of `task.toml`.
- **A separate verifier gets no tests.** With `[verifier] environment_mode = "separate"`, the
  verifier image is built from `tests/` and Harbor does not upload `tests/` into it, so its
  `tests/Dockerfile` must `COPY . /tests/`. Without `environment_mode` set, the verifier runs
  inside the agent's container.
- **The verifier sees only declared artifacts.** In separate mode, list
  `artifacts = ["/logs/agent/trajectory.json"]` (plus any other file under `/logs/agent/` the
  checks read) at the top of `task.toml`.
- **Hidden 600-second defaults.** `[verifier].timeout_sec` and `[environment].build_timeout_sec`
  default to 600; a slow build or judge-free verifier past that fails as an exception, not a
  low score. Set them explicitly when a phase can run long. `[agent].timeout_sec` has no default.
- **Build context is the task's `environment/` directory only.** A Dockerfile there cannot
  COPY files from elsewhere in the repository; build the shared harness image from
  `[harbor.image]` in `evals.toml` and start from it (`FROM <its tag>`), or copy inputs in at
  trial time.
- **Cloud builds see only registries.** With `-e modal`, Harbor builds `environment/Dockerfile`
  in Modal's cloud (linux/amd64) with no build arguments and no registry credentials, so a
  `FROM` naming a tag built on your machine fails there. Credentials go only to
  `[environment].docker_image` tasks, and Harbor then ignores their Dockerfile. The `[harbor]`
  contract in `evals/README.md` says how a runner supplies the harness image instead.
- **`-p` takes one path.** Pass a directory of tasks and filter with `-i <glob>`, or list the
  tasks in a job config (`-c job.yaml`; `harbor job schema` prints its schema). Directories
  that are not valid tasks are skipped without a message, and unknown `task.toml` keys are
  ignored, so check the trial count.
- **Images are deleted after each trial** (`--delete` is the default). `--no-delete` keeps
  them for the next run.
- **A custom agent class is imported from Harbor's tool environment.** `uv tool run` does not
  add the working directory to `sys.path`; set `PYTHONPATH` to the directory holding it.
- **Containers ignore SIGTERM** (`sleep infinity` as PID 1), so teardown waits for Docker's
  stop timeout unless the image sets `STOPSIGNAL SIGKILL`.
- **A missing reward file is an exception**, and `reward.txt` holds one number. Write
  `reward.json` with one key per evaluator.
- **ATIF**: Harbor validates `ATIF-v1.0` to `v1.8`
  (`python -m harbor.utils.trajectory_validator <file>`); `check_trajectory.py` runs it.
- **Useful extras**: `harbor view <jobs dir>` browses trajectories locally;
  `harbor trial regrade <trial dir> -p <task>` reruns only the verifier on a recorded run;
  `harbor job resume -p <job dir>` finishes an interrupted job.
