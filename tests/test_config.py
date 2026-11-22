from pathlib import Path

import pytest
from pydantic import ValidationError

from gateway.config import (
    InputError,
    Settings,
    Workspace,
    WorkspaceError,
    validate_model,
)


def test_workspace_rejects_traversal_and_symlink_escape(workspace: Path, tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "link").symlink_to(outside)
    ws = Workspace([workspace])

    assert ws.resolve_dir(None) == workspace
    assert ws.resolve_dir("sub") == workspace / "sub"
    assert ws.resolve_dir(str(workspace / "sub" / "..")) == workspace
    for bad in [
        "..",
        str(workspace / ".." / "outside"),
        "/etc",
        "link",
        f"{workspace}-evil",
    ]:
        with pytest.raises(WorkspaceError):
            ws.resolve_dir(bad)


def test_workspace_rejects_missing_dir(workspace: Path):
    with pytest.raises(WorkspaceError):
        Workspace([workspace]).resolve_dir("nope")


def test_settings_parse_csv_env_and_require_key(monkeypatch: pytest.MonkeyPatch, workspace: Path):
    monkeypatch.setenv("GATEWAY_API_KEY", "k")
    monkeypatch.setenv("ALLOWED_WORKSPACES", f"{workspace}, {workspace / 'sub'}")
    monkeypatch.setenv("CLAUDE_PROXY_TOOLS", "Read,Bash")
    monkeypatch.setenv("OPENCODE_DEFAULT_MODEL", "")
    s = Settings(_env_file=None)
    assert s.workspace_roots() == [workspace, workspace / "sub"]
    assert s.claude_proxy_tools == ["Read", "Bash"]
    assert s.opencode_default_model is None

    monkeypatch.delenv("GATEWAY_API_KEY")
    with pytest.raises(ValidationError, match="GATEWAY_API_KEY"):
        Settings(_env_file=None)


def test_allowed_tools_parse_and_validate(monkeypatch: pytest.MonkeyPatch, make_settings):
    monkeypatch.setenv("GATEWAY_API_KEY", "k")
    monkeypatch.setenv("COPILOT_ALLOWED_TOOLS", "slack(slack_read_channel), glean-adobe")
    assert Settings(_env_file=None).copilot_allowed_tools == ["slack(slack_read_channel)", "glean-adobe"]
    with pytest.raises(ValidationError, match="Invalid tool rule"):
        make_settings(claude_allowed_tools=["--dangerously-skip-permissions"])
    with pytest.raises(ValidationError, match="CODEX_ALLOWED_TOOLS_ARGS"):
        make_settings(codex_allowed_tools=["x"])
    with pytest.raises(ValidationError, match="OPENCODE_ALLOW_ALL_TOOLS_ARGS"):
        make_settings(opencode_allow_all_tools=True)


@pytest.mark.parametrize("model", ["--dangerously-skip-permissions", "a b", "x;rm", "-m"])
def test_validate_model_rejects_flag_like_values(model: str):
    with pytest.raises(InputError):
        validate_model(model)


def test_validate_model_accepts_provider_models():
    assert validate_model(" anthropic/claude-sonnet-4.5 ") == "anthropic/claude-sonnet-4.5"
    assert validate_model("") is None
