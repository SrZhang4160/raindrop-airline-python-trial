# Dependency image for SkyServe trials: Python and the locked dependencies only.
# src/airline_support (a live path) is never baked in: airline_harbor_agent.py uploads it from the
# checkout being measured at the start of every trial. Built from evals.toml [harbor.image]
# (context: the repository root, narrowed to the lock inputs by Dockerfile.dockerignore).
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.8.14 /uv /usr/local/bin/uv
ENV PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONUNBUFFERED=1
WORKDIR /tmp/lock
COPY pyproject.toml uv.lock ./
RUN uv export --frozen --no-dev --no-emit-project --no-hashes -o requirements.txt \
    && pip install --no-cache-dir -r requirements.txt hatchling==1.33.0 editables==0.6 \
    && rm -rf /tmp/lock && mkdir -p /app /logs/agent /logs/verifier
WORKDIR /app
STOPSIGNAL SIGKILL
