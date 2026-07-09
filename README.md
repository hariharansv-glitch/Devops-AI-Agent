# AI DevOps Assistant

A production-grade AI assistant that inspects a real Linux VM over SSH and
answers infrastructure questions using **live server data**. The assistant
is built on **Google's Agent Development Kit (ADK) 2.x** and **Gemini 2.5**,
with the exact architecture Google recommends: an `LlmAgent` with typed
`FunctionTool`s, an ADK `Runner`, `InMemorySessionService`,
`InMemoryMemoryService`, structured tool responses, and full callback
coverage (`before_/after_agent`, `before_/after_tool`).

The assistant is deliberately grounded: it **never hallucinates**. Every
factual claim must come from a tool call. Destructive actions (restart,
prune, ...) require the operator's explicit confirmation and are also
gated by a global `READ_ONLY_MODE` flag.

---

## Table of contents

- [Features](#features)
- [Architecture](#architecture)
- [Project layout](#project-layout)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Usage](#usage)
  - [Interactive CLI](#interactive-cli)
  - [HTTP API](#http-api)
  - [Docker Compose](#docker-compose)
- [Safety model](#safety-model)
- [Extending the toolbelt](#extending-the-toolbelt)
- [Testing](#testing)
- [Troubleshooting](#troubleshooting)

---

## Features

- **Google ADK 2.x native.** Every reasoning step goes through ADK: `LlmAgent`,
  `Runner`, tool declarations, session state, memory service, and callbacks.
  No custom orchestration, no custom function calling.
- **Live Linux inspection over SSH.** Password *or* key-based auth, thread-safe
  connection reuse, hard-coded safety denylist, and structured
  `CommandResult`s.
- **Full DevOps toolbelt.** SSH / Linux / Docker / Jenkins / Logs tools cover
  CPU, RAM, disk, load, uptime, services, ports, system logs, Docker
  inventory, Docker health, `docker stats`, `docker inspect`, container logs,
  `docker system df`, Jenkins status + logs + restart, and more.
- **Never-hallucinate prompt.** The system instruction requires the LLM to
  ground every claim in a tool call and to explicitly refuse to guess when
  a tool fails.
- **Two-step confirmation for destructive actions.** Restarts, prunes, and
  Jenkins restarts all return a `confirmation_required` payload on the first
  call; only after the user agrees does the LLM re-issue the call with
  `confirm=True`.
- **Two surfaces.** A rich Typer + Rich CLI (`python main.py`) and a FastAPI
  service (`python main.py serve`) with `/chat`, `/chat/stream`, `/healthz`,
  `/readyz`, `/docs`.
- **Production quality.** Type hints everywhere, Pydantic v2 for validation,
  Loguru with rotating file logs, dependency injection via a small service
  container, hermetic pytest suite with an SSH fake, multi-stage Dockerfile,
  Docker Compose, and health checks.

---

## Architecture

```
                            ┌──────────────────────────────┐
                            │       Google Gemini 2.5      │
                            └──────────────▲───────────────┘
                                           │ ADK
┌───────────────┐     ┌───────────────┐    │
│   FastAPI     │     │    Typer CLI  │    │
│  /chat, ...   │     │  main.py chat │    │
└──────┬────────┘     └───────┬───────┘    │
       │                      │            │
       └────────┬─────────────┘            │
                ▼                          │
        ┌────────────────────────────────────────────┐
        │              AgentService                  │
        │  ADK Runner + InMemorySessionService +     │
        │  InMemoryMemoryService + Callbacks + State │
        └────────────────────┬───────────────────────┘
                             │
                    ADK FunctionTools
   ┌─────────────┬───────────┴─────────┬─────────────┬──────────────┐
   ▼             ▼                     ▼             ▼              ▼
 SSH Tool   Linux Tool           Docker Tool    Jenkins Tool     Logs Tool
   │             │                     │             │              │
   └────┬────────┴───────────┬─────────┴────┬────────┘              │
        ▼                    ▼              ▼                       │
   SSHService          LinuxService    DockerService           (pure funcs)
                        (via SSH)      (via SSH)
                             │              │
                             └────┬─────────┘
                                  ▼
                       Remote Linux VM (paramiko)
```

- **`app.agent`** owns the `LlmAgent`, the system instruction, callbacks,
  session/memory services, and the runner facade.
- **`app.tools`** exposes ADK `FunctionTool` bindings that Gemini can call.
- **`app.services`** encapsulates all I/O (SSH, Docker CLI on the remote
  host, Jenkins, ...).
- **`app.api`** exposes FastAPI routes; **`main.py`** exposes the Typer CLI.
- Both surfaces share the same `AgentService` singleton, so behaviour is
  identical.

---

## Project layout

```
Devops-AI-Agent/
├── app/
│   ├── agent/
│   │   ├── callbacks.py      # ADK before_/after_ callbacks (safety + logs)
│   │   ├── instructions.py   # System prompt
│   │   ├── memory.py         # InMemoryMemoryService factory
│   │   ├── root_agent.py     # LlmAgent factory
│   │   ├── runner.py         # AgentService (Runner facade) + singleton
│   │   └── session.py        # InMemorySessionService helpers
│   ├── api/routes.py         # FastAPI /chat, /chat/stream, /healthz, /readyz
│   ├── config/settings.py    # Pydantic Settings (env-driven)
│   ├── schemas/models.py     # Pydantic v2 models for tool payloads / API
│   ├── services/
│   │   ├── docker_service.py
│   │   ├── jenkins_service.py
│   │   ├── linux_service.py
│   │   └── ssh_service.py
│   ├── tools/
│   │   ├── docker_tool.py
│   │   ├── jenkins_tool.py
│   │   ├── linux_tool.py
│   │   ├── logs_tool.py
│   │   └── ssh_tool.py
│   └── utils/                # Loguru logger + formatters
├── tests/                    # Hermetic pytest suite (SSH is faked)
├── main.py                   # Typer CLI: `chat`, `serve`, `version`
├── requirements.txt
├── Dockerfile                # Multi-stage build
├── docker-compose.yml
├── .env.example
├── pytest.ini
└── README.md
```

---

## Requirements

- **Python 3.12** (3.10+ works, 3.12 is the target).
- A **Google Gemini API key** (from [Google AI Studio](https://aistudio.google.com/app/apikey))
  or a **Vertex AI** project with ADC configured.
- **SSH access** to a Linux VM you want to inspect (password or private key).
- Optional: **Docker** installed on the remote VM if you want to use the
  Docker tools; **Jenkins** installed if you want the Jenkins tools.

---

## Quick start

```bash
# 1. Clone and enter the repo.
cd Devops-AI-Agent

# 2. Create and activate a virtual environment.
python -m venv .venv
# Windows PowerShell:
.\.venv\Scripts\Activate.ps1
# macOS / Linux:
# source .venv/bin/activate

# 3. Install dependencies.
pip install -r requirements.txt

# 4. Copy the env template and fill in your values.
cp .env.example .env
# then edit .env

# 5. Run the interactive CLI.
python main.py chat
```

You should see the DevOps Copilot banner. Try:

- `Why is my server slow?`
- `How much RAM is available?`
- `Show Docker containers.`
- `Analyze system logs.`

---

## Configuration

All configuration is environment-driven (see `.env.example` for the full
list). The most important variables are:

| Variable                      | Description                                      | Default            |
| ----------------------------- | ------------------------------------------------ | ------------------ |
| `GOOGLE_API_KEY`              | Gemini API key (AI Studio).                      | *(required)*       |
| `GOOGLE_GENAI_USE_VERTEXAI`   | Set to `TRUE` to route via Vertex AI.            | `FALSE`            |
| `GOOGLE_CLOUD_PROJECT`        | GCP project id (Vertex only).                    | -                  |
| `MODEL_NAME`                  | Gemini model name.                               | `gemini-2.5-flash` |
| `VM_HOST` / `VM_PORT`         | Target Linux VM hostname / SSH port.             | -                  |
| `VM_USER`                     | SSH username.                                    | -                  |
| `VM_PASSWORD`                 | Password (used when no private key is set).      | -                  |
| `VM_PRIVATE_KEY`              | Path to a private key **or** raw PEM contents.   | -                  |
| `VM_PRIVATE_KEY_PASSPHRASE`   | Passphrase for encrypted keys.                   | -                  |
| `SSH_CONNECT_TIMEOUT`         | Seconds to wait for the SSH handshake.           | `15`               |
| `SSH_COMMAND_TIMEOUT`         | Seconds per remote command.                      | `60`               |
| `SSH_AUTO_ADD_HOST_KEYS`      | Auto-accept unknown host keys (dev only).        | `TRUE`             |
| `READ_ONLY_MODE`              | Block *all* destructive tools when `TRUE`.       | `TRUE`             |
| `API_HOST` / `API_PORT`       | FastAPI bind address / port.                     | `0.0.0.0` / `8000` |
| `CORS_ORIGINS`                | Comma-separated origins (or `*` for dev).        | `*`                |
| `LOG_LEVEL`                   | `DEBUG`, `INFO`, `WARNING`, ...                  | `INFO`             |
| `LOG_DIR`                     | Directory for rotating log files.                | `logs`             |
| `LOG_JSON`                    | Emit JSON logs (for log aggregators).            | `FALSE`            |

---

## Usage

### Interactive CLI

```powershell
python main.py                       # same as `chat`
python main.py chat --session-id demo
python main.py chat --ask "Why is my server slow?"   # one-shot mode
python main.py version
```

CLI keywords: `/help`, `/session`, `/reset`, `/quit`.

### HTTP API

Start the FastAPI service:

```powershell
python main.py serve
# or, with auto-reload for local development:
python main.py serve --reload
```

Then hit the endpoints:

```powershell
# Chat
Invoke-RestMethod -Method Post -Uri http://localhost:8000/chat -ContentType 'application/json' `
    -Body (@{ message = 'Why is my server slow?' } | ConvertTo-Json)

# Streaming chat (text/plain)
curl -N http://localhost:8000/chat/stream `
    -H 'content-type: application/json' `
    -d '{"message":"Why is my server slow?"}'

# Health / readiness
curl http://localhost:8000/healthz
curl http://localhost:8000/readyz
```

OpenAPI/Swagger UI: <http://localhost:8000/docs>.

Request body:

```json
{
  "message": "Why is my server slow?",
  "session_id": "optional-existing-session",
  "user_id": "optional-user-id"
}
```

Response body:

```json
{
  "answer": "The server is under memory pressure. `linux_memory_usage` shows 14.2 GiB used out of 16.0 GiB (88 %) ...",
  "session_id": "session-abc123",
  "user_id": "cli-user",
  "tool_calls": ["linux_snapshot", "docker_stats"],
  "duration_ms": 2384.1
}
```

### Docker Compose

```bash
cp .env.example .env
# edit .env

docker compose up --build
```

The service will listen on `http://localhost:8000`. Logs are persisted under
`./logs` on the host. Provide an SSH private key by mounting it read-only
(see the commented `volumes` block in `docker-compose.yml`) and pointing
`VM_PRIVATE_KEY` at the mounted path.

---

## Safety model

Three independent layers protect your VM:

1. **System prompt.** The agent's instruction forbids hallucination, forbids
   emitting destructive shell commands, and requires explicit confirmation
   before any change.
2. **Tool-level confirmation.** `docker_restart_container`, `docker_prune`,
   and `jenkins_restart` all return a `ConfirmationRequired` payload unless
   called with `confirm=True`. Gemini relays the prompt to the user and only
   re-issues the call after they agree.
3. **`READ_ONLY_MODE`.** When `READ_ONLY_MODE=TRUE` (the default), the ADK
   `before_tool_callback` short-circuits any confirmed destructive call with
   a `blocked` response. Setting it to `FALSE` is a deliberate operator
   choice.

Additionally, the raw SSH channel refuses to execute a hard-coded set of
patterns (`rm -rf /`, fork bombs, `mkfs`, `reboot`, `poweroff`,
`dd of=/dev/sdX`, ...) regardless of who asked. Operators can add more
patterns via `SSH_EXTRA_DENYLIST`.

---

## Extending the toolbelt

1. Add a method to the appropriate service in `app/services/` (e.g. a new
   Linux metric to `LinuxService`).
2. Wrap it in an ADK `FunctionTool` inside the matching module in
   `app/tools/` — remember the docstring becomes the tool's LLM-facing
   description, so make it descriptive and mention when to use it.
3. Register the new tool in the `build_*_tools()` factory.
4. Add a unit test in `tests/test_tools.py` using the SSH fake.

For destructive tools, follow the pattern used by `docker_restart_container`:

- Accept a `confirm: bool` parameter.
- Return a `ConfirmationRequired` payload when `confirm is False`.
- Add the tool name to `app.agent.callbacks.DESTRUCTIVE_TOOLS`.

---

## Testing

```powershell
# From the repo root, with dependencies installed:
pytest
```

The suite is fully hermetic:

- **No SSH.** A `FakeSSHService` replies to registered commands.
- **No LLM.** The API test uses a `FakeAgentService`.
- **No network.** No test opens a socket or spawns a subprocess.

Coverage report:

```powershell
pytest --cov=app --cov-report=term-missing
```

---

## Troubleshooting

| Symptom                                                    | Likely cause / fix |
| ---------------------------------------------------------- | ------------------ |
| `SSH authentication failed for user ...`                   | Wrong username / password / key. Verify with `ssh -v` from the host. |
| `Private key is encrypted; set VM_PRIVATE_KEY_PASSPHRASE.` | Provide the passphrase, or generate an unencrypted key for the assistant. |
| `docker binary is not installed on the remote host`       | Install Docker on the VM or grant the SSH user permission to run it (add to `docker` group). |
| Agent returns "confirmation_required" forever              | Reply `yes` / `confirmed`; the agent re-issues the tool call with `confirm=True`. |
| "Read-only mode is enabled; destructive operations are disabled." | Set `READ_ONLY_MODE=FALSE` in `.env` **and** restart the service. |
| `GOOGLE_CLOUD_PROJECT must be set ...`                    | Either unset `GOOGLE_GENAI_USE_VERTEXAI` or provide `GOOGLE_CLOUD_PROJECT`. |
| CLI hangs on the first request                             | Gemini call in progress — check `logs/devops_agent.log`. |

---

## License

MIT. See the top of every source file for header hints; add a
`LICENSE` file if you plan to distribute publicly.
