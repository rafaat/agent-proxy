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
| `/mcp` | Streamable HTTP | `ask_<agent>` for all four, `list_sessions`, `list_agent_capabilities`, `list_available_agents`, `claude_<Tool>` |
| `/sse` + `/messages/` | Legacy HTTP+SSE | Same as `/mcp` |
| `/agents/claude/mcp`, `/agents/claude/sse` | Streamable HTTP / SSE | `ask_claude`, `list_sessions`, `list_agent_capabilities` + `claude_<Tool>` |
| `/agents/copilot/mcp`, `/agents/copilot/sse` | Streamable HTTP / SSE | `ask_copilot`, `list_sessions`, `list_agent_capabilities` |
| `/agents/opencode/mcp`, `/agents/opencode/sse` | Streamable HTTP / SSE | `ask_opencode`, `list_sessions`, `list_agent_capabilities` |
| `/agents/codex/mcp`, `/agents/codex/sse` | Streamable HTTP / SSE | `ask_codex`, `list_sessions`, `list_agent_capabilities` |
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
`INTERACTIVE_APPROVAL_TIMEOUT_SECONDS` (default 900) does not approve the action. The agent timeout
(`SUBPROCESS_TIMEOUT_SECONDS`) is paused while an approval is pending. Requests that report a path
outside `ALLOWED_WORKSPACES` and `ACP_PERMISSION_EXTRA_PATHS` are denied without prompting, unless
`INTERACTIVE_APPROVE_OUTSIDE_PATHS=true`, in which case the question lists those paths under a warning
and the user decides. Paths are resolved through symlinks first, so a `.venv/bin/python` that links
outside the workspace counts as outside. Actions the agent already permits do not prompt, so configure
its permission rules accordingly.

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

### Agent capabilities

`list_agent_capabilities` reports what an agent CLI loads in `working_dir`, using only the CLI's own
metadata commands. Tools and skills are tracked separately in `coverage`:

- `native`: the CLI has a model-free listing and it answered. An empty list is a real, authoritative answer.
- `unavailable`: a native listing exists but timed out, failed or returned malformed output. The field is
  `null`. Raw probe output is never returned.
- `unknown`: no verified safe listing was run (Claude by default).
- `unsupported`: the CLI is verified to have no native listing for that category.

`ask_agent` (default `true`; `"auto"` is an alias) allows one billable model turn with
`CAPABILITIES_PROMPT`, only for categories marked `unsupported`. `false` (or `"never"`) never calls a
model. There is no way to force a model turn. Successful empty, timed-out, failed, malformed and
`unknown` results never trigger one. When it runs, `self_report` (`report` when parsed as JSON,
otherwise raw `output` on success only) and a `self_report_reason` such as
`cli_lacks_native_listing_for_tools` are returned, with a `session_id`.

Probes are fixed commands that read configuration only. They do not run discovered tools. Only
allowlisted fields are returned, so MCP env vars, headers and skill bodies never leave the host.
MCP servers include an `allow_rule` for `<AGENT>_ALLOWED_TOOLS` where available.

Claude has no documented metadata-only listing. `claude mcp list` health-checks connectors, and a
`claude -p /cost` init event relies on undocumented print behavior and was seen starting a plugin
connector even with `--strict-mcp-config`. So `CLAUDE_CAPABILITIES_ARGS` is empty by default and Claude
coverage is `unknown` (no fallback). Set it only to a command you verified; its stream-json init event is parsed.

| Agent | Tools | Skills |
| --- | --- | --- |
| Claude | unknown (no verified safe listing; opt-in via `CLAUDE_CAPABILITIES_ARGS`) | unknown |
| Copilot | unsupported (`copilot mcp list --json` gives servers and tool filters only) | `copilot skill list --json`: name, description, source |
| Codex | unsupported (`codex mcp list --json` gives servers only) | unsupported |
| OpenCode | unsupported | `opencode debug skill`: name, description |

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

### Run as a background service (macOS)

`scripts/gatewayctl` installs a per-user launchd LaunchAgent. The gateway starts at login and restarts
if it crashes. It runs from the repo directory, so it reads `.env` from there.

```bash
scripts/gatewayctl install     # write ~/Library/LaunchAgents/com.agent-proxy.gateway.plist and start
scripts/gatewayctl status      # start | stop | restart | status | logs | uninstall
```

