"""Fake agent binaries so tests never call real models."""

from __future__ import annotations

import json
import os
import re
import socket
import stat
import sys
import textwrap
from pathlib import Path

import pytest

from gateway.config import Settings

FAKE_CLI = """
import json, os, subprocess, sys, time
argv = sys.argv[1:]
if argv[:2] == ["mcp", "serve"]:
    from mcp.server.mcpserver import MCPServer
    srv = MCPServer("fake-claude")

    @srv.tool(name="Read")
    def read(file_path: str) -> str:
        return "READ:" + file_path

    @srv.tool(name="Bash")
    def bash(command: str) -> str:
        return "ran"

    srv.run()
    sys.exit(0)
if argv == ["--version"]:
    print("fake 1.0"); sys.exit(0)
if argv[:2] == ["auth", "status"]:
    print(json.dumps({"loggedIn": True})); sys.exit(0)
if argv[:2] == ["login", "status"]:
    print("Not logged in"); sys.exit(1)
if argv[:2] == ["session", "list"]:
    print(open(os.environ["FAKE_OPENCODE_SESSIONS"]).read()); sys.exit(0)
if argv[:2] == ["-p", "/cost"]:
    event = {"type": "system", "subtype": "init", "cwd": os.getcwd(), "memory_paths": {"auto": "/secret"},
        "tools": ["Read", "mcp__plugin_slack_slack__slack_read_channel", "mcp__scout__search", "mcp__gone__x"],
        "mcp_servers": [{"name": "plugin:slack:slack", "status": "connected", "source": "plugin"},
                        {"name": "scout", "status": "pending", "source": "user"}],
        "skills": ["git"], "agents": ["Explore"], "slash_commands": ["git"],
        "plugins": [{"name": "slack", "path": "/p", "source": "slack@official"}]}
    if "EMPTY" in argv:
        event["tools"] = []
        event["skills"] = []
    if "NO_SERVERS" in argv:
        event["mcp_servers"] = []
    if "NO_TOOLS" in argv:
        event.pop("tools")
    if "NO_SKILLS" in argv:
        event.pop("skills")
    print(json.dumps(event))
    print(json.dumps({"type": "result", "subtype": "success"})); sys.exit(0)
if argv == ["mcp", "list", "--json"]:
    print(json.dumps({"mcpServers": {"slack": {"type": "http", "source": "user", "tools": ["*"],
        "headers": {"Authorization": "Bearer FAKE_SECRET"}, "env": {"TOKEN": "FAKE_SECRET"}}}})); sys.exit(0)
if argv[:2] == ["skill", "list"] and len(argv) == 4:
    # Stub variants for native-listing outcomes: empty, transient error, malformed.
    if argv[3] == "EMPTY":
        print("[]"); sys.exit(0)
    if argv[3] == "ERROR":
        print("boom SECRET_RAW"); sys.exit(3)
    if argv[3] == "BAD":
        print('{"not": "a list"}'); sys.exit(0)
if argv == ["debug", "skill"]:
    print(json.dumps([{"name": "oc", "description": "OC", "location": "/p", "content": "SECRET_BODY"}])); sys.exit(0)
if argv == ["skill", "list", "--json"]:
    print(json.dumps([{"name": "git", "description": "Git", "source": "inherited", "path": "/x", "enabled": True}]))
    sys.exit(0)
stdin = sys.stdin.read() if not sys.stdin.isatty() else ""
text = " ".join(argv) + stdin
if "SLEEP" in text:
    child = subprocess.Popen(["sleep", "60"])
    open(os.environ["FAKE_PID_FILE"], "w").write(str(child.pid))
    time.sleep(60)
if "ORPHAN" in text:
    child = subprocess.Popen(["sleep", "60"])
    open(os.environ["FAKE_PID_FILE"], "w").write(str(child.pid))
    sys.exit(0)
if "FAIL" in text:
    print("boom", file=sys.stderr); sys.exit(3)
if "BIG" in text:
    sys.stdout.write("x" * 50_000); sys.exit(0)
payload = json.dumps({"argv": argv, "stdin": stdin, "cwd": os.getcwd()})
if "--format" in argv:  # opencode run --format json
    sid = argv[argv.index("--session") + 1] if "--session" in argv else "ses_new"
    print(json.dumps({"type": "step_start", "sessionID": sid, "part": {"type": "step-start"}}))
    print(json.dumps({"type": "text", "sessionID": sid, "part": {"type": "text", "text": payload}}))
elif "--json" in argv:  # codex exec --json
    sid = next((a for a in argv if a.startswith("thread-")), "thread-new")
    print(json.dumps({"type": "thread.started", "thread_id": sid}))
    print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "draft"}}))
    print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": payload}}))
else:
    print(payload)
"""

