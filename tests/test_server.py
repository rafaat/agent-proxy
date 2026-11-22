import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import httpx
import httpx2
import pytest
import uvicorn
from mcp import types
from mcp.client.session import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client

from gateway.server import create_app
from tests.conftest import free_port

AUTH = {"Authorization": "Bearer secret-key"}


@pytest.fixture
def gateway(
    make_settings,
) -> Callable[[], contextlib.AbstractAsyncContextManager[httpx.AsyncClient]]:
    # The lifespan must enter and exit in the same task, so tests open it themselves.
    @contextlib.asynccontextmanager
    async def _open() -> AsyncIterator[httpx.AsyncClient]:
        app = create_app(make_settings())
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
                yield c

    return _open


@pytest.fixture
def live_overrides() -> dict[str, object]:
    return {}


@pytest.fixture
async def live_url(make_settings, fake_bins, live_overrides: dict[str, object]) -> AsyncIterator[str]:
    port = free_port()
    settings = make_settings(
        claude_mcp_proxy_enabled=True, claude_interactive_bin=str(fake_bins["acp"]), **live_overrides
    )
    app = create_app(settings)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, lifespan="on"))
    task = asyncio.create_task(server.serve())
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    await task


async def test_health_is_public_and_routes_are_protected(gateway):
    async with gateway() as client:
        assert (await client.get("/health")).status_code == 200
        for path in ["/mcp", "/agents/claude/mcp", "/agents/codex/mcp", "/acp/session/prompt", "/messages/"]:
            resp = await client.post(path, json={})
            assert resp.status_code == 401, path
            assert resp.headers["www-authenticate"] == "Bearer"
        assert (await client.get("/acp/agents", headers={"Authorization": "Bearer wrong"})).status_code == 401
        assert (await client.get("/acp/agents?api_key=secret-key")).status_code == 200
        endpoint = "/messages/?session_id=" + "0" * 32
        assert (await client.post(endpoint, json={})).status_code == 401
        assert (await client.post(endpoint, headers=AUTH, json={})).status_code == 404


async def test_acp_prompt_endpoint(gateway, workspace: Path):
    async with gateway() as client:
        resp = await client.post(
            "/acp/session/prompt",
            headers=AUTH,
            json={
                "agent": "copilot",
                "cwd": "sub",
                "prompt": [{"type": "text", "text": "hello"}],
            },
        )
        body = resp.json()
        assert resp.status_code == 200, body
        assert body["stopReason"] == "end_turn" and body["ok"]
        assert body["sessionId"]
        assert json.loads(body["content"][0]["text"])["cwd"] == str(workspace / "sub")

        bad = await client.post(
            "/acp/session/prompt",
            headers=AUTH,
            json={"agent": "copilot", "cwd": "/", "prompt": "x"},
        )
        assert bad.status_code == 400
        failed = await client.post(
            "/acp/session/prompt",
            headers=AUTH,
            json={"agent": "claude", "prompt": "FAIL"},
        )
        assert failed.status_code == 502 and failed.json()["stopReason"] == "error"
        unknown = await client.post("/acp/session/prompt", headers=AUTH, json={"agent": "gpt", "prompt": "x"})
        assert unknown.status_code == 422
        resumed = await client.post(
            "/acp/session/prompt", headers=AUTH, json={"agent": "opencode", "sessionId": "ses_in", "prompt": "more"}
        )
        assert resumed.json()["sessionId"] == "ses_in"
        listed = await client.get("/acp/sessions?agent=copilot", headers=AUTH)
        assert [s["id"] for s in listed.json()["sessions"]] == ["p-in"]


