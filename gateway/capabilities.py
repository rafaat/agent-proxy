"""Parse agent CLI introspection output into a capabilities report.

Only allowlisted fields are copied, because raw MCP config output can contain env vars,
headers and OAuth details.
"""

from __future__ import annotations

import json
import re
from typing import Any

_CLAUDE_SERVER_CHARS = re.compile(r"[^A-Za-z0-9_-]")


def _json(raw: bytes) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _pick(item: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {key: item[key] for key in keys if key in item}


def claude_server_key(name: str) -> str:
    """Claude's tool prefix for a server: `plugin:slack:slack` -> `plugin_slack_slack`."""
    return _CLAUDE_SERVER_CHARS.sub("_", name)


def parse_claude_init(stdout: bytes) -> dict[str, Any] | None:
    """Read the `system/init` event from `claude -p --output-format stream-json --verbose`."""
    init = None
    for line in stdout.splitlines():
        event = _json(line)
        if isinstance(event, dict) and event.get("type") == "system" and event.get("subtype") == "init":
            init = event
            break
    if init is None:
        return None

    raw_tools, raw_skills = init.get("tools"), init.get("skills")
    tools = [t for t in raw_tools if isinstance(t, str)] if isinstance(raw_tools, list) else []
    servers: list[dict[str, Any]] = []
    claimed: set[str] = set()
    for server in init.get("mcp_servers", []):
        if not isinstance(server, dict) or not isinstance(server.get("name"), str):
            continue
        key = claude_server_key(server["name"])
        prefix = f"mcp__{key}__"
        names = [t for t in tools if t.startswith(prefix)]
        claimed.update(names)
        servers.append(
            _pick(server, "name", "status", "source")
            | {
                "tools": [t.removeprefix(prefix) for t in names] if isinstance(raw_tools, list) else None,
                "allow_rule": f"mcp__{key}__*",
            }
        )
    return {
        "mcp_servers": servers,
        "builtin_tools": [t for t in tools if not t.startswith("mcp__")] if isinstance(raw_tools, list) else None,
        "other_mcp_tools": [t for t in tools if t.startswith("mcp__") and t not in claimed],
        "skills": [{"name": s} for s in raw_skills if isinstance(s, str)] if isinstance(raw_skills, list) else None,
        "plugins": [_pick(p, "name", "source") for p in init.get("plugins", []) if isinstance(p, dict)],
        "subagents": [a for a in init.get("agents", []) if isinstance(a, str)],
        "slash_commands": [c for c in init.get("slash_commands", []) if isinstance(c, str)],
    }


def parse_copilot_mcp_list(stdout: bytes) -> list[dict[str, Any]] | None:
    """Parse `copilot mcp list --json`. Copilot can't list a server's tools without a model run."""
    data = _json(stdout)
    if not isinstance(data, dict) or not isinstance(data.get("mcpServers"), dict):
        return None
    servers = []
    for name, cfg in data["mcpServers"].items():
        if not isinstance(cfg, dict):
            continue
        servers.append(
            {"name": name}
            | _pick(cfg, "type", "source", "sourcePlugin")
            | {"enabled": cfg.get("enabled", True), "tool_filter": cfg.get("tools"), "tools": None}
            | {"allow_rule": name}
        )
    return servers


def _parse_skill_list(stdout: bytes, *keys: str) -> list[dict[str, Any]] | None:
    """Strict: any non-object or nameless entry makes the whole listing malformed (None), `[]` is valid."""
    data = _json(stdout)
    if not isinstance(data, list):
        return None
    if not all(isinstance(s, dict) and isinstance(s.get("name"), str) for s in data):
        return None
    return [_pick(s, *keys) for s in data]


def parse_opencode_skill_list(stdout: bytes) -> list[dict[str, Any]] | None:
    """`opencode debug skill` also prints skill bodies and paths; only name and description are kept."""
    return _parse_skill_list(stdout, "name", "description")


def parse_copilot_skill_list(stdout: bytes) -> list[dict[str, Any]] | None:
    return _parse_skill_list(stdout, "name", "description", "source", "enabled")


def parse_codex_mcp_list(stdout: bytes) -> list[dict[str, Any]] | None:
    data = _json(stdout)
    if not isinstance(data, list):
        return None
    servers = []
    for server in data:
        if not isinstance(server, dict) or "name" not in server:
            continue
        transport = server.get("transport")
        servers.append(
            _pick(server, "name", "enabled", "auth_status")
            | {"type": transport.get("type") if isinstance(transport, dict) else None, "tools": None}
        )
    return servers