FAKE_ACP = """
import json, os, sys
if sys.argv[1:3] == ["session", "list"]:  # same binary also serves the session store
    print(open(os.environ["FAKE_OPENCODE_SESSIONS"]).read()); sys.exit(0)
def send(m):
    sys.stdout.write(json.dumps(m) + "\\n"); sys.stdout.flush()
def update(text):
    send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": "s1",
          "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}}})
model = None
resumed = None
for line in sys.stdin:
    msg = json.loads(line)
    method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if method == "initialize":
        caps = {"loadSession": True}
        send({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": 1, "agentCapabilities": caps}})
    elif method == "session/load":
        update("REPLAYED HISTORY ")
        resumed = params["sessionId"]
        send({"jsonrpc": "2.0", "id": mid, "result": {}})
    elif method == "session/new":
        send({"jsonrpc": "2.0", "id": mid, "result": {"sessionId": "s1", "configOptions": [{"id": "model"}]}})
    elif method == "session/set_config_option":
        model = params["value"]
        send({"jsonrpc": "2.0", "id": mid, "result": {}})
    elif method == "session/prompt":
        text = params["prompt"][0]["text"]
        if "HANG" in text:
            continue
        if "PERM" in text:
            send({"jsonrpc": "2.0", "method": "session/update", "params": {
                "sessionId": "s1", "update": {"sessionUpdate": "tool_call", "toolCallId": "t1",
                "title": "Edit example file", "locations": [{"path": text.split("|")[1]}]}}})
            send({"jsonrpc": "2.0", "id": 99, "method": "session/request_permission", "params": {
                "sessionId": "s1",
                "toolCall": {"toolCallId": "t1", "locations": [{"path": text.split("|")[1]}]},
                "options": [{"optionId": "yes", "kind": "allow_once"}, {"optionId": "no", "kind": "reject_once"}]}})
            reply = json.loads(sys.stdin.readline())
            update("perm=" + reply["result"]["outcome"]["optionId"])
            if "PERM_TWICE" in text:
                send({"jsonrpc": "2.0", "id": 100, "method": "session/request_permission", "params": {
                    "sessionId": "s1", "toolCall": {"toolCallId": "t1"},
                    "options": [{"optionId": "yes", "kind": "allow_once"},
                                {"optionId": "no", "kind": "reject_once"}]}})
                reply = json.loads(sys.stdin.readline())
                update(" perm=" + reply["result"]["outcome"]["optionId"])
        else:
            update("hello ")
            update("world model=" + str(model) + (" resumed=" + resumed if resumed else ""))
        send({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn"}})
"""


def _write_exe(path: Path, body: str) -> Path:
    path.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    (ws / "sub").mkdir(parents=True)
    return ws.resolve()


@pytest.fixture
def fake_bins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    bins = tmp_path / "bin"
    bins.mkdir()
    monkeypatch.setenv("FAKE_PID_FILE", str(tmp_path / "child.pid"))
    return {
        "cli": _write_exe(bins / "fake-cli", FAKE_CLI),
        "acp": _write_exe(bins / "fake-acp", FAKE_ACP),
    }


@pytest.fixture
def stores(tmp_path: Path, workspace: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Native session stores for each agent with one session in the workspace and one outside it."""
    root = tmp_path / "stores"
    outside = tmp_path / "elsewhere"
    outside.mkdir()

    claude_dir = root / "claude"
    for sid, cwd, title in [("c-in", workspace / "sub", "Fix login bug"), ("c-out", outside, "Secret")]:
        d = claude_dir / re.sub(r"[^A-Za-z0-9]", "-", str(cwd))
        d.mkdir(parents=True, exist_ok=True)
        lines = [
            {"type": "user", "cwd": str(cwd), "timestamp": "2026-01-01T00:00:00Z", "message": {"content": "first"}},
            {"type": "ai-title", "aiTitle": title, "sessionId": sid},
        ]
        (d / f"{sid}.jsonl").write_text("\n".join(json.dumps(x) for x in lines))

    copilot_dir = root / "copilot"
    for sid, cwd in [("p-in", workspace), ("p-out", outside)]:
        (copilot_dir / sid).mkdir(parents=True)
        (copilot_dir / sid / "workspace.yaml").write_text(
            f"id: {sid}\ncwd: {cwd}\nname: 'Copilot {sid}'\nupdated_at: 2026-01-02T00:00:00Z\n"
        )

    codex_dir = root / "codex" / "2026" / "01" / "03"
    codex_dir.mkdir(parents=True)
    for sid, cwd in [("thread-in", workspace), ("thread-out", outside)]:
        meta = {"type": "session_meta", "payload": {"id": sid, "cwd": str(cwd), "timestamp": "2026-01-03T00:00:00Z"}}
        msg = {"type": "event_msg", "payload": {"type": "user_message", "message": f"codex {sid}"}}
        (codex_dir / f"rollout-2026-01-03T00-00-00-{sid}.jsonl").write_text(f"{json.dumps(meta)}\n{json.dumps(msg)}\n")
    index = root / "codex_index.jsonl"
    index.write_text(json.dumps({"id": "thread-in", "thread_name": "Codex named thread"}) + "\n")

    opencode = root / "opencode.json"
    opencode.write_text(
        json.dumps(
            [
                {"id": "ses_in", "title": "OpenCode in", "directory": str(workspace), "updated": 1767400000000},
                {"id": "ses_out", "title": "OpenCode out", "directory": str(outside), "updated": 1767500000000},
            ]
        )
    )
    monkeypatch.setenv("FAKE_OPENCODE_SESSIONS", str(opencode))
    return {
        "claude_projects_dir": str(claude_dir),
        "copilot_session_state_dir": str(copilot_dir),
        "codex_sessions_dir": str(root / "codex"),
        "codex_session_index": str(index),
    }


@pytest.fixture
def make_settings(workspace: Path, fake_bins: dict[str, Path], stores: dict[str, object]):
    def _make(**overrides: object) -> Settings:
        cli = str(fake_bins["cli"])
        base: dict[str, object] = {
            "gateway_api_key": "secret-key",
            "allowed_workspaces": [str(workspace)],
            "claude_bin": cli,
            "copilot_bin": cli,
            "opencode_bin": cli,
            "codex_bin": cli,
            "subprocess_timeout_seconds": 10,
            "claude_mcp_proxy_enabled": False,
            "claude_proxy_tools": ["Read"],
            **stores,
        }
        base.update(overrides)
        return Settings(_env_file=None, **base)  # type: ignore[arg-type]

    return _make


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True
