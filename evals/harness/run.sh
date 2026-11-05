#!/usr/bin/env bash
# Build the harness image from evals.toml [harbor.image], then run Harbor trials for one task
# directory (or a directory of tasks), exactly as a hosted runner would.
#   evals/harness/run.sh <task path> [attempts] [-- extra harbor args]
# Needs Docker and uv. Reads OPENAI_API_KEY from evals/.env into Harbor's environment only.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TASK="$(cd "$1" && pwd)"; shift
ATTEMPTS=1
if [ $# -gt 0 ] && [ "$1" != "--" ]; then ATTEMPTS="$1"; shift; fi
[ "${1:-}" = "--" ] && shift
docker build --platform linux/amd64 -f "$ROOT/evals/harness/Dockerfile" -t airline-evals:deps "$ROOT" >&2
JOBS="${EVALS_JOBS_DIR:-$ROOT/evals/work/jobs}"; mkdir -p "$JOBS"
set -a; . "$ROOT/evals/.env"; set +a
cd "$ROOT/evals/work"   # no .env.local here, so Harbor loads no host values
HARBOR_TELEMETRY=0 DOCKER_DEFAULT_PLATFORM=linux/amd64 \
  PYTHONPATH="$ROOT/evals/harness:$ROOT/evals/bin/reference" \
  uv tool run --python 3.12 --from harbor==0.23.0 harbor run -y \
  -p "$TASK" -o "$JOBS" -k "$ATTEMPTS" -a airline_harbor_agent:AirlineAgent \
  --ae EVALS_ADAPTER_COMMAND="python3 /app/adapter.py" --ae EVALS_PASS_ENV=OPENAI_API_KEY "$@"