async def test_mcp_streamable_http_end_to_end(live_url: str, workspace: Path, caplog: pytest.LogCaptureFixture):
    async with (
        httpx2.AsyncClient(headers=AUTH, timeout=30) as http,
        streamable_http_client(f"{live_url}/mcp", http_client=http) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        tools = (await session.list_tools()).tools
        names = {t.name for t in tools}
        # Bash is not in CLAUDE_PROXY_TOOLS, so it must not be re-exported.
        assert names == {
            "ask_claude",
            "ask_copilot",
            "ask_opencode",
            "ask_codex",
            "ask_claude_interactive",
            "ask_copilot_interactive",
            "ask_opencode_interactive",
            "ask_codex_interactive",
            "list_available_agents",
            "list_sessions",
            "list_agent_capabilities",
            "claude_Read",
        }
        capabilities_tool = next(t for t in tools if t.name == "list_agent_capabilities")
        ask_agent_schema = capabilities_tool.input_schema["properties"]["ask_agent"]
        assert ask_agent_schema["default"] is True
        assert "always" not in json.dumps(ask_agent_schema)
        capabilities = await session.call_tool("list_agent_capabilities", {"agent": "claude"})
        assert not capabilities.is_error
        assert "self_report" not in json.loads(capabilities.content[0].text)
        legacy_false = await session.call_tool("list_agent_capabilities", {"agent": "codex", "ask_agent": False})
        assert not legacy_false.is_error
        assert "self_report" not in json.loads(legacy_false.content[0].text)
        invalid_mode = await session.call_tool("list_agent_capabilities", {"agent": "claude", "ask_agent": "always"})
        assert invalid_mode.is_error

        ok = await session.call_tool("ask_opencode", {"prompt": "hi", "working_dir": "sub"})
        assert not ok.is_error
        assert json.loads(ok.content[0].text)["cwd"] == str(workspace / "sub")
        assert ok.structured_content["session_id"] == "ses_new"
        assert "session_id: ses_new" in ok.content[1].text

        resumed = await session.call_tool("ask_claude", {"prompt": "again", "session_id": "c-in"})
        assert resumed.structured_content["session_id"] == "c-in"
        listed = await session.call_tool("list_sessions", {"agent": "codex"})
        assert [s["id"] for s in json.loads(listed.content[0].text)["sessions"]] == ["thread-in"]

        fail = await session.call_tool("ask_copilot", {"prompt": "FAIL"})
        assert fail.is_error and "exit code 3" in fail.content[0].text

        read_ok = await session.call_tool("claude_Read", {"file_path": "sub/x.txt"})
        assert read_ok.content[0].text == f"READ:{workspace / 'sub' / 'x.txt'}"
        escaped = await session.call_tool("claude_Read", {"file_path": "../../etc/passwd"})
        assert escaped.is_error and "outside the allowed workspaces" in escaped.content[0].text
        no_approval_needed = await session.call_tool("ask_claude_interactive", {"prompt": "hi"})
        assert not no_approval_needed.is_error
        assert no_approval_needed.structured_content["output"] == "hello world model=None"

    requests = [record for record in caplog.records if record.msg == "mcp_tool_request"]
    assert [record.tool for record in requests] == [
        "list_agent_capabilities",
        "list_agent_capabilities",
        "list_agent_capabilities",
        "ask_opencode",
        "ask_claude",
        "list_sessions",
        "ask_copilot",
        "claude_Read",
        "claude_Read",
        "ask_claude_interactive",
    ]
    assert all(not record.args and not hasattr(record, "arguments") for record in requests)


@pytest.mark.parametrize(
    ("live_overrides", "outside_decision"),
    [({}, "no"), ({"interactive_approve_outside_paths": True}, "yes")],
)
async def test_interactive_permission_round_trip(live_url: str, workspace: Path, outside_decision: str):
    messages = []
    approve = types.ElicitResult(action="accept", content={"approve": True})
    decisions = iter([approve, types.ElicitResult(action="decline"), approve])

    async def elicit(_context, params: types.ElicitRequestParams) -> types.ElicitResult:
        messages.append(params.message)
        return next(decisions)

    async with (
        httpx2.AsyncClient(headers=AUTH, timeout=30) as http,
        streamable_http_client(f"{live_url}/agents/claude/mcp", http_client=http) as (read, write),
        ClientSession(read, write, elicitation_callback=elicit) as session,
    ):
        await session.initialize()
        cases = (("example.txt", "yes"), ("example.txt", "no"), ("../../outside.txt", outside_decision))
        for path, expected in cases:
            result = await session.call_tool("ask_claude_interactive", {"prompt": f"PERM|{path}", "working_dir": "sub"})
            assert not result.is_error
            assert result.structured_content["output"] == f"perm={expected}"
        inside = f"claude requests permission: Edit example file\nReported paths:\n{workspace / 'sub' / 'example.txt'}"
        assert messages[:2] == [inside] * 2
        if outside_decision == "no":
            assert len(messages) == 2
        else:
            outside = workspace.parent / "outside.txt"
            assert messages[2].endswith(f"{outside}\nWARNING: outside the allowed workspaces:\n{outside}")


async def test_interactive_followup_resumes_same_turn(live_url: str, workspace: Path):
    async with (
        httpx2.AsyncClient(headers=AUTH, timeout=30) as http,
        streamable_http_client(f"{live_url}/mcp", http_client=http) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        tool = "ask_claude_interactive"
        pending = await session.call_tool(tool, {"prompt": "PERM_TWICE|example.txt", "working_dir": "sub"})
        assert not pending.is_error
        request = pending.structured_content
        assert request["status"] == "approval_required"
        assert str(workspace / "sub" / "example.txt") in request["question"]
        reply = request["reply"]
        assert reply["tool"] == tool
        assert reply["approval"]["approval_id"] in pending.content[0].text
        args = {"session_id": reply["session_id"], "approval": reply["approval"] | {"approve": True}}
        assert (await session.call_tool(tool, args | {"session_id": "different-session"})).is_error

        second = await session.call_tool(tool, args)
        assert not second.is_error
        assert second.structured_content["status"] == "approval_required"
        second_reply = second.structured_content["reply"]
        assert second_reply["session_id"] == args["session_id"]
        assert second_reply["approval"]["approval_id"] != args["approval"]["approval_id"]
        assert (await session.call_tool(tool, args)).is_error
        finished = await session.call_tool(
            tool, {"session_id": second_reply["session_id"], "approval": second_reply["approval"] | {"approve": False}}
        )
        assert not finished.is_error
        assert finished.structured_content["session_id"] == args["session_id"]
        assert finished.structured_content["output"] == "perm=yes perm=no"


@pytest.mark.parametrize("live_overrides", [{"interactive_approval_timeout_seconds": 0.2}])
async def test_interactive_followup_expires(live_url: str):
    async with (
        httpx2.AsyncClient(headers=AUTH, timeout=30) as http,
        streamable_http_client(f"{live_url}/mcp", http_client=http) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        tool = "ask_claude_interactive"
        pending = await session.call_tool(tool, {"prompt": "PERM|example.txt"})
        assert not pending.is_error
        reply = pending.structured_content["reply"]
        await asyncio.sleep(0.25)
        expired = await session.call_tool(
            tool, {"session_id": reply["session_id"], "approval": reply["approval"] | {"approve": True}}
        )
        assert expired.is_error
        assert "expired" in expired.content[0].text


@pytest.mark.parametrize("live_overrides", [{"interactive_approval_timeout_seconds": 0.2}])
async def test_interactive_permission_timeout_denies(live_url: str):
    shown = asyncio.Event()

    async def elicit(_context, _params: types.ElicitRequestParams) -> types.ElicitResult:
        shown.set()
        await asyncio.sleep(0.5)
        return types.ElicitResult(action="accept", content={"approve": True})

    async with (
        httpx2.AsyncClient(headers=AUTH, timeout=30) as http,
        streamable_http_client(f"{live_url}/mcp", http_client=http) as (read, write),
        ClientSession(read, write, elicitation_callback=elicit) as session,
    ):
        await session.initialize()
        result = await session.call_tool("ask_claude_interactive", {"prompt": "PERM|example.txt"})
        assert shown.is_set()
        assert not result.is_error
        assert result.structured_content["output"] == "perm=no"


@pytest.mark.parametrize(
    "live_overrides", [{"subprocess_timeout_seconds": 0.5, "interactive_approval_timeout_seconds": 5}]
)
async def test_pending_approval_pauses_run_timeout(live_url: str):
    async def elicit(_context, _params: types.ElicitRequestParams) -> types.ElicitResult:
        await asyncio.sleep(1.0)
        return types.ElicitResult(action="accept", content={"approve": True})

    async with (
        httpx2.AsyncClient(headers=AUTH, timeout=30) as http,
        streamable_http_client(f"{live_url}/mcp", http_client=http) as (read, write),
        ClientSession(read, write, elicitation_callback=elicit) as session,
    ):
        await session.initialize()
        result = await session.call_tool("ask_claude_interactive", {"prompt": "PERM|example.txt"})
        assert not result.is_error, result.content
        assert result.structured_content["output"] == "perm=yes"


async def test_per_agent_mount_and_authenticated_sse(live_url: str):
    async with (
        httpx2.AsyncClient(headers=AUTH, timeout=30) as http,
        streamable_http_client(f"{live_url}/agents/copilot/mcp", http_client=http) as (
            read,
            write,
        ),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        assert [t.name for t in (await session.list_tools()).tools] == [
            "ask_copilot",
            "ask_copilot_interactive",
            "list_sessions",
            "list_agent_capabilities",
        ]
        listed = await session.call_tool("list_sessions", {})
        assert [s["id"] for s in json.loads(listed.content[0].text)["sessions"]] == ["p-in"]
        capabilities = await session.call_tool("list_agent_capabilities", {})
        assert not capabilities.is_error
        report = json.loads(capabilities.content[0].text)
        assert report["self_report"]["ok"]
        assert report["self_report_reason"] == "cli_lacks_native_listing_for_tools"
        listing_only = await session.call_tool("list_agent_capabilities", {"ask_agent": False})
        assert not listing_only.is_error
        assert "self_report" not in json.loads(listing_only.content[0].text)

    async with (
        sse_client(f"{live_url}/agents/claude/sse", headers=AUTH) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        names = {t.name for t in (await session.list_tools()).tools}
        assert names == {
            "ask_claude",
            "ask_claude_interactive",
            "list_sessions",
            "list_agent_capabilities",
            "claude_Read",
        }
        result = await session.call_tool("ask_claude", {"prompt": "over sse"})
        assert json.loads(result.content[0].text)["stdin"] == "over sse"
