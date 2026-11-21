"""Minimal Agent Client Protocol (ACP) client over stdio JSON-RPC.

Implements the subset needed to drive one prompt turn headlessly:
initialize -> session/new -> (session/set_config_option model) -> session/prompt,
while collecting `session/update` message chunks and answering permission requests.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gateway.config import PermissionPolicy, WorkspaceError

logger = logging.getLogger(__name__)

ACP_PROTOCOL_VERSION = 1
_STREAM_LIMIT = 16 * 1024 * 1024
_METHOD_NOT_FOUND = -32601
_MAX_TRACKED_TOOLS = 128

PermissionApproval = Callable[[str, str, tuple[Path, ...]], Awaitable[bool]]


class AcpError(RuntimeError):
    pass


@dataclass
class AcpTurn:
    stop_reason: str | None = None
    text: list[str] = field(default_factory=list)

    @property
    def output(self) -> str:
        return "".join(self.text)


class AcpStdioClient:
    """One ACP agent process, one session, one prompt turn at a time."""

    def __init__(
        self,
        process: asyncio.subprocess.Process,
        *,
        permission_policy: PermissionPolicy,
        resolve_path: Callable[[str], Path],
        max_output_bytes: int,
        approve_permission: PermissionApproval | None = None,
    ):
        self._proc = process
        self._policy = permission_policy
        self._resolve_path = resolve_path
        self._approve_permission = approve_permission
        self._tool_calls: dict[str, dict[str, Any]] = {}
        self._max_output = max_output_bytes
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._turn = AcpTurn()
        self._output_size = 0
        self.truncated = False
        self._reader: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()
        self.session_id: str | None = None

    @staticmethod
    def stream_limit() -> int:
        return _STREAM_LIMIT

    async def __aenter__(self) -> AcpStdioClient:
        self._reader = asyncio.create_task(self._read_loop())
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._reader:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader

    async def run_turn(self, cwd: Path, prompt: str, model: str | None, resume_id: str | None = None) -> AcpTurn:
        init = await self.request(
            "initialize",
            {
                "protocolVersion": ACP_PROTOCOL_VERSION,
                "clientCapabilities": {
                    "fs": {"readTextFile": False, "writeTextFile": False},
                    "terminal": False,
                },
                "clientInfo": {"name": "agent-proxy", "version": "0.1.0"},
            },
        )
        if resume_id:
            session = await self._resume(init.get("agentCapabilities") or {}, resume_id, cwd)
            session_id = self.session_id = resume_id
        else:
            session = await self.request("session/new", {"cwd": str(cwd), "mcpServers": []})
            session_id = self.session_id = session["sessionId"]
        if model:
            await self._select_model(session_id, session, model)
        # session/load replays history as updates. Only collect the new turn.
        self._turn = AcpTurn()
        self._output_size = 0
        self.truncated = False
        result = await self.request(
            "session/prompt",
            {"sessionId": session_id, "prompt": [{"type": "text", "text": prompt}]},
        )
        self._turn.stop_reason = result.get("stopReason")
        return self._turn

    async def _resume(self, capabilities: dict[str, Any], session_id: str, cwd: Path) -> dict[str, Any]:
        params = {"sessionId": session_id, "cwd": str(cwd), "mcpServers": []}
        if "resume" in (capabilities.get("sessionCapabilities") or {}):
            return await self.request("session/resume", params) or {}
        if capabilities.get("loadSession"):
            return await self.request("session/load", params) or {}
        raise AcpError("ACP agent does not support resuming sessions")

    async def _select_model(self, session_id: str, session: dict[str, Any], model: str) -> None:
        options = session.get("configOptions") or []
        if any(opt.get("id") == "model" for opt in options):
            await self.request(
                "session/set_config_option",
                {"sessionId": session_id, "configId": "model", "value": model},
            )
            return
        # Older ACP agents expose the unstable session/set_model instead.
        try:
            await self.request("session/set_model", {"sessionId": session_id, "modelId": model})
        except AcpError as exc:
            logger.warning("acp_model_not_supported", extra={"model": model, "error": str(exc)})

    async def request(self, method: str, params: dict[str, Any]) -> Any:
        self._next_id += 1
        msg_id = self._next_id
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = future
        try:
            await self._send({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params})
            return await future
        finally:
            self._pending.pop(msg_id, None)

    async def _send(self, message: dict[str, Any]) -> None:
        stdin = self._proc.stdin
        if stdin is None or stdin.is_closing():
            raise AcpError("ACP agent stdin is closed")
        async with self._write_lock:
            stdin.write(json.dumps(message).encode() + b"\n")
            await stdin.drain()

    async def _read_loop(self) -> None:
        assert self._proc.stdout is not None
        try:
            while line := await self._proc.stdout.readline():
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug(
                        "acp_non_json_line",
                        extra={"bytes": len(line)},
                    )
                    continue
                await self._dispatch(message)
        finally:
            exit_error = AcpError("ACP agent exited before responding")
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(exit_error)

    async def _dispatch(self, message: dict[str, Any]) -> None:
        if "method" not in message:
            future = self._pending.get(message.get("id"))  # type: ignore[arg-type]
            if future and not future.done():
                if "error" in message:
                    err = message["error"] or {}
                    future.set_exception(AcpError(f"{err.get('message', 'ACP error')} (code {err.get('code')})"))
                else:
                    future.set_result(message.get("result") or {})
            return

        method = message["method"]
        params = message.get("params") or {}
        if "id" not in message:
            if method == "session/update":
                self._on_update(params.get("update") or {})
            return

        if method == "session/request_permission":
            result = await self._permission_outcome(params)
            await self._send({"jsonrpc": "2.0", "id": message["id"], "result": result})
        else:
            await self._send(
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "error": {
                        "code": _METHOD_NOT_FOUND,
                        "message": f"Client does not support {method}",
                    },
                }
            )

    def _on_update(self, update: dict[str, Any]) -> None:
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            content = update.get("content") or {}
            if content.get("type") == "text":
                self._append_text(content.get("text", ""))
        elif self._approve_permission and kind in ("tool_call", "tool_call_update"):
            tool_id = update.get("toolCallId")
            if isinstance(tool_id, str):
                if tool_id not in self._tool_calls and len(self._tool_calls) >= _MAX_TRACKED_TOOLS:
                    self._tool_calls.pop(next(iter(self._tool_calls)))
                details = self._tool_calls.setdefault(tool_id, {})
                details.update({key: update[key] for key in ("title", "locations") if key in update})

    def _append_text(self, text: str) -> None:
        if self.truncated:
            return
        size = len(text.encode())
        if self._output_size + size > self._max_output:
            self.truncated = True
            return
        self._output_size += size
        self._turn.text.append(text)

    async def _permission_outcome(self, params: dict[str, Any]) -> dict[str, Any]:
        options = params.get("options") or []
        call = params.get("toolCall") or {}
        if self._approve_permission:
            call = self._tool_calls.get(call.get("toolCallId"), {}) | call
        locations = call.get("locations") or []
        paths = []
        valid_paths = True
        try:
            for loc in locations:
                if not isinstance(loc, dict) or not isinstance(loc.get("path"), str) or not loc["path"]:
                    raise WorkspaceError("Invalid permission location")
                paths.append(self._resolve_path(loc["path"]))
        except (WorkspaceError, OSError, RuntimeError):
            valid_paths = False
        title = call.get("title")
        if self._approve_permission:
            one_time_option = any(opt.get("kind") == "allow_once" for opt in options)
            allow = (
                valid_paths
                and one_time_option
                and self.session_id is not None
                and params.get("sessionId") == self.session_id
                and isinstance(title, str)
                and bool(title.strip())
                and await self._approve_permission(self.session_id, title, tuple(paths))
            )
            preferred = ("allow_once",) if allow else ("reject_once",)
        else:
            allow = self._policy == "allow" and valid_paths
            preferred = ("allow_once", "allow_always") if allow else ("reject_once", "reject_always")
        for kind in preferred:
            for option in options:
                if option.get("kind") == kind:
                    logger.info(
                        "acp_permission",
                        extra={"decision": kind, "valid_paths": valid_paths},
                    )
                    return {
                        "outcome": {
                            "outcome": "selected",
                            "optionId": option["optionId"],
                        }
                    }
        return {"outcome": {"outcome": "cancelled"}}
