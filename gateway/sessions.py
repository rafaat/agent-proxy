"""Read each agent's native session store so callers can list and resume conversations."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import yaml

LATEST = "latest"
_TITLE_CHARS = 200


@dataclass(slots=True)
class SessionInfo:
    agent: str
    id: str
    cwd: str
    title: str | None
    created_at: str | None
    updated_at: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SessionStore(Protocol):
    async def list(self, cwd: Path, limit: int) -> list[SessionInfo]:
        """Sessions whose cwd is `cwd` or below it, newest first."""
        ...

    async def get(self, session_id: str) -> SessionInfo | None: ...


def _within(raw_cwd: str | None, root: Path) -> bool:
    if not raw_cwd:
        return False
    try:
        return Path(raw_cwd).resolve().is_relative_to(root)
    except (OSError, ValueError):
        return False


def _iso(value: Any) -> str | None:
    """Normalize epoch milliseconds, epoch seconds, datetimes, or ISO strings to ISO-8601 UTC."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=UTC)
        return dt.astimezone(UTC).isoformat()
    if isinstance(value, int | float):
        seconds = value / 1000 if value > 1e11 else value
        return datetime.fromtimestamp(seconds, UTC).isoformat()
    with contextlib.suppress(ValueError):
        return _iso(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    return str(value)


def _mtime_iso(path: Path) -> str | None:
    with contextlib.suppress(OSError):
        return _iso(path.stat().st_mtime)
    return None


def _clip(text: Any) -> str | None:
    if not isinstance(text, str) or not text.strip():
        return None
    line = " ".join(text.split())
    return line[:_TITLE_CHARS]


def _newest_first(paths: Iterator[Path]) -> list[Path]:
    stamped = []
    for path in paths:
        with contextlib.suppress(OSError):
            stamped.append((path.stat().st_mtime, path))
    return [p for _, p in sorted(stamped, key=lambda item: item[0], reverse=True)]


def _sort(sessions: list[SessionInfo]) -> list[SessionInfo]:
    return sorted(sessions, key=lambda s: s.updated_at or "", reverse=True)


# ---------------------------------------------------------------------------
# Claude Code: ~/.claude/projects/<encoded cwd>/<session id>.jsonl
# ---------------------------------------------------------------------------


class ClaudeSessionStore:
    def __init__(self, projects_dir: Path):
        self._dir = projects_dir

    @staticmethod
    def encode(cwd: Path) -> str:
        return re.sub(r"[^A-Za-z0-9]", "-", str(cwd))

    async def list(self, cwd: Path, limit: int) -> list[SessionInfo]:
        return await asyncio.to_thread(self._list, cwd, limit)

    async def get(self, session_id: str) -> SessionInfo | None:
        return await asyncio.to_thread(self._get, session_id)

    def _list(self, cwd: Path, limit: int) -> list[SessionInfo]:
        if not self._dir.is_dir():
            return []
        prefix = self.encode(cwd)
        # Encoding is lossy, so prefix-match directories, then confirm with the recorded cwd.
        dirs = [d for d in self._dir.iterdir() if d.is_dir() and (d.name == prefix or d.name.startswith(prefix + "-"))]
        files = _newest_first(f for d in dirs for f in d.glob("*.jsonl"))
        sessions: list[SessionInfo] = []
        for path in files:
            info = self._parse(path)
            if info and _within(info.cwd, cwd):
                sessions.append(info)
                if len(sessions) >= limit:
                    break
        return sessions

    def _get(self, session_id: str) -> SessionInfo | None:
        if not self._dir.is_dir():
            return None
        for path in self._dir.glob(f"*/{session_id}.jsonl"):
            return self._parse(path)
        return None

    @staticmethod
    def _parse(path: Path) -> SessionInfo | None:
        cwd = created = first_prompt = None
        titles: dict[str, str] = {}
        try:
            with path.open("rb") as fh:
                for line in fh:
                    # Cheap substring checks before json.loads; transcripts can be large.
                    wanted = cwd is None or first_prompt is None or b'-title"' in line or b'"last-prompt"' in line
                    if not wanted:
                        continue
                    with contextlib.suppress(ValueError):
                        record = json.loads(line)
                        kind = record.get("type")
                        created = created or record.get("timestamp")
                        cwd = cwd or record.get("cwd")
                        if kind == "custom-title":
                            titles["custom"] = record.get("customTitle")
                        elif kind == "ai-title":
                            titles["ai"] = record.get("aiTitle")
                        elif kind == "last-prompt":
                            titles["last"] = record.get("lastPrompt")
                        elif kind == "user" and first_prompt is None and not record.get("isMeta"):
                            content = (record.get("message") or {}).get("content")
                            first_prompt = content if isinstance(content, str) else ""
        except OSError:
            return None
        if not cwd:
            return None
        title = next(
            (t for t in (_clip(titles.get(k)) for k in ("custom", "ai")) if t),
            _clip(first_prompt) or _clip(titles.get("last")),
        )
        return SessionInfo("claude", path.stem, cwd, title, _iso(created), _mtime_iso(path))


# ---------------------------------------------------------------------------
# GitHub Copilot CLI: ~/.copilot/session-state/<id>/workspace.yaml
# ---------------------------------------------------------------------------


class CopilotSessionStore:
    def __init__(self, state_dir: Path):
        self._dir = state_dir

    async def list(self, cwd: Path, limit: int) -> list[SessionInfo]:
        return await asyncio.to_thread(self._list, cwd, limit)

    async def get(self, session_id: str) -> SessionInfo | None:
        return await asyncio.to_thread(self._parse, self._dir / session_id / "workspace.yaml")

    def _list(self, cwd: Path, limit: int) -> list[SessionInfo]:
        if not self._dir.is_dir():
            return []
        sessions = [
            info
            for path in self._dir.glob("*/workspace.yaml")
            if (info := self._parse(path)) is not None and _within(info.cwd, cwd)
        ]
        return _sort(sessions)[:limit]

    @staticmethod
    def _parse(path: Path) -> SessionInfo | None:
        try:
            data = yaml.safe_load(path.read_text())
        except (OSError, yaml.YAMLError):
            return None
        if not isinstance(data, dict) or not data.get("cwd"):
            return None
        return SessionInfo(
            "copilot",
            str(data.get("id") or path.parent.name),
            str(data["cwd"]),
            _clip(data.get("name")),
            _iso(data.get("created_at")),
            _iso(data.get("updated_at")) or _mtime_iso(path),
        )


# ---------------------------------------------------------------------------
# OpenAI Codex: ~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<id>.jsonl
# ---------------------------------------------------------------------------


class CodexSessionStore:
    def __init__(self, sessions_dir: Path, index_file: Path):
        self._dir = sessions_dir
        self._index = index_file

    async def list(self, cwd: Path, limit: int) -> list[SessionInfo]:
        return await asyncio.to_thread(self._list, cwd, limit)

    async def get(self, session_id: str) -> SessionInfo | None:
        return await asyncio.to_thread(self._get, session_id)

    def _list(self, cwd: Path, limit: int) -> list[SessionInfo]:
        if not self._dir.is_dir():
            return []
        names = self._thread_names()
        sessions: list[SessionInfo] = []
        for path in _newest_first(self._dir.rglob("rollout-*.jsonl")):
            info = self._parse(path, names)
            if info and _within(info.cwd, cwd):
                sessions.append(info)
                if len(sessions) >= limit:
                    break
        return sessions

    def _get(self, session_id: str) -> SessionInfo | None:
        if not self._dir.is_dir():
            return None
        for path in self._dir.rglob(f"rollout-*{session_id}.jsonl"):
            info = self._parse(path, self._thread_names())
            if info and info.id == session_id:
                return info
        return None

    def _thread_names(self) -> dict[str, str]:
        names: dict[str, str] = {}
        with contextlib.suppress(OSError), self._index.open() as fh:
            for line in fh:
                with contextlib.suppress(ValueError):
                    entry = json.loads(line)
                    if entry.get("id") and entry.get("thread_name"):
                        names[entry["id"]] = entry["thread_name"]
        return names

    @staticmethod
    def _parse(path: Path, names: dict[str, str]) -> SessionInfo | None:
        try:
            with path.open("rb") as fh:
                meta = json.loads(fh.readline())
                if meta.get("type") != "session_meta":
                    return None
                payload = meta.get("payload") or {}
                session_id = payload.get("id") or payload.get("session_id")
                title = names.get(session_id)
                if title is None:
                    for line in fh:
                        if b'"user_message"' in line:
                            with contextlib.suppress(ValueError):
                                title = (json.loads(line).get("payload") or {}).get("message")
                                break
        except (OSError, ValueError):
            return None
        if not session_id or not payload.get("cwd"):
            return None
        return SessionInfo(
            "codex", session_id, payload["cwd"], _clip(title), _iso(payload.get("timestamp")), _mtime_iso(path)
        )


# ---------------------------------------------------------------------------
# OpenCode: `opencode session list --format json`
# ---------------------------------------------------------------------------

CommandRunner = Callable[[], Awaitable[bytes | None]]


class OpenCodeSessionStore:
    def __init__(self, run_list: CommandRunner):
        self._run_list = run_list

    async def list(self, cwd: Path, limit: int) -> list[SessionInfo]:
        return _sort([s for s in await self._all() if _within(s.cwd, cwd)])[:limit]

    async def get(self, session_id: str) -> SessionInfo | None:
        return next((s for s in await self._all() if s.id == session_id), None)

    async def _all(self) -> list[SessionInfo]:
        stdout = await self._run_list()
        if not stdout:
            return []
        try:
            entries = json.loads(stdout)
        except ValueError:
            return []
        return [
            SessionInfo(
                "opencode",
                str(e["id"]),
                str(e["directory"]),
                _clip(e.get("title")),
                _iso(e.get("created")),
                _iso(e.get("updated")),
            )
            for e in entries
            if isinstance(e, dict) and e.get("id") and e.get("directory")
        ]
