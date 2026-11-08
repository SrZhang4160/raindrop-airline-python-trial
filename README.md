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

## Raindrop monitoring

To monitor terminal conversations, set `RAINDROP_WRITE_KEY` in the ignored `.env`
file or process environment. Set `RAINDROP_PROJECT_ID` to a project slug if
needed; leaving it empty uses your organization's default Raindrop project.
Never commit the real write key. `RAINDROP_DEBUG=true` enables SDK diagnostics
while checking delivery; return it to `false` after verifying events.

Run the installed `airline-support` entry point from this directory, using your
normal secure provider-key setup. Each model turn is recorded under the event
name **SkyServe Airline Support**, with tool calls; the actual tool catalog is
also supplied through SDK trace metadata. The local OS account identifies the
user, and the app's session ID groups the conversation. Only one terminal conversation should run per
process; the framework's Raindrop trace processor is process-global.

Raindrop's PII redaction is enabled for event input/output. It does not guarantee
redaction of every tool-span field. Treat chat content and tool inputs/outputs
as telemetry sent to Raindrop and avoid entering secrets. The existing JSONL
and ATIF logs remain local and unchanged. Queued monitoring data is drained when chat exits,
including after an agent error. Monitoring initialization failures emit a
generic warning and allow the chat to continue. Without a write key, no
Raindrop client is initialized.

To verify delivery, ask `Can you look up booking SKY123?`, then exit the chat.
In Raindrop, check the **SkyServe Airline Support** event for populated input,
output, model/token usage, the matching session ID, and a `lookup_booking` tool
span. API ingestion acknowledgment establishes delivery; use the dashboard to
confirm the event has been indexed. Direct calls to `stream_agent_response`
outside the terminal chat do not initialize a monitoring session.

The Anthropic path keeps SDK tracing enabled when a Raindrop key is present;
provider selection is unchanged. The integration's live verification used
OpenAI. The Anthropic monitoring configuration has unit coverage but has not
been verified with a live Anthropic call.

## Checks

```sh
uv run pytest
bash -n start.sh
```
