# AGENTS.md

Guidance for coding agents changing this repo. User-facing usage lives in README.md.

## Commands

```bash
uv run pytest -q                      # ~8s, uses fake agent binaries
uv run ruff format gateway tests
uv run ruff check gateway tests       # line length 120, bandit + async rules enabled
```

Run all three before you finish. Add dependencies with `uv add`, then regenerate the pip pins with
`uv export --no-hashes --no-dev --no-emit-project --format requirements-txt -o requirements.txt`.

## MCP SDK 2.x pitfalls

- The SDK is `mcp` 2.x. `FastMCP` does not exist. Use `mcp.server.mcpserver.MCPServer`. Don't pin `mcp<2`
  and don't copy v1 examples from the web without checking the installed source in `.venv`.
- `MCPServer.add_tool` derives schemas from Python signatures. Proxied tools with foreign JSON schemas
  are served by overriding the public `list_tools` / `call_tool` on `GatewayMCPServer`. Don't reach into
  `_tool_manager` or other private attributes.
- MCP routes are added at absolute paths via `mcp_routes()` instead of `Mount`. A `Mount` turns `/mcp`
  into a 307 redirect to `/mcp/`. Every `session_manager.run()` must be entered in the app lifespan.
- Leave `transport_security` explicit. The SDK default only allows a localhost `Host` header, which
  breaks tunnels.
- The HTTP client inside the SDK is `httpx2`, not `httpx`. Pass `httpx2.AsyncClient` to `streamable_http_client`.

## Security invariants (do not regress)

- Never use `shell=True` or string commands. Templates are `shlex.split` first, then placeholders are
  substituted per token in one regex pass (`render_args`). Keep it that way so prompts can't inject flags.
- Every caller-supplied path (`working_dir`, proxied `file_path`, ACP permission locations) goes through
  `Workspace`. New tools that accept paths must do the same.
- New authenticated routes must live under a prefix in `PROTECTED_PREFIXES`. Keep `BearerAuthMiddleware`
  pure ASGI. `BaseHTTPMiddleware` buffers responses and breaks SSE.
- Spawn children only through `spawn()` / `run_process()`. They scrub `GATEWAY_API_KEY`, start a new
  process group, and `terminate_process()` kills the group on timeout and cancellation.
- Never log prompt text or tool output. Log sizes, durations and exit codes. Pass new secrets to
  `configure_logging(secrets=...)`.
- A resumed session runs in its recorded cwd, so `AgentRegistry.resolve_session` must keep checking that
  cwd against `Workspace` (and against `working_dir` when given). Never trust a caller-supplied session id
  to pick the directory. Session ids go through `validate_session_id` before they reach argv.
- Session titles can contain prompt text. Return them to callers but never log them.
- Shell-capable proxied tools (`Bash`) stay out of the `CLAUDE_PROXY_TOOLS` default. Path checks can't sandbox them.

## Adding an agent

1. Add a member to `AgentName` in `gateway/config.py`.
2. Add `Settings` fields named exactly `<agent>_bin`, `_mode`, `_args`, `_resume_args`, `_new_session_args`,
   `_output_format`, `_model_args`, `_default_model`, `_acp_args`, `_version_args`, `_auth_check_args`. `Settings.agent()` looks them up with `getattr`,
   so a typo fails at runtime. Include `<agent>_default_model` in the empty-to-None validator.
3. Add a label in `_AGENT_LABELS` (`gateway/server.py`). Routes, tools and discovery iterate over
   `AgentName` and pick the agent up automatically.
4. Implement a `SessionStore` in `gateway/sessions.py` that reads the CLI's native session storage and
   register it in `AgentRegistry._make_session_stores`. Inspect real files or `--help` output to learn the
   format. If the CLI can't take a caller-chosen session id, pick an `OutputFormat` that exposes the id
   (add a branch to `parse_output` if needed) instead of guessing "newest session", which races.
5. Derive default flags from the installed CLI's `--help` and its docs, not from memory. Prefer
   stdin for prompts (no `{prompt}` in the template) when the CLI supports it.
6. Update `.env.example`, the endpoint, template and session tables in README.md, set `<agent>_bin` in the
   `make_settings` fixture, and add store fixtures to the `stores` fixture.

## Tests

- Tests must never call real models. Extend the fake scripts in `tests/conftest.py` (`FAKE_CLI`, `FAKE_ACP`)
  to simulate new behavior. The fake CLI switches on keywords in the prompt (`SLEEP`, `FAIL`, `BIG`) and
  mimics each agent's JSON output when it sees `--format json` (OpenCode) or `--json` (Codex).
- The `stores` fixture writes fake native session stores (one session inside the workspace, one outside)
  and points the session-store settings at them. Never read the real `~/.claude`, `~/.copilot` or `~/.codex`.
- Open the FastAPI lifespan inside the test body (see the `gateway` fixture). An async generator fixture
  tears down in a different task and anyio raises "exit cancel scope in a different task".
- Transport-level behavior (Streamable HTTP, SSE, proxy) is tested against a live uvicorn on a free port
  via the `live_url` fixture.
- For a manual check against real CLIs, start the gateway and send `"Reply with exactly: PONG"`. It is
  cheap and easy to assert on. Don't commit scripts that spend tokens.
