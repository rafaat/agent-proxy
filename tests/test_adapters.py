import asyncio
import json
import logging
import os
import time
from pathlib import Path

import pytest

from gateway.adapters import AgentRegistry, SessionTarget, build_cli_command, render_args, run_process
from gateway.config import AgentName, InputError, WorkspaceError
from tests.conftest import pid_alive


def test_render_args_keeps_prompt_as_one_safe_argv_element(workspace: Path):
    argv, used = render_args("run {prompt} --dir {cwd}", prompt="--yolo {cwd} $(rm -rf ~)", cwd=workspace)
    assert used
    assert argv == ["run", " --yolo {cwd} $(rm -rf ~)", "--dir", str(workspace)]

    argv, used = render_args("--prompt={prompt}", prompt="-x")
    assert argv == ["--prompt=-x"]


def test_template_without_prompt_uses_stdin(make_settings, workspace: Path):
    cfg = make_settings(claude_args="-p --output-format text").agent(AgentName.CLAUDE)
    argv, stdin = build_cli_command(cfg, "hello", workspace, "sonnet")
    assert argv[1:] == ["-p", "--output-format", "text", "--model", "sonnet"]
    assert stdin == b"hello"


def test_allowed_tools_are_appended_per_rule_for_new_and_resumed_sessions(make_settings, workspace: Path):
    rules = ["mcp__plugin_slack_slack__slack_search_*", "Bash(git diff *)"]
    cfg = make_settings(claude_args="-p", claude_resume_args="-p --resume {session_id}", claude_allowed_tools=rules)
    cfg = cfg.agent(AgentName.CLAUDE)
    expected = [f"--allowedTools={rule}" for rule in rules]
    argv, _ = build_cli_command(cfg, "hi", workspace, None)
    assert argv[1:] == ["-p", *expected]
    argv, _ = build_cli_command(cfg, "hi", workspace, None, SessionTarget("abc", resume=True))
    assert argv[1:] == ["-p", "--resume", "abc", *expected]


def test_allow_all_tools_appends_flag_only_when_enabled(make_settings, workspace: Path):
    off = make_settings(copilot_args="--prompt={prompt}").agent(AgentName.COPILOT)
    assert build_cli_command(off, "hi", workspace, None)[0][1:] == ["--prompt=hi"]
    on = make_settings(copilot_args="--prompt={prompt}", copilot_allow_all_tools=True).agent(AgentName.COPILOT)
    assert build_cli_command(on, "hi", workspace, None)[0][1:] == ["--prompt=hi", "--allow-all-tools"]


def test_claude_server_glob_rule_warns(make_settings, caplog: pytest.LogCaptureFixture):
    with caplog.at_level(logging.WARNING, logger="gateway.agents"):
        AgentRegistry(make_settings(claude_allowed_tools=["mcp__plugin_slack_*", "mcp__slack__read_*"]))
    rules = [r.rule for r in caplog.records if r.message == "allowed_tool_server_glob_unsupported"]
    assert rules == ["mcp__plugin_slack_*"]


async def test_invoke_runs_in_requested_cwd(make_settings, workspace: Path):
    registry = AgentRegistry(make_settings())
    result = await registry.invoke(AgentName.OPENCODE, "hi there", "sub", "prov/model")
    payload = json.loads(result.output)
    assert result.ok
    assert payload["cwd"] == str(workspace / "sub")
    assert payload["argv"] == ["run", "--format", "json", "hi there", "-m", "prov/model"]

    result = await registry.invoke(AgentName.CLAUDE, "via stdin", None, None)
    assert json.loads(result.output)["stdin"] == "via stdin"

    payload = json.loads((await registry.invoke(AgentName.CODEX, "-rf codex", None, "gpt-5")).output)
    assert payload["argv"][0] == "exec" and payload["argv"][-3:] == ["-", "-m", "gpt-5"]
    assert payload["stdin"] == "-rf codex"


async def test_invoke_validates_inputs(make_settings):
    registry = AgentRegistry(make_settings())
    with pytest.raises(WorkspaceError):
        await registry.invoke(AgentName.COPILOT, "hi", "/etc", None)
    with pytest.raises(InputError):
        await registry.invoke(AgentName.COPILOT, "   ", None, None)
    with pytest.raises(InputError):
        await registry.invoke(AgentName.COPILOT, "hi", None, "--evil")


