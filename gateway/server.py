"""FastAPI + MCP gateway: unified and per-agent MCP endpoints plus an ACP HTTP bridge."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import secrets
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from mcp import types
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError
from pydantic import BaseModel, Field, StrictBool, ValidationError
from starlette.routing import BaseRoute
from starlette.types import ASGIApp, Receive, Scope, Send

from gateway import __version__
from gateway.acp_client import PermissionApproval
from gateway.adapters import (
    AgentRegistry,
    AgentResult,
    AgentUnavailableError,
    ClaudeMcpProxy,
)
from gateway.config import AgentName, InputError, Settings, WorkspaceError, get_settings
from gateway.observability import configure_logging

logger = logging.getLogger("gateway.server")

PROTECTED_PREFIXES = ("/mcp", "/sse", "/messages", "/agents", "/acp")
_HEARTBEAT_SECONDS = 15.0
_STDERR_TAIL_CHARS = 4000
_APPROVAL_TIMEOUT_SECONDS = 60.0
_MAX_FOLLOWUP_TURNS = 16

PromptArg = Annotated[str, Field(description="Task or question for the agent.")]
WorkingDirArg = Annotated[
    str | None,
    Field(description="Directory to run in. Must be inside ALLOWED_WORKSPACES. Defaults to the first workspace."),
]
ModelArg = Annotated[
    str | None,
    Field(description="Optional model override, passed to the CLI's model flag."),
]
SessionIdArg = Annotated[
    str | None,
    Field(
        description=(
            "Continue an existing conversation: a session_id returned by a previous call or by list_sessions, "
            "or 'latest' for the newest session under working_dir. Omit to start a new session."
        )
    ),
]
LimitArg = Annotated[int, Field(description="Maximum sessions to return (1-200).", ge=1, le=200)]


# ---------------------------------------------------------------------------
# MCP servers
# ---------------------------------------------------------------------------


class GatewayMCPServer(MCPServer):
    """MCPServer that also re-exports tools from the supervised `claude mcp serve` proxy."""

    def __init__(self, *args: Any, proxy: ClaudeMcpProxy | None = None, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._proxy = proxy

    async def list_tools(self) -> list[types.Tool]:
        tools = await super().list_tools()
        return tools + self._proxy.tools if self._proxy else tools

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        context: Context[Any, Any] | None = None,
    ) -> Any:
        logger.info("mcp_tool_request", extra={"tool": name})
        if self._proxy and self._proxy.owns(name):
            return await self._proxy.call(name, arguments)
        return await super().call_tool(name, arguments, context)


_AGENT_LABELS = {
    AgentName.CLAUDE: "Claude Code (`claude -p`)",
    AgentName.COPILOT: "GitHub Copilot CLI (`copilot -p`)",
    AgentName.OPENCODE: "OpenCode (`opencode run` or ACP)",
    AgentName.CODEX: "OpenAI Codex CLI (`codex exec`)",
}


def format_failure(result: AgentResult) -> str:
    if result.timed_out:
        head = f"{result.agent} timed out after {result.duration_ms // 1000}s and was terminated."
    else:
        head = f"{result.agent} failed (exit code {result.exit_code}, stop reason {result.stop_reason})."
    parts = [head]
    if result.stderr.strip():
        parts.append("stderr:\n" + result.stderr[-_STDERR_TAIL_CHARS:])
    if result.output.strip():
        parts.append("output:\n" + result.output[-_STDERR_TAIL_CHARS:])
    return "\n\n".join(parts)


async def _heartbeat(ctx: Context[Any, Any], agent: AgentName) -> None:
    """Emit progress so clients that reset timeouts on progress keep waiting."""
    elapsed = 0.0
    while True:
        await asyncio.sleep(_HEARTBEAT_SECONDS)
        elapsed += _HEARTBEAT_SECONDS
        with contextlib.suppress(Exception):
            await ctx.report_progress(elapsed, None, f"{agent.value} running for {int(elapsed)}s")


async def run_agent_tool(
    registry: AgentRegistry,
    agent: AgentName,
    prompt: str,
    working_dir: str | None,
    model: str | None,
    session_id: str | None = None,
    ctx: Context[Any, Any] | None = None,
    *,
    approve_permission: PermissionApproval | None = None,
) -> types.CallToolResult:
    heartbeat = asyncio.create_task(_heartbeat(ctx, agent)) if ctx else None
    try:
        result = await registry.invoke(
            agent, prompt, working_dir, model, session_id, approve_permission=approve_permission
        )
    except (InputError, WorkspaceError, AgentUnavailableError) as exc:
        raise ToolError(str(exc)) from exc
    finally:
        if heartbeat:
            heartbeat.cancel()
    if not result.ok:
        raise ToolError(format_failure(result))
    output = result.output.strip()
    if result.truncated:
        output += f"\n\n[output truncated at {registry.settings.max_output_bytes} bytes]"
    content = [types.TextContent(type="text", text=output)]
    if result.session_id:
        # A separate block so text-only clients (LLM orchestrators) can see how to continue.
        hint = f"session_id: {result.session_id} (pass it as session_id to continue this conversation)"
        content.append(types.TextContent(type="text", text=hint))
    return types.CallToolResult(
        content=content,
        structured_content={
            "agent": result.agent,
            "session_id": result.session_id,
            "cwd": result.cwd,
            "output": output,
        },
    )


class PermissionForm(BaseModel):
    approve: Annotated[StrictBool, Field(description="Approve this action once.")]


def _supports_form_elicitation(ctx: Context[Any, Any]) -> bool:
    capabilities = ctx.client_capabilities
    elicitation = capabilities.elicitation if capabilities else None
    return elicitation is not None and (elicitation.form is not None or elicitation.url is None)


def _approval_message(agent: AgentName, title: str, paths: tuple[Path, ...]) -> str | None:
    message = f"{agent.value} requests permission: {title}"
    if paths:
        message += "\nReported paths:\n" + "\n".join(str(path) for path in paths)
    if len(message) > 4000:
        logger.warning("interactive_approval_details_too_long", extra={"agent": agent.value})
        return None
    return message


def _interactive_approval(ctx: Context[Any, Any], agent: AgentName) -> PermissionApproval:
    async def approve(_session_id: str, title: str, paths: tuple[Path, ...]) -> bool:
        message = _approval_message(agent, title, paths)
        if message is None:
            return False
        try:
            async with asyncio.timeout(_APPROVAL_TIMEOUT_SECONDS):
                result = await ctx.elicit(message, PermissionForm)
        except (TimeoutError, MCPError, ValidationError) as exc:
            logger.warning("interactive_approval_failed", extra={"agent": agent.value, "reason": type(exc).__name__})
            return False
        return result.action == "accept" and result.data is not None and result.data.approve is True

    return approve


class ApprovalReply(BaseModel):
    approval_id: Annotated[str, Field(min_length=1, max_length=64, description="One-time ID from the follow-up.")]
    approve: Annotated[StrictBool, Field(description="The user's explicit decision for this action.")]


@dataclass
class FollowupTurn:
    agent: AgentName
    task: asyncio.Task[types.CallToolResult]
    next_question: asyncio.Future[types.CallToolResult]


@dataclass(frozen=True)
class PendingApproval:
    turn: FollowupTurn
    session_id: str
    decision: asyncio.Future[bool]


class FollowupApprovals:
    """Keep an ACP turn alive between calls without keeping an MCP request open."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[types.CallToolResult]] = set()
        self._pending: dict[str, PendingApproval] = {}

    async def start(
        self,
        registry: AgentRegistry,
        agent: AgentName,
        prompt: str,
        working_dir: str | None,
        model: str | None,
        session_id: str | None,
        ctx: Context[Any, Any],
    ) -> types.CallToolResult:
        if len(self._tasks) >= _MAX_FOLLOWUP_TURNS:
            raise ToolError("Too many interactive turns are in progress. Try again shortly.")
        loop = asyncio.get_running_loop()

        async def approve(active_session: str, title: str, paths: tuple[Path, ...]) -> bool:
            message = _approval_message(agent, title, paths)
            if message is None:
                return False
            approval_id = secrets.token_urlsafe(24)
            decision: asyncio.Future[bool] = loop.create_future()
            self._pending[approval_id] = PendingApproval(turn, active_session, decision)
            instructions = (
                "Show the question to the user. Only after the user answers, call the same tool with "
                "the returned session_id and approval_id, and set approval.approve to their decision. "
                "Do not send a new prompt."
            )
            question = f"{message}\nAllow this action once?"
            turn.next_question.set_result(
                types.CallToolResult(
                    content=[
                        types.TextContent(
                            type="text",
                            text=(
                                f"{question}\n\n{instructions}\n"
                                f"session_id: {active_session}\napproval_id: {approval_id}"
                            ),
                        )
                    ],
                    structured_content={
                        "status": "approval_required",
                        "agent": agent.value,
                        "session_id": active_session,
                        "question": question,
                        "reply": {
                            "tool": f"ask_{agent.value}_interactive",
                            "session_id": active_session,
                            "approval": {"approval_id": approval_id},
                        },
                        "expires_in_seconds": _APPROVAL_TIMEOUT_SECONDS,
                        "instructions": instructions,
                    },
                )
            )
            try:
                async with asyncio.timeout(_APPROVAL_TIMEOUT_SECONDS):
                    return await decision
            except TimeoutError:
                logger.info("interactive_followup_expired", extra={"agent": agent.value})
                turn.task.cancel()
                return False
            finally:
                self._pending.pop(approval_id, None)

        task = asyncio.create_task(
            run_agent_tool(registry, agent, prompt, working_dir, model, session_id, approve_permission=approve)
        )
        turn = FollowupTurn(agent, task, loop.create_future())
        self._tasks.add(task)
        task.add_done_callback(self._finished)
        return await self._wait(turn, ctx)

    async def answer(
        self,
        agent: AgentName,
        session_id: str | None,
        approval: ApprovalReply,
        ctx: Context[Any, Any],
    ) -> types.CallToolResult:
        pending = self._pending.get(approval.approval_id)
        if (
            pending is None
            or pending.turn.agent != agent
            or pending.session_id != session_id
            or pending.decision.done()
            or pending.turn.task.done()
            or pending.turn.task.cancelling()
        ):
            raise ToolError("No matching pending approval. It may have expired or already been answered.")
        turn = pending.turn
        turn.next_question = asyncio.get_running_loop().create_future()
        pending.decision.set_result(approval.approve)
        return await self._wait(turn, ctx)

    async def _wait(self, turn: FollowupTurn, ctx: Context[Any, Any]) -> types.CallToolResult:
        heartbeat = asyncio.create_task(_heartbeat(ctx, turn.agent))
        try:
            await asyncio.wait((turn.task, turn.next_question), return_when=asyncio.FIRST_COMPLETED)
            if turn.task.cancelled():
                raise ToolError("The interactive turn expired or was cancelled.")
            if turn.task.done():
                return turn.task.result()
            return turn.next_question.result()
        except asyncio.CancelledError:
            if not turn.task.cancelling():
                turn.task.cancel()
            raise
        finally:
            heartbeat.cancel()

    def _finished(self, task: asyncio.Task[types.CallToolResult]) -> None:
        self._tasks.discard(task)
        if not task.cancelled():
            task.exception()

    async def stop(self) -> None:
        tasks = tuple(self._tasks)
        for task in tasks:
            if not task.cancelling():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _add_agent_tool(
    server: MCPServer,
    registry: AgentRegistry,
    agent: AgentName,
    *,
    followups: FollowupApprovals | None = None,
) -> None:
    async def ask(
        prompt: PromptArg,
        ctx: Context,
        working_dir: WorkingDirArg = None,
        model: ModelArg = None,
        session_id: SessionIdArg = None,
    ) -> types.CallToolResult:
        return await run_agent_tool(registry, agent, prompt, working_dir, model, session_id, ctx)

    if followups is not None:

        async def ask_interactive(
            ctx: Context,
            prompt: Annotated[str | None, Field(description="New prompt. Omit when answering an approval.")] = None,
            working_dir: WorkingDirArg = None,
            model: ModelArg = None,
            session_id: SessionIdArg = None,
            approval: ApprovalReply | None = None,
        ) -> types.CallToolResult:
            if approval is not None:
                if prompt is not None or working_dir is not None or model is not None or not session_id:
                    raise ToolError("To answer an approval, send only session_id and approval.")
                return await followups.answer(agent, session_id, approval, ctx)
            if prompt is None:
                raise ToolError("A prompt is required to start an interactive turn.")
            if not _supports_form_elicitation(ctx):
                return await followups.start(registry, agent, prompt, working_dir, model, session_id, ctx)
            return await run_agent_tool(
                registry,
                agent,
                prompt,
                working_dir,
                model,
                session_id,
                ctx,
                approve_permission=_interactive_approval(ctx, agent),
            )

        server.add_tool(
            ask_interactive,
            name=f"ask_{agent.value}_interactive",
            description=(
                f"Run {_AGENT_LABELS[agent]} when the task may need user approval (for example, shell commands "
                f"or edits), or when ask_{agent.value} reports a permission denial. Use this tool even if the "
                "client does not support MCP form elicitation. When the agent requests permission, the tool "
                "uses elicitation if supported or returns an approval_required result. Show the question to the "
                "user and wait for their decision. Then call this same tool with the returned session_id and "
                "approval (approval_id and approve), without a new prompt. Never approve on the user's behalf."
            ),
            structured_output=False,
        )
        return

    server.add_tool(
        ask,
        name=f"ask_{agent.value}",
        description=(
            f"Run {_AGENT_LABELS[agent]} headlessly from an allowed working directory and return its final answer "
            "plus a session_id. Use this for unattended tasks that do not need user approval. This tool does "
            f"not relay approval prompts. If the task may require approval or gets blocked by permissions, "
            f"use ask_{agent.value}_interactive instead. Pass session_id back to continue the same conversation."
        ),
        structured_output=False,
    )


