# Starts FROM the harness image built from evals.toml [harbor.image]. The agent's source is
# uploaded at trial time by evals/harness/airline_harbor_agent.py, never copied here.
FROM airline-evals:deps
COPY scenario.json /app/scenario.json
STOPSIGNAL SIGKILL