async def test_nonzero_exit_is_reported(make_settings):
    result = await AgentRegistry(make_settings()).invoke(AgentName.COPILOT, "FAIL", None, None)
    assert not result.ok
    assert result.exit_code == 3
    assert "boom" in result.stderr


async def test_timeout_kills_whole_process_group(make_settings, tmp_path: Path):
    registry = AgentRegistry(make_settings(subprocess_timeout_seconds=1.5))
    started = time.monotonic()
    result = await registry.invoke(AgentName.OPENCODE, "SLEEP", None, None)
    assert result.timed_out and not result.ok
    assert time.monotonic() - started < 10
    grandchild = int((tmp_path / "child.pid").read_text())
    for _ in range(50):
        if not pid_alive(grandchild):
            break
        await asyncio.sleep(0.1)
    assert not pid_alive(grandchild)


async def test_output_is_capped(fake_bins, workspace: Path):
    outcome = await run_process([str(fake_bins["cli"]), "BIG"], workspace, timeout_seconds=10, max_output_bytes=1000)
    assert outcome.exit_code == 0
    assert outcome.truncated
    assert len(outcome.stdout) == 1000


async def test_timeout_cleans_group_after_leader_exits(fake_bins, workspace: Path, tmp_path: Path):
    outcome = await run_process(
        [str(fake_bins["cli"]), "ORPHAN"], workspace, timeout_seconds=0.5, max_output_bytes=1000
    )
    assert outcome.timed_out
    descendant = int((tmp_path / "child.pid").read_text())
    for _ in range(30):
        if not pid_alive(descendant):
            break
        await asyncio.sleep(0.1)
    assert not pid_alive(descendant)


async def test_child_env_drops_gateway_key(monkeypatch: pytest.MonkeyPatch, workspace: Path):
    monkeypatch.setenv("GATEWAY_API_KEY", "super-secret")
    outcome = await run_process(["/usr/bin/env"], workspace, timeout_seconds=10, max_output_bytes=100_000)
    assert b"super-secret" not in outcome.stdout
    assert os.environ["GATEWAY_API_KEY"] == "super-secret"


async def test_acp_adapter_collects_chunks_and_sets_model(make_settings, fake_bins):
    settings = make_settings(opencode_mode="acp", opencode_bin=str(fake_bins["acp"]), opencode_acp_args="acp")
    result = await AgentRegistry(settings).invoke(AgentName.OPENCODE, "hi", None, "prov/m1")
    assert result.ok, result.stderr
    assert result.mode == "acp"
    assert result.stop_reason == "end_turn"
    assert result.output == "hello world model=prov/m1"


async def test_acp_permission_rejects_paths_outside_workspace(make_settings, fake_bins, workspace: Path):
    settings = make_settings(opencode_mode="acp", opencode_bin=str(fake_bins["acp"]), opencode_acp_args="acp")
    registry = AgentRegistry(settings)
    inside = await registry.invoke(AgentName.OPENCODE, f"PERM|{workspace}/a.txt", None, None)
    outside = await registry.invoke(AgentName.OPENCODE, "PERM|/etc/passwd", None, None)
    assert inside.output == "perm=yes"
    assert outside.output == "perm=no"


async def test_acp_permission_allows_extra_paths_only(make_settings, fake_bins, tmp_path: Path):
    extra = tmp_path / "dot-agents"
    extra.mkdir()
    settings = make_settings(
        opencode_mode="acp",
        opencode_bin=str(fake_bins["acp"]),
        opencode_acp_args="acp",
        acp_permission_extra_paths=f"{extra},{tmp_path / 'missing'}",
    )
    registry = AgentRegistry(settings)
    allowed = await registry.invoke(AgentName.OPENCODE, f"PERM|{extra}/AGENTS.md", None, None)
    sibling = await registry.invoke(AgentName.OPENCODE, f"PERM|{tmp_path}/other.md", None, None)
    assert allowed.output == "perm=yes"
    assert sibling.output == "perm=no"
    with pytest.raises(WorkspaceError):
        await registry.invoke(AgentName.OPENCODE, "hi", str(extra), None)


async def test_acp_timeout(make_settings, fake_bins):
    settings = make_settings(
        opencode_mode="acp",
        opencode_bin=str(fake_bins["acp"]),
        opencode_acp_args="acp",
        subprocess_timeout_seconds=1,
    )
    result = await AgentRegistry(settings).invoke(AgentName.OPENCODE, "HANG", None, None)
    assert result.timed_out
    assert result.stop_reason == "cancelled"