def _add_sessions_tool(server: MCPServer, registry: AgentRegistry, fixed_agent: AgentName | None) -> None:
    description = (
        "List existing agent sessions recorded under working_dir (newest first) with id, title and timestamps. "
        "Pass an id as session_id to ask_<agent> to continue that conversation."
    )
    if fixed_agent:

        async def list_sessions(working_dir: WorkingDirArg = None, limit: LimitArg = 20) -> str:
            return await _sessions_json(registry, fixed_agent, working_dir, limit)

    else:

        async def list_sessions(  # type: ignore[misc]
            agent: Annotated[AgentName | None, Field(description="Only this agent. Omit for all agents.")] = None,
            working_dir: WorkingDirArg = None,
            limit: LimitArg = 20,
        ) -> str:
            return await _sessions_json(registry, agent, working_dir, limit)

    server.add_tool(list_sessions, description=description, structured_output=False)


async def _sessions_json(registry: AgentRegistry, agent: AgentName | None, working_dir: str | None, limit: int) -> str:
    try:
        sessions = await registry.list_sessions(agent, working_dir, limit)
    except WorkspaceError as exc:
        raise ToolError(str(exc)) from exc
    return json.dumps({"sessions": [s.to_dict() for s in sessions]}, indent=2)


def _add_discovery_tool(server: MCPServer, registry: AgentRegistry) -> None:
    async def list_available_agents(
        refresh: Annotated[bool, Field(description="Bypass the 60s discovery cache.")] = False,
    ) -> str:
        agents = await registry.list_available(refresh=refresh)
        return json.dumps({"agents": agents}, indent=2)

    server.add_tool(
        list_available_agents,
        description="Report which agent CLIs are installed on the host, their versions, and auth status.",
        structured_output=False,
    )


