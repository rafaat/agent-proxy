# Agent Proxy

> [!WARNING]
> **Experimental project. Use at your own risk.**
>
> The gateway runs on your machine, but any MCP or ACP client that holds the API key gets
> effectively unrestricted access to every agent CLI configured on this host. Through those
> agents, a client can read and edit files, run shell commands and use the agents' own
> credentials, with the same permissions as the user running the gateway. The workspace
> allowlist is not a sandbox (see [Security model](#security-model)). Only connect clients
> you fully trust, keep the API key secret, and prefer running it in an isolated container
> or VM.

One HTTP endpoint that exposes four local coding agents to remote MCP and ACP clients (for example Nod):

- **Claude Code** (`claude`): headless `claude -p`, plus its native tools proxied from `claude mcp serve`
- **GitHub Copilot CLI** (`copilot`): headless `copilot -p` or native ACP (`copilot --acp`)
- **OpenCode** (`opencode`): headless `opencode run` or native ACP (`opencode acp`)
- **OpenAI Codex** (`codex`): headless `codex exec`, or ACP through an adapter such as `codex-acp`

Everything runs on one port, so you only tunnel one URL.

```
remote orchestrator ──HTTPS──> tunnel ──> :8765 Agent Proxy ──stdio/subprocess──> claude / copilot / opencode / codex
```

## Endpoints

| Path | Transport | Contents |
| --- | --- | --- |
| `/mcp` | Streamable HTTP | `ask_<agent>` for all four, `list_sessions`, `list_available_agents`, `claude_<Tool>` |
| `/sse` + `/messages/` | Legacy HTTP+SSE | Same as `/mcp` |
| `/agents/claude/mcp`, `/agents/claude/sse` | Streamable HTTP / SSE | `ask_claude`, `list_sessions` + `claude_<Tool>` |
| `/agents/copilot/mcp`, `/agents/copilot/sse` | Streamable HTTP / SSE | `ask_copilot`, `list_sessions` |
| `/agents/opencode/mcp`, `/agents/opencode/sse` | Streamable HTTP / SSE | `ask_opencode`, `list_sessions` |
| `/agents/codex/mcp`, `/agents/codex/sse` | Streamable HTTP / SSE | `ask_codex`, `list_sessions` |
| `POST /acp/session/prompt` | JSON | ACP-style one-shot prompt for non-MCP clients |
| `GET /acp/sessions?agent=&cwd=&limit=` | JSON | Same data as `list_sessions` |
| `GET /acp/agents` | JSON | Same data as `list_available_agents` |
| `GET /health` | JSON | Liveness. The only unauthenticated route |

Every `ask_*` tool takes `prompt`, plus optional `working_dir`, `model` and `session_id`.

### Interactive approvals

Use `ask_<agent>` for unattended work that does not need user approval. Use
`ask_<agent>_interactive` when a task may require approval (for example, a shell command
such as `git status` or a file edit), or to retry when the regular tool reports a permission
denial. Lack of MCP form elicitation is not a reason to choose the regular tool.

Use `ask_claude_interactive`, `ask_copilot_interactive`, `ask_opencode_interactive`, or
`ask_codex_interactive` on the unified or corresponding per-agent MCP endpoint. These tools
use ACP. Clients with MCP form elicitation can present the approval form directly. Clients
without it, such as Nod, receive an `approval_required` tool result with the question and
reply details in both structured and plain-text content. The client must ask the user, not
decide on their behalf.

When the agent requests permission, the client shows the reported action and any reported
paths. Select `approve: true` to allow it once. Declining, cancelling, or not answering within
60 seconds does not approve the action. Requests that report a path outside `ALLOWED_WORKSPACES`
are denied without prompting. The overall agent timeout still applies. Actions the agent
already permits do not prompt, so configure its permission rules accordingly.

For clients without elicitation, call the same tool with the returned `session_id` and
one-time `approval_id` after the user answers. Omit `prompt`, `working_dir`, and `model`:

```json
{
  "session_id": "<same agent session>",
  "approval": {
    "approval_id": "<one-time ID from the result>",
    "approve": true
  }
}
```

Use `false` to deny. The call resumes the same paused turn and returns either the final
answer or the next approval question. The paused turn lives in the running server process.
If unanswered for 60 seconds (or if the overall agent timeout expires first), the turn
ends without approving the action. Restarting the server also discards pending approvals.

Copilot and OpenCode use their configured binaries and native ACP arguments. For Claude and
Codex, install trusted ACP adapters on the host (defaults: `claude-agent-acp` and `codex-acp`),
or set `CLAUDE_INTERACTIVE_BIN` and `CODEX_INTERACTIVE_BIN` to their executable paths. All
four tools use the corresponding `<AGENT>_ACP_ARGS`. The regular `ask_<agent>` tools continue
to use `<AGENT>_MODE` and do not show approval forms.

## Sessions

Every `ask_*` call returns a `session_id`, both in `structuredContent` and as a second text block
so text-only orchestrators can read it. Pass it back to continue the same conversation with full
context instead of starting over:

```jsonc
// 1. start
{"name": "ask_claude", "arguments": {"prompt": "Find the flaky test", "working_dir": "/Users/you/dev/project-a"}}
// -> "...answer..." + "session_id: 25341a9e-... (pass it as session_id to continue this conversation)"
// 2. continue
{"name": "ask_claude", "arguments": {"prompt": "Now fix it", "session_id": "25341a9e-..."}}
```

`list_sessions` returns existing sessions recorded at or below `working_dir`, newest first, with id,
title and timestamps. On the unified endpoint it takes an optional `agent`. On `/agents/<agent>/mcp`
it is scoped to that agent. It also shows sessions you started yourself in a terminal, so a remote
orchestrator can pick up local work. `session_id: "latest"` resumes the newest session under `working_dir`.

How each agent's sessions are found and resumed:

| Agent | Listed from | New session id | Resume |
| --- | --- | --- | --- |
| Claude | `~/.claude/projects/<cwd>/*.jsonl` | gateway UUID via `--session-id` | `--resume <id>` |
| Copilot | `~/.copilot/session-state/*/workspace.yaml` | gateway UUID via `--session-id` | `--resume=<id>` |
| OpenCode | `opencode session list --format json` | `sessionID` in `--format json` events | `--session <id>` |
| Codex | `~/.codex/sessions/**/rollout-*.jsonl` | `thread_id` in `--json` events | `exec resume <id>` |

In ACP mode, resuming uses `session/resume` or `session/load`, depending on what the agent advertises.

A session can only be resumed if its recorded directory is inside `ALLOWED_WORKSPACES`, and inside
`working_dir` when one is given. The run happens in that directory. Two turns into the same session
never run at the same time. The second one waits.

## Setup

Requires Python 3.11+ and whichever agent CLIs you want, installed and logged in on the host
(`claude`, `copilot login`, `opencode auth login`, `codex login`).

```bash
uv sync                    # or: python -m venv .venv && .venv/bin/pip install -r requirements.txt -e .
cp .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(32))"   # paste into GATEWAY_API_KEY
```

At minimum, set `GATEWAY_API_KEY` and `ALLOWED_WORKSPACES` in `.env`.

## Run

```bash
uv run uvicorn gateway.server:app --host 127.0.0.1 --port 8765
# or, using HOST/PORT from .env:
uv run agent-proxy
```

Keep it bound to `127.0.0.1` and let the tunnel handle public traffic. Logs are JSON lines on stderr.

## Configuration

All settings come from environment variables or `.env`. See [.env.example](.env.example) for the full list.

### Command templates

CLI flags live in `.env`, so you can adjust them without touching Python. Templates use shell-style
quoting and support `{prompt}`, `{cwd}`, `{model}` and `{session_id}`.

| Variable | Default (verified against claude 2.1, copilot 1.0, opencode 1.19, codex 0.145) |
| --- | --- |
| `CLAUDE_ARGS` | `-p --output-format text --permission-mode acceptEdits` (prompt sent on stdin) |
| `COPILOT_ARGS` | `--prompt={prompt} --silent --no-color --no-ask-user --allow-tool write` |
| `OPENCODE_ARGS` | `run --format json {prompt}` |
| `CODEX_ARGS` | `exec --json --skip-git-repo-check --sandbox workspace-write --color never -` (prompt sent on stdin) |
| `*_RESUME_ARGS` | Full replacement for `*_ARGS` when `session_id` is given. See `.env.example` |
| `*_MODEL_ARGS` | `--model {model}` (claude, copilot), `-m {model}` (opencode, codex). Only added when a model is set |

- If a template has no `{prompt}`, the prompt is written to stdin. This avoids argv length limits.
- Placeholders are substituted after the template is split and never pass through a shell, so a prompt
  is always exactly one argument. A bare `{prompt}` that starts with `-` gets a leading space, so it
  can't be read as a flag. Model names and session ids are checked against strict patterns.
- In non-interactive mode, agents deny tools that have no allow rule. Widen permissions on purpose,
  for example `COPILOT_ARGS=... --allow-tool 'shell(git:*)'` or `CLAUDE_ARGS=... --allowedTools "Bash(npm test)"`.

### ACP mode

Set `OPENCODE_MODE=acp` or `COPILOT_MODE=acp` to drive that agent through the Agent Client Protocol
(JSON-RPC over stdio) instead of its one-shot CLI. The gateway runs `initialize`, `session/new`,
an optional model selection, and `session/prompt`, collects the streamed `agent_message_chunk`
updates, and answers `session/request_permission` using `ACP_PERMISSION_POLICY`. A permission request
that touches a path outside `ALLOWED_WORKSPACES` is always rejected. Claude Code and Codex have no
built-in ACP server. To use ACP with them, point `CLAUDE_BIN` or `CODEX_BIN` at an ACP adapter (for
example `codex-acp`) and set `<AGENT>_MODE=acp`. `<AGENT>_ACP_ARGS` can stay empty for adapters.

### Claude native tool proxy

On startup the gateway launches `claude mcp serve` as a supervised child (it restarts with backoff if
the child dies), lists its tools, and re-exports the allowlisted ones as `claude_<Tool>`.

- `CLAUDE_PROXY_TOOLS` defaults to `Read,Edit,Write,NotebookEdit,WebFetch,WebSearch`. Use `*` to expose all.
- `file_path`, `notebook_path` and `path` arguments are resolved and must fall inside `ALLOWED_WORKSPACES`.
- `Bash` is off by default. Path checks can't sandbox shell commands, so enable `claude_Bash` only
  when you fully trust every caller holding the API key.

## Security model

- **Auth.** Send `Authorization: Bearer <GATEWAY_API_KEY>` on every route except `/health`.
  Legacy SSE clients must authenticate both the stream and the `/messages/` POSTs; a session ID
  alone is not a credential. The gateway refuses to start without a key unless `ALLOW_NO_AUTH=true`.
- **Workspace allowlist, not a filesystem sandbox.** The proxy checks requested working
  directories, session directories, explicit proxied file-tool paths, and paths reported in ACP
  permission requests. It follows symlinks before checking. It cannot restrict arbitrary paths
  used inside the agent, shell commands, or other tools. Treat API-key holders as trusted operators.
  For an enforced boundary, run the service and agents in a restricted container or VM with only
  the intended workspaces and credentials exposed.
- **Limits.** Each agent has its own `asyncio.Semaphore` (`MAX_CONCURRENCY_PER_AGENT`). Every run has a
  `SUBPROCESS_TIMEOUT_SECONDS` limit. On timeout or cancellation, the proxy terminates the
  process group on POSIX, even if its leader has exited, with a bounded cleanup grace period.
  Output is capped at
  `MAX_OUTPUT_BYTES` and prompts at `MAX_PROMPT_CHARS`.
- **Secrets.** `GATEWAY_API_KEY` is removed from the environment of child agents. Bearer tokens,
  `api_key=` and `session_id=` values, and common token formats (`sk-`, `ghp_`, `github_pat_`)
  are redacted from app logs.
  Prompt text is never logged, only its length.
- **DNS rebinding.** Set `ALLOWED_HOSTS` and `ALLOWED_ORIGINS` to enable the MCP SDK's Host/Origin checks.

Incoming MCP tool calls log the requested tool name at INFO without logging arguments. Agent
invocations also log agent, mode, cwd, model, duration, exit code, timeout and truncation flags.

## Verify locally

### MCP Inspector

```bash
npx @modelcontextprotocol/inspector
```

In the Inspector UI, pick transport **Streamable HTTP** and URL `http://127.0.0.1:8765/mcp`. Add the
header `Authorization: Bearer <GATEWAY_API_KEY>`, then connect. Run `list_available_agents` first to
check that each CLI is installed and authenticated. For SSE, use `http://127.0.0.1:8765/sse?api_key=<key>`.

### curl

```bash
KEY=your-key
curl -s localhost:8765/health
curl -s -H "Authorization: Bearer $KEY" localhost:8765/acp/agents
curl -s -H "Authorization: Bearer $KEY" "localhost:8765/acp/sessions?agent=claude&cwd=/Users/you/dev/project-a"

curl -s -H "Authorization: Bearer $KEY" -H 'content-type: application/json' \
  localhost:8765/acp/session/prompt \
  -d '{"agent":"opencode","cwd":"/Users/you/dev/project-a","prompt":[{"type":"text","text":"Summarize README.md"}]}'
```

Add `"sessionId": "..."` to the request body to continue a session. The ACP bridge returns:

```json
{"agent":"opencode","mode":"cli","cwd":"...","sessionId":"ses_...","stopReason":"end_turn","content":[{"type":"text","text":"..."}],
 "ok":true,"exitCode":0,"durationMs":4342,"timedOut":false,"truncated":false,"stderr":""}
```

Status codes: `200` success, `400` invalid input or disallowed workspace path, `401` auth, `422` bad payload,
`502` agent failed, `503` CLI not installed, `504` timed out.

## Expose to Nod

The gateway runs agents with your local credentials, so only expose it with a strong `GATEWAY_API_KEY`.

### Cloudflare Tunnel

Quick tunnel (random hostname, no account needed):

```bash
cloudflared tunnel --url http://127.0.0.1:8765
```

Named tunnel (stable hostname):

```bash
cloudflared tunnel login
cloudflared tunnel create agent-proxy
cloudflared tunnel route dns agent-proxy agents.example.com
cat > ~/.cloudflared/config.yml <<'EOF'
tunnel: agent-proxy
credentials-file: /Users/you/.cloudflared/<TUNNEL_ID>.json
ingress:
  - hostname: agents.example.com
    service: http://127.0.0.1:8765
  - service: http_status:404
EOF
cloudflared tunnel run agent-proxy
```

You can also put Cloudflare Access (service tokens) in front of the hostname as a second auth layer.
If you set `ALLOWED_HOSTS`, include the tunnel hostname.

### ngrok

```bash
ngrok http 8765
```

### Configure the orchestrator

- **Streamable HTTP (preferred):** `https://<tunnel-host>/mcp` with header `Authorization: Bearer <GATEWAY_API_KEY>`
- **Legacy SSE:** `https://<tunnel-host>/sse` with the same bearer header on both the stream and message POSTs
- **Single agent:** `https://<tunnel-host>/agents/opencode/mcp`
- **ACP/REST:** `POST https://<tunnel-host>/acp/session/prompt`

Agent runs can take minutes. The gateway sends MCP progress notifications every 15 seconds while an
agent runs, so clients that reset their timeout on progress keep waiting. Set the orchestrator's
tool-call timeout above `SUBPROCESS_TIMEOUT_SECONDS`.

## Development

```bash
uv run pytest        # fake agent binaries, no model calls
uv run ruff check gateway tests
```

| File | Purpose |
| --- | --- |
| `gateway/config.py` | Settings, workspace allowlist, input validation |
| `gateway/adapters.py` | Subprocess runner, CLI and ACP adapters, `claude mcp serve` proxy, registry |
| `gateway/acp_client.py` | Minimal ACP JSON-RPC stdio client |
| `gateway/sessions.py` | Readers for each agent's native session store |
| `gateway/server.py` | MCP servers, routing, auth middleware, ACP HTTP bridge |
| `gateway/observability.py` | JSON logging and redaction |