`install` saves your current shell `PATH` into the plist so launchd can find `claude`, `copilot`, `codex`
and `opencode`. If you move those CLIs (for example after an nvm upgrade), run `install` again. Logs go to
`~/Library/Logs/agent-proxy/`. `stop` keeps the gateway down until the next `start` or login. The
service runs `uv run --no-sync`, so run `uv sync` and then `restart` after you change dependencies.

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

### Tool permissions

In headless mode, agents deny every tool that has no allow rule. The default templates only approve
file edits in `cwd` (`--permission-mode acceptEdits` for Claude, `--allow-tool write` for Copilot), so
MCP tools such as Slack and shell commands are refused. You can widen this in two ways. Both apply
to new and resumed runs in CLI mode. ACP mode and the `ask_<agent>_interactive` tools ignore them.

| Setting | Effect | Claude flag | Copilot flag |
| --- | --- | --- | --- |
| `<AGENT>_ALLOWED_TOOLS` | Granular: approve only the listed rules (comma-separated) | `--allowedTools={tool}` per rule | `--allow-tool={tool}` per rule |
| `<AGENT>_ALLOW_ALL_TOOLS=true` | Approve every tool, including shell and file writes | `--dangerously-skip-permissions` | `--allow-all-tools` |

The flag each setting adds comes from `<AGENT>_ALLOWED_TOOLS_ARGS` and `<AGENT>_ALLOW_ALL_TOOLS_ARGS`,
so you can change it in `.env` (for example `COPILOT_ALLOW_ALL_TOOLS_ARGS=--allow-all` to lift
Copilot's `cwd` path check as well). OpenCode and Codex have no matching flags, so their templates
are empty and enabling either setting for them is a startup error.

**Granular rules** are passed to the CLI unchanged, so the syntax and wildcards are the CLI's own:

- Claude: `mcp__<server>__<tool>`, `mcp__<server>` for a whole server, or built-ins like `Bash(git diff *)`.
  `*` matches only in the tool part (`mcp__plugin_slack_slack__*`, `mcp__plugin_slack_slack__slack_read_*`).
  A `*` in the server name (`mcp__plugin_slack_*`) is ignored by Claude, and the gateway logs
  `allow_tool_server_glob_unsupported` at startup.
- Copilot: `<server>`, `<server>(<tool>)`, `shell(git:*)`, `write`. Copilot already runs read-only MCP tools
  without a rule.
- Rules starting with `-` are rejected. Use `list_agent_capabilities` to find server names, and copy its
  `allow_rule` values.

```bash
CLAUDE_ALLOWED_TOOLS=mcp__plugin_slack_slack__slack_read_*,mcp__plugin_slack_slack__slack_search_*
COPILOT_ALLOWED_TOOLS=slack(slack_read_channel),glean-adobe(outlook_search)
```

**Allow all** is off by default. With it on, any client holding `GATEWAY_API_KEY` can make the agent run
arbitrary commands as your user, so the gateway logs `allow_all_tools_enabled` at startup. Prefer
granular rules when the gateway is reachable through a tunnel.

```bash
CLAUDE_ALLOW_ALL_TOOLS=true
COPILOT_ALLOW_ALL_TOOLS=true
```

### ACP mode

Set `OPENCODE_MODE=acp` or `COPILOT_MODE=acp` to drive that agent through the Agent Client Protocol
(JSON-RPC over stdio) instead of its one-shot CLI. The gateway runs `initialize`, `session/new`,
an optional model selection, and `session/prompt`, collects the streamed `agent_message_chunk`
updates, and answers `session/request_permission` using `ACP_PERMISSION_POLICY`. A permission request
that touches a path outside `ALLOWED_WORKSPACES` is always rejected, unless the path is under one of the
comma-separated `ACP_PERMISSION_EXTRA_PATHS` (for example `~/.agents,~/.claude,~/.copilot`). Those extra
paths never become valid `working_dir` or session directories. Claude Code and Codex have no
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
- **Tool permissions.** Agents only get the tools that their templates, `<AGENT>_ALLOWED_TOOLS`, or
  `<AGENT>_ALLOW_ALL_TOOLS` approve (see [Tool permissions](#tool-permissions)). Allow-all gives every
  API-key holder shell access as your user.
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