def build_mcp_server(
    name: str,
    registry: AgentRegistry,
    agents: Sequence[AgentName],
    followups: FollowupApprovals,
    *,
    include_proxy: bool,
    include_discovery: bool,
) -> GatewayMCPServer:
    server = GatewayMCPServer(
        name=name,
        version=__version__,
        instructions=(
            "Gateway to local coding agents. Use ask_<agent> tools. working_dir must be an allowed workspace. "
            "Use list_sessions and session_id to continue earlier conversations instead of starting new ones."
        ),
        proxy=registry.claude_proxy if include_proxy else None,
        log_level=registry.settings.log_level,
    )
    for agent in agents:
        _add_agent_tool(server, registry, agent)
        _add_agent_tool(server, registry, agent, followups=followups)
    _add_sessions_tool(server, registry, agents[0] if len(agents) == 1 else None)
    if include_discovery:
        _add_discovery_tool(server, registry)
    return server


@dataclass(frozen=True)
class McpMount:
    prefix: str
    server: GatewayMCPServer


def build_mounts(registry: AgentRegistry, followups: FollowupApprovals) -> list[McpMount]:
    mounts = [
        McpMount(
            "",
            build_mcp_server(
                "agent-proxy",
                registry,
                list(AgentName),
                followups,
                include_proxy=True,
                include_discovery=True,
            ),
        )
    ]
    for agent in AgentName:
        server = build_mcp_server(
            f"agent-proxy-{agent.value}",
            registry,
            [agent],
            followups,
            include_proxy=agent is AgentName.CLAUDE,
            include_discovery=False,
        )
        mounts.append(McpMount(f"/agents/{agent.value}", server))
    return mounts


