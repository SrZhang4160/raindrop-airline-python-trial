# Airline Support Agent

A terminal airline customer support agent built with the OpenAI Agents SDK for Python.

The agent can help with demo booking lookups, baggage policy, seat changes, and flight-change guidance. Each terminal conversation is saved as JSONL with an ATIF trajectory under `logs/`.

The agent should end all responses with "Please let me know if you have any questions".

The agent should not answer off-topic, non-airline questions. It should politely say it can only help with airline booking, baggage, seat, and flight-change questions.

## Prerequisites

- Python 3.11+
- `uv`
- GitHub CLI (`gh`), authenticated with `gh auth login`
- `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` in your environment, or ready to paste when prompted

## Fork and Clone

Fork the sample repo to your GitHub account, then clone your fork:

```sh
gh auth login
gh repo fork relai-ai/airline-customer-support-agent --clone
cd airline-customer-support-agent
```

## Start the Agent

Run the launcher from the repository root:

```sh
./start.sh
```

The script prompts for an OpenAI or Anthropic API key when needed, saves it to the ignored `.env` file, installs Python dependencies, and starts a terminal chat session.
When both `OPENAI_API_KEY` and `ANTHROPIC_API_KEY` are set, OpenAI is used.
Pass a log name to save the session to a fixed file such as `logs/booking-help.jsonl`:

```sh
./start.sh booking-help
```

Type `exit`, `quit`, or `q` to end the chat. Each run prints its log and trajectory paths. Named sessions overwrite their previous logs.

## Local Logs

Each chat session is saved as JSONL under `logs/`:

```text
logs/session-<id>.jsonl
logs/session-<id>.atif.json
```

Session files are ignored by Git. The JSONL log contains user and assistant messages; the ATIF trajectory also captures model and tool activity. Both preserve turns within the session. Reusing a named session overwrites its previous logs.

## Checks

```sh
uv run pytest
bash -n start.sh
```

## Evals

`evals/` holds agent tests built with [agent-evals](https://github.com/relai-ai/agent-evals). They run the real agent in Harbor trials and check its tool calls. RELAI runs them on every pull request and reports the result as a Check. See `evals/README.md` to run them locally.