async def test_discovery_reports_install_and_auth(make_settings):
    agents = {
        a["agent"]: a for a in await AgentRegistry(make_settings(copilot_bin="definitely-missing")).list_available()
    }
    assert agents["claude"]["installed"] and agents["claude"]["authenticated"] is True
    assert agents["claude"]["version"] == "fake 1.0"
    assert agents["copilot"]["installed"] is False
    assert agents["codex"]["installed"] and agents["codex"]["authenticated"] is False


CLAUDE_STUB = "-p /cost"  # stub probe; the real default is empty (no verified safe Claude listing)


async def test_capabilities_group_claude_tools_and_filter_copilot_config(make_settings, workspace: Path):
    registry = AgentRegistry(make_settings(claude_capabilities_args=CLAUDE_STUB))
    claude = await registry.capabilities(AgentName.CLAUDE, "sub", ask_agent=False)
    assert "self_report" not in claude
    assert claude["cwd"] == str(workspace / "sub")
    assert claude["mcp_servers"] == [
        {
            "name": "plugin:slack:slack",
            "status": "connected",
            "source": "plugin",
            "tools": ["slack_read_channel"],
            "allow_rule": "mcp__plugin_slack_slack__*",
        },
        {"name": "scout", "status": "pending", "source": "user", "tools": ["search"], "allow_rule": "mcp__scout__*"},
    ]
    assert claude["builtin_tools"] == ["Read"] and claude["other_mcp_tools"] == ["mcp__gone__x"]
    assert claude["skills"] == [{"name": "git"}] and claude["plugins"] == [
        {"name": "slack", "source": "slack@official"}
    ]
    assert any("pending" in note for note in claude["notes"])

    copilot = await registry.capabilities(AgentName.COPILOT, None, ask_agent="never")
    assert "FAKE_SECRET" not in json.dumps(copilot)
    assert copilot["mcp_servers"][0]["name"] == "slack" and copilot["mcp_servers"][0]["tool_filter"] == ["*"]
    assert copilot["skills"] == [{"name": "git", "description": "Git", "source": "inherited", "enabled": True}]

    with pytest.raises(WorkspaceError):
        await registry.capabilities(AgentName.CLAUDE, "/etc")


async def _no_model(monkeypatch, registry: AgentRegistry) -> None:
    async def boom(*_a, **_k):
        raise AssertionError("model call must not happen")

    monkeypatch.setattr(registry, "invoke", boom)


async def test_capabilities_claude_unverified_listing_runs_nothing_and_no_model(make_settings, monkeypatch):
    registry = AgentRegistry(make_settings())  # default claude_capabilities_args is empty
    await _no_model(monkeypatch, registry)
    report = await registry.capabilities(AgentName.CLAUDE, None)
    assert report["coverage"] == {"tools": "unknown", "skills": "unknown"}
    assert report["builtin_tools"] is None and report["skills"] is None and "self_report" not in report


async def test_capabilities_native_supported_never_calls_model(make_settings, monkeypatch):
    registry = AgentRegistry(make_settings(claude_capabilities_args=CLAUDE_STUB))
    await _no_model(monkeypatch, registry)
    report = await registry.capabilities(AgentName.CLAUDE, None)
    assert report["coverage"] == {"tools": "native", "skills": "native"}
    assert "self_report" not in report and report["builtin_tools"] == ["Read"]
    assert report["skills"] == [{"name": "git"}]


@pytest.mark.parametrize("variant", ["EMPTY", "EMPTY NO_SERVERS"])
async def test_capabilities_empty_claude_listing_is_authoritative(make_settings, monkeypatch, variant):
    registry = AgentRegistry(make_settings(claude_capabilities_args=f"{CLAUDE_STUB} {variant}"))
    await _no_model(monkeypatch, registry)
    report = await registry.capabilities(AgentName.CLAUDE, None)
    assert report["skills"] == [] and report["builtin_tools"] == []
    assert report["coverage"] == {"tools": "native", "skills": "native"}


