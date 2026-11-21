import asyncio
import json
import os
import time
from pathlib import Path

import pytest

from gateway.adapters import AgentRegistry, build_cli_command, render_args, run_process
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
