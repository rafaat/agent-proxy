"""Agent adapters: headless CLI runners, ACP runner, and the `claude mcp serve` proxy."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shlex
import shutil
import signal
import time
import uuid
from collections.abc import AsyncIterator, Hashable
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Protocol

import anyio
from mcp import types
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from gateway.acp_client import AcpError, AcpStdioClient, PermissionApproval
from gateway.config import (
    AgentConfig,
    AgentName,
    InputError,
    OutputFormat,
    Settings,
    Workspace,
    WorkspaceError,
    validate_model,
    validate_prompt,
    validate_session_id,
)
from gateway.sessions import (
    LATEST,
    ClaudeSessionStore,
    CodexSessionStore,
    CopilotSessionStore,
    OpenCodeSessionStore,
    SessionInfo,
    SessionStore,
)

logger = logging.getLogger("gateway.agents")

_PLACEHOLDER = re.compile(r"\{(prompt|cwd|model|session_id)\}")
_CHUNK = 64 * 1024
_KILL_GRACE_SECONDS = 5.0
_SCRUBBED_ENV = ("GATEWAY_API_KEY",)


class AgentUnavailableError(RuntimeError):
    """The agent binary is missing or the agent is disabled."""


@dataclass(slots=True)
class AgentResult:
    agent: str
    mode: str
    cwd: str
    output: str
    stderr: str
    exit_code: int | None
    duration_ms: int
    timed_out: bool = False
    truncated: bool = False
    stop_reason: str | None = None
    session_id: str | None = None

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"ok": self.ok}


@dataclass(slots=True)
class ProcessOutcome:
    stdout: bytes
    stderr: bytes
    exit_code: int | None
    timed_out: bool
    truncated: bool


@dataclass(frozen=True, slots=True)
class SessionTarget:
    """Which conversation a run belongs to. `id` is None when the CLI picks it."""

    id: str | None = None
    resume: bool = False


# ---------------------------------------------------------------------------
# Command templates
# ---------------------------------------------------------------------------


def render_args(
    template: str, *, prompt: str = "", cwd: Path | None = None, model: str = "", session_id: str = ""
) -> tuple[list[str], bool]:
    """Expand a shlex template into argv. Returns (argv, prompt_was_used).

    Substitution happens after splitting and in a single pass, so prompt text
    always stays one argv element and can never inject flags or placeholders.
    """
    values = {"prompt": prompt, "cwd": str(cwd or ""), "model": model, "session_id": session_id}
    used_prompt = False
    argv: list[str] = []
    for part in shlex.split(template):
        if "{prompt}" in part:
            used_prompt = True
        rendered = _PLACEHOLDER.sub(lambda m: values[m.group(1)], part)
        # A bare positional prompt starting with "-" would be parsed as a flag.
        if part == "{prompt}" and rendered.startswith("-"):
            rendered = " " + rendered
        argv.append(rendered)
    return argv, used_prompt


def build_cli_command(
    cfg: AgentConfig, prompt: str, cwd: Path, model: str | None, session: SessionTarget | None = None
) -> tuple[list[str], bytes | None]:
    session = session or SessionTarget()
    if session.resume:
        if not cfg.resume_args.strip():
            raise InputError(f"{cfg.name.value} does not support resuming sessions ({cfg.name.upper()}_RESUME_ARGS)")
        template = cfg.resume_args
    else:
        template = cfg.args
        if session.id and cfg.new_session_args.strip():
            template = f"{template} {cfg.new_session_args}"
    args, used_prompt = render_args(template, prompt=prompt, cwd=cwd, session_id=session.id or "")
    if model:
        model_args, _ = render_args(cfg.model_args, model=model, cwd=cwd)
        args += model_args
    stdin = None if used_prompt else prompt.encode()
    return [cfg.bin, *args], stdin


def parse_output(fmt: OutputFormat, stdout: str) -> tuple[str, str | None, list[str]]:
    """Extract (answer text, session id, error messages) from CLI stdout."""
    if fmt == "text":
        return stdout, None, []
    texts: list[str] = []
    errors: list[str] = []
    session_id: str | None = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind in ("error", "turn.failed"):
            error = event.get("error")
            message = event.get("message") or (error.get("message") if isinstance(error, dict) else error)
            errors.append(str(message or event))
        elif fmt == "opencode_json":
            session_id = session_id or event.get("sessionID")
            part = event.get("part") or {}
            if kind == "text" and part.get("text"):
                texts.append(part["text"])
        else:
            item = event.get("item") or {}
            if kind == "thread.started":
                session_id = event.get("thread_id")
            elif kind == "item.completed" and item.get("type") == "agent_message":
                # Codex prints only its final message in text mode, so keep the last one.
                texts = [item.get("text", "")]
    return "\n".join(texts), session_id, errors


def child_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _SCRUBBED_ENV}
    env.setdefault("NO_COLOR", "1")
    env.setdefault("TERM", "dumb")
    return env


# ---------------------------------------------------------------------------
# Subprocess execution
# ---------------------------------------------------------------------------


async def _read_capped(stream: asyncio.StreamReader | None, limit: int) -> tuple[bytes, bool]:
    """Read a stream to EOF, keeping at most `limit` bytes but draining the rest."""
    if stream is None:
        return b"", False
    buf = bytearray()
    truncated = False
    while chunk := await stream.read(_CHUNK):
        room = limit - len(buf)
        if room > 0:
            buf += chunk[:room]
        if len(chunk) > room:
            truncated = True
    return bytes(buf), truncated


async def _feed_stdin(proc: asyncio.subprocess.Process, data: bytes | None) -> None:
    if proc.stdin is None:
        return
    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
        if data:
            proc.stdin.write(data)
            await proc.stdin.drain()
        proc.stdin.close()


def _signal_group(proc: asyncio.subprocess.Process, sig: int) -> bool:
    try:
        if os.name == "posix":
            os.killpg(proc.pid, sig)
        elif proc.returncode is not None:
            return False
        elif sig == signal.SIGTERM:
            proc.terminate()
        else:
            proc.kill()
    except ProcessLookupError:
        return False
    except PermissionError:
        logger.warning("process_signal_denied", extra={"pid": proc.pid, "signal": sig})
        return False
    return True


async def terminate_process(proc: asyncio.subprocess.Process, grace: float = _KILL_GRACE_SECONDS) -> None:
    """SIGTERM the whole process group, then SIGKILL after a grace period."""
    if _signal_group(proc, signal.SIGTERM):
        try:
            async with asyncio.timeout(grace):
                if os.name == "posix":
                    # The leader's exit cannot notify us that its descendants have exited.
                    while _signal_group(proc, 0):  # noqa: ASYNC110
                        await asyncio.sleep(0.05)
                else:
                    await proc.wait()
        except TimeoutError:
            _signal_group(proc, getattr(signal, "SIGKILL", 9))
    if proc.returncode is None:
        try:
            await asyncio.wait_for(proc.wait(), grace)
        except TimeoutError:
            logger.warning("process_cleanup_timeout", extra={"pid": proc.pid})


async def spawn(argv: list[str], cwd: Path, *, stdin: bool, limit: int = 2**16) -> asyncio.subprocess.Process:
    try:
        return await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            stdin=asyncio.subprocess.PIPE if stdin else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=child_env(),
            start_new_session=os.name == "posix",
            limit=limit,
        )
    except FileNotFoundError as exc:
        raise AgentUnavailableError(f"Executable not found: {argv[0]}") from exc


async def run_process(
    argv: list[str],
    cwd: Path,
    *,
    stdin: bytes | None = None,
    timeout_seconds: float,
    max_output_bytes: int,
) -> ProcessOutcome:
    proc = await spawn(argv, cwd, stdin=stdin is not None)
    timed_out = False
    try:
        async with asyncio.timeout(timeout_seconds):
            (stdout, out_trunc), (stderr, err_trunc), _, _ = await asyncio.gather(
                _read_capped(proc.stdout, max_output_bytes),
                _read_capped(proc.stderr, max_output_bytes),
                _feed_stdin(proc, stdin),
                proc.wait(),
            )
    except TimeoutError:
        timed_out = True
        stdout, stderr, out_trunc, err_trunc = b"", b"", False, False
    finally:
        # Also covers cancellation (client disconnect): never leak a child.
        await terminate_process(proc)
    return ProcessOutcome(
        stdout,
        stderr,
        None if timed_out else proc.returncode,
        timed_out,
        out_trunc or err_trunc,
    )


# ---------------------------------------------------------------------------
# Agent adapters
# ---------------------------------------------------------------------------


class AgentAdapter(Protocol):
    config: AgentConfig

    async def run(self, prompt: str, cwd: Path, model: str | None, session: SessionTarget) -> AgentResult: ...


class CliAdapter:
    """Runs a CLI agent in headless mode (`claude -p`, `copilot -p`, `opencode run`, `codex exec`)."""

    def __init__(self, config: AgentConfig, settings: Settings):
        self.config = config
        self._settings = settings

    async def run(self, prompt: str, cwd: Path, model: str | None, session: SessionTarget) -> AgentResult:
        argv, stdin = build_cli_command(self.config, prompt, cwd, model, session)
        started = time.monotonic()
        outcome = await run_process(
            argv,
            cwd,
            stdin=stdin,
            timeout_seconds=self._settings.subprocess_timeout_seconds,
            max_output_bytes=self._settings.max_output_bytes,
        )
        output, parsed_id, errors = parse_output(self.config.output_format, outcome.stdout.decode(errors="replace"))
        return AgentResult(
            agent=self.config.name.value,
            mode="cli",
            cwd=str(cwd),
            output=output,
            stderr="\n".join([*errors, outcome.stderr.decode(errors="replace")]).strip(),
            exit_code=outcome.exit_code,
            duration_ms=int((time.monotonic() - started) * 1000),
            timed_out=outcome.timed_out,
            truncated=outcome.truncated,
            session_id=parsed_id or session.id,
        )


class AcpAdapter:
    """Runs one prompt turn against an ACP agent (`opencode acp`, `copilot --acp`, or an adapter binary)."""

    def __init__(
        self,
        config: AgentConfig,
        settings: Settings,
        workspace: Workspace,
        approve_permission: PermissionApproval | None = None,
    ):
        self.config = config
        self._settings = settings
        self._workspace = workspace
        self._approve_permission = approve_permission

    async def run(self, prompt: str, cwd: Path, model: str | None, session: SessionTarget) -> AgentResult:
        args, _ = render_args(self.config.acp_args, cwd=cwd)
        started = time.monotonic()
        proc = await spawn(
            [self.config.bin, *args],
            cwd,
            stdin=True,
            limit=AcpStdioClient.stream_limit(),
        )
        stderr_task = asyncio.create_task(_read_capped(proc.stderr, 64 * 1024))
        timed_out, exit_code, error = False, 0, ""
        client = AcpStdioClient(
            proc,
            permission_policy=self._settings.acp_permission_policy,
            resolve_path=lambda path: self._workspace.resolve_path(path, base=cwd),
            max_output_bytes=self._settings.max_output_bytes,
            approve_permission=self._approve_permission,
        )
        turn = None
        try:
            async with client:
                try:
                    async with asyncio.timeout(self._settings.subprocess_timeout_seconds):
                        turn = await client.run_turn(cwd, prompt, model, session.id if session.resume else None)
                except TimeoutError:
                    timed_out, exit_code = True, None
                except AcpError as exc:
                    exit_code, error = 1, str(exc)
        finally:
            try:
                await terminate_process(proc)
                stderr, _ = await asyncio.wait_for(stderr_task, _KILL_GRACE_SECONDS)
            except TimeoutError:
                stderr = b""
                logger.warning("acp_stderr_cleanup_timeout", extra={"pid": proc.pid})
            finally:
                stderr_task.cancel()
        stop_reason = turn.stop_reason if turn else ("cancelled" if timed_out else "error")
        if stop_reason == "refusal" and exit_code == 0:
            exit_code = 1
        return AgentResult(
            agent=self.config.name.value,
            mode="acp",
            cwd=str(cwd),
            output=turn.output if turn else "",
            stderr="\n".join(filter(None, [error, stderr.decode(errors="replace")])),
            exit_code=exit_code,
            duration_ms=int((time.monotonic() - started) * 1000),
            timed_out=timed_out,
            truncated=client.truncated,
            stop_reason=stop_reason,
            session_id=client.session_id,
        )


# ---------------------------------------------------------------------------
# Claude `mcp serve` proxy
# ---------------------------------------------------------------------------

# Argument names that Claude's file tools treat as filesystem paths.
_PATH_ARGS = frozenset({"file_path", "notebook_path", "path"})
_TRANSPORT_ERRORS = (
    anyio.ClosedResourceError,
    anyio.BrokenResourceError,
    anyio.EndOfStream,
    OSError,
)
_STARTUP_TIMEOUT_SECONDS = 30.0


class ClaudeMcpProxy:
    """Keeps a supervised `claude mcp serve` child alive and re-exports its tools."""

    def __init__(self, settings: Settings, workspace: Workspace, semaphore: asyncio.Semaphore):
        self._settings = settings
        self._workspace = workspace
        self._semaphore = semaphore
        self._prefix = settings.claude_proxy_tool_prefix
        allow = settings.claude_proxy_tools
        self._allow_all = "*" in allow
        self._allow = set(allow)
        self._tools: dict[str, types.Tool] = {}
        self._session: ClientSession | None = None
        self._ready = asyncio.Event()
        self._broken = asyncio.Event()
        self._stopping = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def tools(self) -> list[types.Tool]:
        return list(self._tools.values())

    def owns(self, name: str) -> bool:
        return name in self._tools

    async def start(self) -> None:
        self._task = asyncio.create_task(self._supervise(), name="claude-mcp-proxy")
        try:
            await asyncio.wait_for(self._ready.wait(), _STARTUP_TIMEOUT_SECONDS)
        except TimeoutError:
            logger.warning("claude_proxy_not_ready", extra={"timeout_s": _STARTUP_TIMEOUT_SECONDS})

    async def stop(self) -> None:
        self._stopping.set()
        if self._task:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._task, 10)

    async def call(self, name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        upstream = self._tools[name].name.removeprefix(self._prefix)
        try:
            arguments = self._sandbox_arguments(arguments)
        except WorkspaceError as exc:
            return _error_result(str(exc))
        session = self._session
        if session is None:
            return _error_result("claude mcp serve is not connected")
        started = time.monotonic()
        is_error = True
        try:
            async with self._semaphore:
                result = await session.call_tool(
                    upstream,
                    arguments,
                    read_timeout_seconds=self._settings.subprocess_timeout_seconds,
                )
            is_error = bool(result.is_error)
            return result
        except _TRANSPORT_ERRORS as exc:
            self._broken.set()
            return _error_result(f"claude mcp serve connection lost: {exc}")
        finally:
            logger.info(
                "tool_call",
                extra={
                    "agent": "claude",
                    "mode": "mcp_proxy",
                    "tool": upstream,
                    "duration_ms": int((time.monotonic() - started) * 1000),
                    "is_error": is_error,
                },
            )

    def _sandbox_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        sandboxed = dict(arguments)
        for key in _PATH_ARGS & sandboxed.keys():
            value = sandboxed[key]
            if isinstance(value, str) and value:
                sandboxed[key] = str(self._workspace.resolve_path(value))
        return sandboxed

    def _wanted(self, tool: types.Tool) -> bool:
        return self._allow_all or tool.name in self._allow

    async def _supervise(self) -> None:
        cfg = self._settings.agent(AgentName.CLAUDE)
        args, _ = render_args(self._settings.claude_mcp_serve_args, cwd=self._workspace.default)
        params = StdioServerParameters(command=cfg.bin, args=args, cwd=self._workspace.default, env=child_env())
        backoff = 1.0
        while not self._stopping.is_set():
            try:
                async with (
                    stdio_client(params) as (read, write),
                    ClientSession(read, write) as session,
                ):
                    await session.initialize()
                    listed = await session.list_tools()
                    self._tools = {
                        f"{self._prefix}{t.name}": t.model_copy(update={"name": f"{self._prefix}{t.name}"})
                        for t in listed.tools
                        if self._wanted(t)
                    }
                    self._session = session
                    self._broken.clear()
                    self._ready.set()
                    backoff = 1.0
                    logger.info("claude_proxy_connected", extra={"tools": sorted(self._tools)})
                    if "Bash" in {t.name.removeprefix(self._prefix) for t in self._tools.values()}:
                        logger.warning(
                            "claude_proxy_bash_enabled",
                            extra={"cwd": str(self._workspace.default)},
                        )
                    await _wait_any(self._stopping, self._broken)
            except Exception as exc:
                logger.warning("claude_proxy_error", extra={"error": repr(exc)})
            finally:
                self._session = None
            if self._stopping.is_set():
                break
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), backoff)
            backoff = min(backoff * 2, 60.0)


async def _wait_any(*events: asyncio.Event) -> None:
    waiters = [asyncio.create_task(e.wait()) for e in events]
    try:
        await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for w in waiters:
            w.cancel()


def _error_result(message: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=message)], is_error=True)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_DISCOVERY_TTL_SECONDS = 60.0
_DISCOVERY_TIMEOUT_SECONDS = 20.0
_MAX_SESSION_LIMIT = 200


class KeyedLocks:
    """One asyncio.Lock per key, dropped once nobody holds or waits for it."""

    def __init__(self) -> None:
        self._locks: dict[Hashable, tuple[asyncio.Lock, int]] = {}

    @contextlib.asynccontextmanager
    async def hold(self, key: Hashable) -> AsyncIterator[None]:
        lock, users = self._locks.get(key, (asyncio.Lock(), 0))
        self._locks[key] = (lock, users + 1)
        try:
            async with lock:
                yield
        finally:
            lock, users = self._locks[key]
            if users == 1:
                del self._locks[key]
            else:
                self._locks[key] = (lock, users - 1)


class AgentRegistry:
    """Owns adapters, per-agent concurrency limits, the claude proxy, and discovery."""

    def __init__(self, settings: Settings, workspace: Workspace | None = None):
        self.settings = settings
        self.workspace = workspace or Workspace(settings.workspace_roots())
        self.semaphores = {name: asyncio.Semaphore(settings.max_concurrency_per_agent) for name in AgentName}
        self.adapters: dict[AgentName, AgentAdapter] = {name: self._make_adapter(name) for name in AgentName}
        self.claude_proxy = (
            ClaudeMcpProxy(settings, self.workspace, self.semaphores[AgentName.CLAUDE])
            if settings.claude_mcp_proxy_enabled
            else None
        )
        self.sessions = self._make_session_stores()
        self._session_locks = KeyedLocks()
        self._discovery: tuple[float, list[dict[str, Any]]] | None = None

    def _make_session_stores(self) -> dict[AgentName, SessionStore]:
        s = self.settings

        def home(path: str) -> Path:
            return Path(path).expanduser()

        return {
            AgentName.CLAUDE: ClaudeSessionStore(home(s.claude_projects_dir)),
            AgentName.COPILOT: CopilotSessionStore(home(s.copilot_session_state_dir)),
            AgentName.OPENCODE: OpenCodeSessionStore(self._opencode_session_list),
            AgentName.CODEX: CodexSessionStore(home(s.codex_sessions_dir), home(s.codex_session_index)),
        }

    async def _opencode_session_list(self) -> bytes | None:
        outcome = await self._run_check(
            self.settings.opencode_bin, self.settings.opencode_session_list_args, max_output_bytes=8_000_000
        )
        return outcome.stdout if outcome and outcome.exit_code == 0 else None

    def _make_adapter(self, name: AgentName) -> AgentAdapter:
        cfg = self.settings.agent(name)
        if cfg.mode == "acp":
            return AcpAdapter(cfg, self.settings, self.workspace)
        return CliAdapter(cfg, self.settings)

    async def start(self) -> None:
        if self.claude_proxy and shutil.which(self.settings.claude_bin):
            await self.claude_proxy.start()

    async def stop(self) -> None:
        if self.claude_proxy:
            await self.claude_proxy.stop()

    async def invoke(
        self,
        agent: AgentName,
        prompt: str,
        working_dir: str | None,
        model: str | None,
        session_id: str | None = None,
        *,
        approve_permission: PermissionApproval | None = None,
    ) -> AgentResult:
        """Validate inputs, enforce limits, run the agent, and log the outcome.

        With `session_id` (or "latest") the agent continues that conversation in the
        session's own directory. Otherwise a new session starts in `working_dir`.
        """
        adapter = self.adapters[agent]
        if approve_permission is not None:
            interactive_bin = {
                AgentName.CLAUDE: self.settings.claude_interactive_bin,
                AgentName.CODEX: self.settings.codex_interactive_bin,
            }.get(agent, adapter.config.bin)
            cfg = replace(adapter.config, bin=interactive_bin, mode="acp")
            adapter = AcpAdapter(cfg, self.settings, self.workspace, approve_permission)
        prompt = validate_prompt(prompt, self.settings.max_prompt_chars)
        model = validate_model(model) or adapter.config.default_model
        session_id = validate_session_id(session_id)
        if not shutil.which(adapter.config.bin):
            role = "ACP executable" if approve_permission is not None else "binary"
            raise AgentUnavailableError(f"{agent.value} is not installed ({role} {adapter.config.bin!r} not on PATH)")

        if session_id:
            cwd, info = await self.resolve_session(agent, session_id, working_dir)
            target = SessionTarget(info.id, resume=True)
        else:
            cwd = self.workspace.resolve_dir(working_dir)
            preassign = adapter.config.mode == "cli" and bool(adapter.config.new_session_args.strip())
            target = SessionTarget(str(uuid.uuid4()) if preassign else None)

        log_ctx = {
            "agent": agent.value,
            "mode": adapter.config.mode,
            "cwd": str(cwd),
            "model": model,
            "resume": target.resume,
        }
        logger.info("tool_call_start", extra=log_ctx | {"prompt_chars": len(prompt), "session_id": target.id})
        # Two turns must never run concurrently inside the same conversation.
        session_lock = self._session_locks.hold((agent, target.id)) if target.resume else contextlib.nullcontext()
        async with session_lock, self.semaphores[agent]:
            result = await adapter.run(prompt, cwd, model, target)
        logger.info(
            "tool_call",
            extra=log_ctx
            | {
                "duration_ms": result.duration_ms,
                "exit_code": result.exit_code,
                "timed_out": result.timed_out,
                "truncated": result.truncated,
                "stop_reason": result.stop_reason,
                "session_id": result.session_id,
            },
        )
        return result

    async def list_sessions(
        self, agent: AgentName | None, working_dir: str | None, limit: int = 20
    ) -> list[SessionInfo]:
        """Sessions recorded at or below `working_dir`, newest first, across one or all agents."""
        cwd = self.workspace.resolve_dir(working_dir)
        limit = max(1, min(limit, _MAX_SESSION_LIMIT))
        agents = [agent] if agent else list(AgentName)
        found = await asyncio.gather(*(self.sessions[a].list(cwd, limit) for a in agents))
        merged = [s for group in found for s in group]
        return sorted(merged, key=lambda s: s.updated_at or "", reverse=True)[:limit]

    async def resolve_session(
        self, agent: AgentName, session_id: str, working_dir: str | None
    ) -> tuple[Path, SessionInfo]:
        """Find a session and confirm its recorded cwd is inside the sandbox (and `working_dir`)."""
        if session_id == LATEST:
            scope = self.workspace.resolve_dir(working_dir)
            latest = await self.sessions[agent].list(scope, 1)
            if not latest:
                raise InputError(f"No {agent.value} sessions found under {scope}")
            info = latest[0]
        else:
            found = await self.sessions[agent].get(session_id)
            if found is None:
                raise InputError(f"Unknown {agent.value} session: {session_id}")
            info = found
        cwd = self.workspace.resolve_dir(info.cwd)
        if working_dir:
            requested = self.workspace.resolve_dir(working_dir)
            if not cwd.is_relative_to(requested):
                raise WorkspaceError(f"Session {info.id} belongs to {cwd}, not {requested}")
        return cwd, info

    async def list_available(self, refresh: bool = False) -> list[dict[str, Any]]:
        now = time.monotonic()
        if not refresh and self._discovery and now - self._discovery[0] < _DISCOVERY_TTL_SECONDS:
            return self._discovery[1]
        results = list(await asyncio.gather(*(self._probe(name) for name in AgentName)))
        self._discovery = (now, results)
        return results

    async def _probe(self, name: AgentName) -> dict[str, Any]:
        cfg = self.settings.agent(name)
        path = shutil.which(cfg.bin)
        info: dict[str, Any] = {
            "agent": name.value,
            "mode": cfg.mode,
            "binary": cfg.bin,
            "installed": path is not None,
            "path": path,
            "version": None,
            "authenticated": None,
        }
        if name is AgentName.CLAUDE and self.claude_proxy:
            info["mcp_proxy_tools"] = [t.name for t in self.claude_proxy.tools]
        if not path:
            return info
        version, auth = await asyncio.gather(
            self._run_check(cfg.bin, cfg.version_args),
            self._run_check(cfg.bin, cfg.auth_check_args),
        )
        if version and version.exit_code == 0:
            lines = version.stdout.decode(errors="replace").strip().splitlines()
            info["version"] = lines[0] if lines else None
        info["authenticated"] = _auth_state(auth)
        return info

    async def _run_check(self, binary: str, template: str, max_output_bytes: int = 64 * 1024) -> ProcessOutcome | None:
        if not template.strip() or not shutil.which(binary):
            return None
        args, _ = render_args(template, cwd=self.workspace.default)
        try:
            return await run_process(
                [binary, *args],
                self.workspace.default,
                timeout_seconds=_DISCOVERY_TIMEOUT_SECONDS,
                max_output_bytes=max_output_bytes,
            )
        except AgentUnavailableError:
            return None


def _auth_state(outcome: ProcessOutcome | None) -> bool | None:
    if outcome is None or outcome.timed_out:
        return None
    if outcome.exit_code != 0:
        return False
    with contextlib.suppress(ValueError):
        data = json.loads(outcome.stdout)
        if isinstance(data, dict) and "loggedIn" in data:
            return bool(data["loggedIn"])
    return True