def mcp_routes(mount: McpMount, security: TransportSecuritySettings) -> list[BaseRoute]:
    """Build Streamable HTTP + legacy SSE routes at absolute paths (no redirects)."""
    http_app = mount.server.streamable_http_app(streamable_http_path=f"{mount.prefix}/mcp", transport_security=security)
    sse_app = mount.server.sse_app(
        sse_path=f"{mount.prefix}/sse",
        message_path=f"{mount.prefix}/messages/",
        transport_security=security,
    )
    return [*http_app.routes, *sse_app.routes]


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def _is_protected(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in PROTECTED_PREFIXES)


class BearerAuthMiddleware:
    """Pure ASGI bearer-token check (keeps SSE streaming intact, unlike BaseHTTPMiddleware)."""

    def __init__(self, app: ASGIApp, api_key: str):
        self.app = app
        self._key = api_key.encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self._key or not _is_protected(scope["path"]):
            await self.app(scope, receive, send)
            return
        if self._authorized(scope):
            await self.app(scope, receive, send)
            return
        response = JSONResponse(
            {"error": "unauthorized", "detail": "Missing or invalid bearer token"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
        await response(scope, receive, send)

    def _authorized(self, scope: Scope) -> bool:
        token = b""
        for name, value in scope.get("headers", []):
            if name == b"authorization" and value[:7].lower() == b"bearer ":
                token = value[7:].strip()
                break
        if not token:
            query = parse_qs(scope.get("query_string", b"").decode(errors="replace"))
            token = (query.get("api_key") or [""])[0].encode()
        return bool(token) and hmac.compare_digest(token, self._key)


# ---------------------------------------------------------------------------
# ACP HTTP bridge
# ---------------------------------------------------------------------------


class TextBlock(BaseModel):
    type: Literal["text"] = "text"
    text: str


class AcpPromptRequest(BaseModel):
    agent: AgentName
    prompt: str | list[TextBlock]
    cwd: str | None = None
    model: str | None = None
    sessionId: str | None = None

    def prompt_text(self) -> str:
        if isinstance(self.prompt, str):
            return self.prompt
        return "\n".join(block.text for block in self.prompt)


class AcpPromptResponse(BaseModel):
    agent: str
    mode: str
    cwd: str
    sessionId: str | None
    stopReason: str
    content: list[TextBlock]
    ok: bool
    exitCode: int | None
    durationMs: int
    timedOut: bool
    truncated: bool
    stderr: str

    @classmethod
    def from_result(cls, result: AgentResult) -> AcpPromptResponse:
        if result.stop_reason:
            stop_reason = result.stop_reason
        elif result.timed_out:
            stop_reason = "cancelled"
        else:
            stop_reason = "end_turn" if result.ok else "error"
        return cls(
            agent=result.agent,
            mode=result.mode,
            cwd=result.cwd,
            sessionId=result.session_id,
            stopReason=stop_reason,
            content=[TextBlock(text=result.output)],
            ok=result.ok,
            exitCode=result.exit_code,
            durationMs=result.duration_ms,
            timedOut=result.timed_out,
            truncated=result.truncated,
            stderr=result.stderr[-_STDERR_TAIL_CHARS:],
        )


def _register_http_routes(app: FastAPI, registry: AgentRegistry) -> None:
    @app.get("/health", include_in_schema=False)
    async def health() -> dict[str, Any]:
        proxy = registry.claude_proxy
        return {
            "status": "ok",
            "version": __version__,
            "claude_mcp_proxy": None if proxy is None else {"tools": len(proxy.tools)},
        }

    @app.post("/acp/session/prompt", response_model=AcpPromptResponse)
    async def acp_prompt(body: AcpPromptRequest) -> JSONResponse:
        try:
            result = await registry.invoke(body.agent, body.prompt_text(), body.cwd, body.model, body.sessionId)
        except (InputError, WorkspaceError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except AgentUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        status = 200 if result.ok else 504 if result.timed_out else 502
        return JSONResponse(AcpPromptResponse.from_result(result).model_dump(), status_code=status)

    @app.get("/acp/sessions")
    async def acp_sessions(agent: AgentName | None = None, cwd: str | None = None, limit: int = 20) -> dict[str, Any]:
        try:
            sessions = await registry.list_sessions(agent, cwd, limit)
        except WorkspaceError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"sessions": [s.to_dict() for s in sessions]}

    @app.get("/acp/agents")
    async def acp_agents(refresh: bool = False) -> dict[str, Any]:
        return {"agents": await registry.list_available(refresh=refresh)}


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(settings: Settings | None = None, registry: AgentRegistry | None = None) -> FastAPI:
    settings = settings or get_settings()
    api_key = settings.gateway_api_key.get_secret_value()
    configure_logging(settings.log_level, secrets=[api_key])
    registry = registry or AgentRegistry(settings)
    followups = FollowupApprovals()
    mounts = build_mounts(registry, followups)
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=bool(settings.allowed_hosts),
        allowed_hosts=settings.allowed_hosts,
        allowed_origins=settings.allowed_origins,
    )

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if not api_key:
            logger.warning("auth_disabled", extra={"reason": "ALLOW_NO_AUTH=true"})
        logger.info(
            "gateway_starting",
            extra={
                "workspaces": [str(p) for p in registry.workspace.roots],
                "port": settings.port,
            },
        )
        async with contextlib.AsyncExitStack() as stack:
            for mount in mounts:
                await stack.enter_async_context(mount.server.session_manager.run())
            await registry.start()
            stack.push_async_callback(registry.stop)
            stack.push_async_callback(followups.stop)
            yield

    app = FastAPI(
        title="Agent Proxy",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.registry = registry
    _register_http_routes(app, registry)
    for mount in mounts:
        app.router.routes.extend(mcp_routes(mount, security))
    app.add_middleware(BearerAuthMiddleware, api_key=api_key)
    return app


_app: FastAPI | None = None


def __getattr__(name: str) -> Any:
    # Lazily build `app` so `uvicorn gateway.server:app` works without
    # importing settings (and requiring GATEWAY_API_KEY) at module import time.
    global _app
    if name == "app":
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(name)