@pytest.mark.parametrize("variant", ["NO_TOOLS", "NO_SKILLS", "NO_TOOLS NO_SKILLS"])
async def test_capabilities_missing_claude_field_is_unavailable_not_unsupported(make_settings, monkeypatch, variant):
    registry = AgentRegistry(make_settings(claude_capabilities_args=f"{CLAUDE_STUB} {variant}"))
    await _no_model(monkeypatch, registry)
    report = await registry.capabilities(AgentName.CLAUDE, None)
    assert "unavailable" in report["coverage"].values() and "self_report" not in report


async def test_capabilities_empty_native_copilot_result_is_authoritative(make_settings, monkeypatch):
    registry = AgentRegistry(make_settings(copilot_skill_list_args="skill list --json EMPTY"))
    await _no_model(monkeypatch, registry)
    report = await registry.capabilities(AgentName.COPILOT, None, ask_agent=False)
    assert report["skills"] == [] and report["coverage"]["skills"] == "native"


@pytest.mark.parametrize("probe_args", ["-p FAIL", "-p hello"])  # transient error, malformed (no init event)
@pytest.mark.parametrize("ask_agent", [True, False, "auto", "never"])
async def test_capabilities_failed_or_malformed_native_never_calls_model(
    make_settings, monkeypatch, probe_args, ask_agent
):
    registry = AgentRegistry(make_settings(claude_capabilities_args=probe_args))
    await _no_model(monkeypatch, registry)
    report = await registry.capabilities(AgentName.CLAUDE, None, ask_agent=ask_agent)
    assert report["skills"] is None and report["builtin_tools"] is None
    assert report["coverage"] == {"tools": "unavailable", "skills": "unavailable"}
    assert "self_report" not in report and "hello" not in json.dumps(report)


@pytest.mark.parametrize("variant", ["ERROR", "BAD"])
async def test_capabilities_copilot_partial_support_failed_skills(make_settings, monkeypatch, variant):
    """Skills probe failed (unavailable, no fallback); tools are verified unsupported (fallback allowed)."""
    registry = AgentRegistry(make_settings(copilot_skill_list_args=f"skill list --json {variant}"))
    off = await registry.capabilities(AgentName.COPILOT, None, ask_agent=False)
    assert off["coverage"] == {"tools": "unsupported", "skills": "unavailable"}
    assert off["skills"] is None and "SECRET_RAW" not in json.dumps(off) and "self_report" not in off
    on = await registry.capabilities(AgentName.COPILOT, None)
    assert on["self_report_reason"] == "cli_lacks_native_listing_for_tools"


@pytest.mark.parametrize("ask_agent", [False, "never"])
async def test_capabilities_false_never_calls_model(make_settings, monkeypatch, ask_agent):
    registry = AgentRegistry(make_settings())
    await _no_model(monkeypatch, registry)
    for agent in (AgentName.CODEX, AgentName.COPILOT, AgentName.OPENCODE):
        report = await registry.capabilities(agent, None, ask_agent=ask_agent)
        assert "self_report" not in report


async def test_capabilities_unsupported_falls_back_by_default(make_settings):
    registry = AgentRegistry(make_settings(capabilities_prompt="List your tools as JSON"))
    asked = await registry.capabilities(AgentName.CODEX, None, model="m")
    assert asked["coverage"] == {"tools": "unsupported", "skills": "unsupported"}
    # The fake CLI echoes its argv and stdin as JSON, which stands in for the agent's JSON answer.
    assert asked["self_report"]["ok"] and asked["self_report"]["report"]["stdin"] == "List your tools as JSON"
    assert asked["self_report_reason"] == "cli_lacks_native_listing_for_skills_and_tools"


async def test_capabilities_partial_support_opencode(make_settings):
    report = await AgentRegistry(make_settings()).capabilities(AgentName.OPENCODE, None, ask_agent=False)
    assert report["coverage"] == {"tools": "unsupported", "skills": "native"}
    assert report["skills"] == [{"name": "oc", "description": "OC"}]
    assert "SECRET_BODY" not in json.dumps(report)


async def test_capabilities_invalid_ask_agent_rejected(make_settings):
    with pytest.raises(InputError):
        await AgentRegistry(make_settings()).capabilities(AgentName.CODEX, None, ask_agent="always")  # type: ignore[arg-type]


async def test_capabilities_failed_self_report_hides_raw_output(make_settings):
    failed = await AgentRegistry(make_settings(capabilities_prompt="FAIL")).capabilities(AgentName.CODEX, None)
    assert failed["self_report"]["ok"] is False and failed["self_report"]["output"] is None
