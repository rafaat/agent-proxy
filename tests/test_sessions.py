import json
from pathlib import Path

import pytest

from gateway.adapters import AgentRegistry
from gateway.config import AgentName, InputError, WorkspaceError


async def test_list_sessions_filters_to_workspace_and_reads_titles(make_settings):
    registry = AgentRegistry(make_settings())
    sessions = await registry.list_sessions(None, None, 20)
    by_agent = {s.agent: s for s in sessions}
    assert {s.id for s in sessions} == {"c-in", "p-in", "thread-in", "ses_in"}
    assert by_agent["claude"].title == "Fix login bug"
    assert by_agent["copilot"].title == "Copilot p-in"
    assert by_agent["codex"].title == "Codex named thread"
    assert by_agent["opencode"].title == "OpenCode in"
    assert [s.updated_at for s in sessions] == sorted((s.updated_at for s in sessions), reverse=True)

    only_sub = await registry.list_sessions(AgentName.CLAUDE, "sub", 20)
    assert [s.id for s in only_sub] == ["c-in"]
    with pytest.raises(WorkspaceError):
        await registry.list_sessions(None, "/", 20)


async def test_new_sessions_return_an_id(make_settings):
    registry = AgentRegistry(make_settings())
    claude = await registry.invoke(AgentName.CLAUDE, "hi", None, None)
    argv = json.loads(claude.output)["argv"]
    assert argv[argv.index("--session-id") + 1] == claude.session_id
    assert (await registry.invoke(AgentName.OPENCODE, "hi", None, None)).session_id == "ses_new"
    codex = await registry.invoke(AgentName.CODEX, "hi", None, None)
    assert codex.session_id == "thread-new"
    assert json.loads(codex.output)["stdin"] == "hi"


@pytest.mark.parametrize(
    ("agent", "session_id", "expected_args", "cwd_suffix"),
    [
        (AgentName.CLAUDE, "c-in", ["--resume", "c-in"], "sub"),
        (AgentName.COPILOT, "p-in", ["--resume=p-in"], ""),
        (AgentName.OPENCODE, "ses_in", ["--session", "ses_in"], ""),
        (AgentName.CODEX, "thread-in", ["resume", "--json"], ""),
    ],
)
async def test_resume_runs_in_session_cwd(make_settings, workspace: Path, agent, session_id, expected_args, cwd_suffix):
    result = await AgentRegistry(make_settings()).invoke(agent, "continue", None, None, session_id)
    payload = json.loads(result.output)
    argv = payload["argv"]
    start = argv.index(expected_args[0])
    assert argv[start : start + len(expected_args)] == expected_args
    assert "--session-id" not in argv
    assert payload["cwd"] == str(workspace / cwd_suffix)
    assert result.session_id == session_id


async def test_resume_latest_and_rejections(make_settings):
    registry = AgentRegistry(make_settings())
    latest = await registry.invoke(AgentName.COPILOT, "go on", None, None, "latest")
    assert latest.session_id == "p-in"
    with pytest.raises(WorkspaceError):
        await registry.invoke(AgentName.CLAUDE, "x", None, None, "c-out")
    with pytest.raises(WorkspaceError, match="belongs to"):
        await registry.invoke(AgentName.CODEX, "x", "sub", None, "thread-in")
    with pytest.raises(InputError, match="Unknown"):
        await registry.invoke(AgentName.OPENCODE, "x", None, None, "ses_missing")
    with pytest.raises(InputError, match="Invalid session_id"):
        await registry.invoke(AgentName.CLAUDE, "x", None, None, "--fork-session")


async def test_acp_resume_loads_session_and_skips_replay(make_settings, fake_bins):
    settings = make_settings(opencode_mode="acp", opencode_bin=str(fake_bins["acp"]), opencode_acp_args="acp")
    result = await AgentRegistry(settings).invoke(AgentName.OPENCODE, "hi", None, None, "ses_in")
    assert result.ok, result.stderr
    assert result.output == "hello world model=None resumed=ses_in"
    assert result.session_id == "ses_in"
