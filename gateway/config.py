"""Gateway settings and workspace path validation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class AgentName(StrEnum):
    CLAUDE = "claude"
    COPILOT = "copilot"
    OPENCODE = "opencode"
    CODEX = "codex"


AgentMode = Literal["cli", "acp"]
# How to read CLI stdout: plain text, or JSON event streams that also carry the session id.
OutputFormat = Literal["text", "opencode_json", "codex_jsonl"]
PermissionPolicy = Literal["allow", "reject"]

# Model names are passed as argv values, so keep them to a conservative charset
# and never allow a leading dash (which would be parsed as a flag).
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+\-\[\]]{0,199}$")
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:\-]{0,127}$")


class WorkspaceError(ValueError):
    """Raised when a requested workspace path is invalid or disallowed."""


class InputError(ValueError):
    """Raised when a tool argument is invalid."""


def _split_csv(value: object) -> object:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


CsvList = Annotated[list[str], NoDecode]


@dataclass(frozen=True, slots=True)
class AgentConfig:
    """Per-agent launch configuration derived from Settings."""

    name: AgentName
    bin: str
    mode: AgentMode
    args: str
    resume_args: str
    new_session_args: str
    output_format: OutputFormat
    model_args: str
    default_model: str | None
    allowed_tools: tuple[str, ...]
    allowed_tools_args: str
    allow_all_tools: bool
    allow_all_tools_args: str
    acp_args: str
    version_args: str
    auth_check_args: str
    max_concurrency: int


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Server
    host: str = "127.0.0.1"
    port: int = 8765
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    gateway_api_key: SecretStr = SecretStr("")
    allow_no_auth: bool = False
    # Host/Origin allow lists for MCP DNS-rebinding protection. Empty disables the
    # check, which is fine behind bearer auth and a tunnel with a random hostname.
    allowed_hosts: CsvList = Field(default_factory=list)
    allowed_origins: CsvList = Field(default_factory=list)

    # Workspace allowlist
    allowed_workspaces: CsvList = Field(default_factory=list)

    # Execution limits
    subprocess_timeout_seconds: float = Field(default=300, gt=0)
    max_output_bytes: int = Field(default=1_000_000, gt=0)
    max_prompt_chars: int = Field(default=200_000, gt=0)
    max_concurrency_per_agent: int = Field(default=2, ge=1)

    # Binaries
    claude_bin: str = "claude"
    copilot_bin: str = "copilot"
    opencode_bin: str = "opencode"
    codex_bin: str = "codex"

    # Mode per agent. "acp" talks Agent Client Protocol over stdio.
    claude_mode: AgentMode = "cli"
    copilot_mode: AgentMode = "cli"
    opencode_mode: AgentMode = "cli"
    codex_mode: AgentMode = "cli"

    # Command templates (shlex syntax). Placeholders: {prompt}, {cwd}, {model}, {session_id}.
    # Without {prompt}, the prompt is written to the process stdin instead.
    # *_ARGS starts a new session, *_RESUME_ARGS replaces it when continuing one.
    claude_args: str = "-p --output-format text --permission-mode acceptEdits"
    copilot_args: str = "--prompt={prompt} --silent --no-color --no-ask-user --allow-tool write"
    opencode_args: str = "run --format json {prompt}"
    # Prompt via stdin ("-"). workspace-write lets Codex edit files only inside cwd.
    codex_args: str = "exec --json --skip-git-repo-check --sandbox workspace-write --color never -"

    claude_resume_args: str = "-p --output-format text --permission-mode acceptEdits --resume {session_id}"
    copilot_resume_args: str = (
        "--prompt={prompt} --silent --no-color --no-ask-user --allow-tool write --resume={session_id}"
    )
    opencode_resume_args: str = "run --format json --session {session_id} {prompt}"
    # `codex exec resume` has no --sandbox flag, so set the sandbox through config.
    codex_resume_args: str = "exec resume --json --skip-git-repo-check -c sandbox_mode=workspace-write {session_id} -"

    # Appended to *_ARGS for new sessions when the CLI accepts a caller-chosen id.
    # The gateway generates a UUID. Leave empty to read the id from the output instead.
    claude_new_session_args: str = "--session-id {session_id}"
    copilot_new_session_args: str = "--session-id={session_id}"
    opencode_new_session_args: str = ""
    codex_new_session_args: str = ""

    claude_output_format: OutputFormat = "text"
    copilot_output_format: OutputFormat = "text"
    opencode_output_format: OutputFormat = "opencode_json"
    codex_output_format: OutputFormat = "codex_jsonl"

    claude_model_args: str = "--model {model}"
    copilot_model_args: str = "--model {model}"
    opencode_model_args: str = "-m {model}"
    codex_model_args: str = "-m {model}"

    claude_default_model: str | None = None
    copilot_default_model: str | None = None
    opencode_default_model: str | None = None
    codex_default_model: str | None = None

    # Tool permission rules, passed to the CLI verbatim (wildcards are the CLI's own syntax).
    # *_ALLOWED_TOOLS_ARGS is rendered once per rule with {tool} and appended in CLI mode.
    claude_allowed_tools: CsvList = Field(default_factory=list)
    copilot_allowed_tools: CsvList = Field(default_factory=list)
    opencode_allowed_tools: CsvList = Field(default_factory=list)
    codex_allowed_tools: CsvList = Field(default_factory=list)

    claude_allowed_tools_args: str = "--allowedTools={tool}"
    copilot_allowed_tools_args: str = "--allow-tool={tool}"
    opencode_allowed_tools_args: str = ""
    codex_allowed_tools_args: str = ""

    # <AGENT>_ALLOW_ALL_TOOLS=true appends *_ALLOW_ALL_TOOLS_ARGS in CLI mode: every tool, shell
    # included, runs without approval. Remote callers with the API key get that power on this host.
    claude_allow_all_tools: bool = False
    copilot_allow_all_tools: bool = False
    opencode_allow_all_tools: bool = False
    codex_allow_all_tools: bool = False

    claude_allow_all_tools_args: str = "--dangerously-skip-permissions"
    copilot_allow_all_tools_args: str = "--allow-all-tools"
    opencode_allow_all_tools_args: str = ""
    codex_allow_all_tools_args: str = ""

    claude_acp_args: str = ""
    copilot_acp_args: str = "--acp"
    opencode_acp_args: str = "acp --cwd {cwd}"
    # Codex has no built-in ACP server. Use an adapter such as codex-acp via CODEX_BIN.
    codex_acp_args: str = ""

    claude_interactive_bin: str = "claude-agent-acp"
    codex_interactive_bin: str = "codex-acp"

    claude_version_args: str = "--version"
    copilot_version_args: str = "--version"
    opencode_version_args: str = "--version"
    codex_version_args: str = "--version"

    claude_auth_check_args: str = "auth status"
    copilot_auth_check_args: str = ""
    opencode_auth_check_args: str = "auth list"
    codex_auth_check_args: str = "login status"

    acp_permission_policy: PermissionPolicy = "allow"
    # How long an interactive turn waits for a human approval. The run timeout pauses meanwhile.
    interactive_approval_timeout_seconds: float = Field(default=900, gt=0)
    # Interactive turns ask the human about paths outside ALLOWED_WORKSPACES and ACP_PERMISSION_EXTRA_PATHS
    # (with a warning) instead of rejecting them outright.
    interactive_approve_outside_paths: bool = False
    # Extra directories ACP permission requests may touch besides ALLOWED_WORKSPACES (e.g. ~/.claude).
    # They never widen working_dir or session cwd checks.
    acp_permission_extra_paths: CsvList = Field(default_factory=list)

    # Native session stores, used by list_sessions and to validate resumed sessions.
    claude_projects_dir: str = "~/.claude/projects"
    copilot_session_state_dir: str = "~/.copilot/session-state"
    codex_sessions_dir: str = "~/.codex/sessions"
    codex_session_index: str = "~/.codex/session_index.jsonl"
    opencode_session_list_args: str = "session list --format json -n 1000"

    # list_agent_capabilities probes. They must not start a model turn.
    # Claude has no documented metadata-only listing (`claude mcp list` health-checks connectors and
    # `-p /cost` relies on undocumented behavior and can start connectors), so discovery is off by default.
    # Operators who verified a safe command can set it; its stream-json init event is parsed.
    claude_capabilities_args: str = ""
    copilot_mcp_list_args: str = "mcp list --json"
    copilot_skill_list_args: str = "skill list --json"
    codex_mcp_list_args: str = "mcp list --json"
    opencode_skill_list_args: str = "debug skill"
    # Sent when list_agent_capabilities requests a self-report (one billable model turn).
    capabilities_prompt: str = (
        "Describe your own capabilities in this environment. Do not call any tools, do not read or change "
        "files, and do not contact any service; answer from what you already know about your configuration. "
        "Reply with only a JSON object with these keys: summary (string), builtin_tools (array of tool names), "
        "mcp_servers (array of objects with name and tools, an array of tool names), skills (array of objects "
        "with name and description), other_capabilities (array of strings), limitations (array of strings)."
    )

    # Claude `mcp serve` proxy
    claude_mcp_proxy_enabled: bool = True
    claude_mcp_serve_args: str = "mcp serve"
    claude_proxy_tool_prefix: str = "claude_"
    # "*" exposes every tool claude reports. Bash is powerful, so it is opt-in by name.
    claude_proxy_tools: CsvList = Field(
        default_factory=lambda: [
            "Read",
            "Edit",
            "Write",
            "NotebookEdit",
            "WebFetch",
            "WebSearch",
        ]
    )

    @field_validator(
        "allowed_hosts",
        "allowed_origins",
        "allowed_workspaces",
        "claude_proxy_tools",
        "claude_allowed_tools",
        "copilot_allowed_tools",
        "opencode_allowed_tools",
        "codex_allowed_tools",
        "acp_permission_extra_paths",
        mode="before",
    )
    @classmethod
    def _split(cls, value: object) -> object:
        return _split_csv(value)

    @field_validator(
        "claude_allowed_tools",
        "copilot_allowed_tools",
        "opencode_allowed_tools",
        "codex_allowed_tools",
    )
    @classmethod
    def _check_tool_rules(cls, value: list[str]) -> list[str]:
        for rule in value:
            if rule.startswith("-") or "\x00" in rule:
                raise ValueError(f"Invalid tool rule: {rule!r}")
        return value

    @field_validator(
        "claude_default_model",
        "copilot_default_model",
        "opencode_default_model",
        "codex_default_model",
        mode="before",
    )
    @classmethod
    def _empty_to_none(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @model_validator(mode="after")
    def _check_auth(self) -> Settings:
        if not self.gateway_api_key.get_secret_value() and not self.allow_no_auth:
            raise ValueError("GATEWAY_API_KEY is required (set ALLOW_NO_AUTH=true only for local development)")
        for name in AgentName:
            template = getattr(self, f"{name.value}_allowed_tools_args")
            if getattr(self, f"{name.value}_allowed_tools") and "{tool}" not in template:
                key = name.value.upper()
                raise ValueError(f"{key}_ALLOWED_TOOLS needs {key}_ALLOWED_TOOLS_ARGS with a {{tool}} placeholder")
            if (
                getattr(self, f"{name.value}_allow_all_tools")
                and not getattr(self, f"{name.value}_allow_all_tools_args").strip()
            ):
                key = name.value.upper()
                raise ValueError(f"{key}_ALLOW_ALL_TOOLS needs {key}_ALLOW_ALL_TOOLS_ARGS")
        return self

    def workspace_roots(self) -> list[Path]:
        """Resolved allowed workspace roots. Defaults to the current directory."""
        raw = self.allowed_workspaces or ["."]
        roots: list[Path] = []
        for item in raw:
            path = Path(item).expanduser().resolve()
            if not path.is_dir():
                raise WorkspaceError(f"Allowed workspace does not exist or is not a directory: {path}")
            roots.append(path)
        return roots

    def acp_permission_roots(self) -> list[Path]:
        """Resolved extra ACP permission roots. Missing directories are skipped."""
        roots = (Path(item).expanduser().resolve() for item in self.acp_permission_extra_paths)
        return [path for path in roots if path.is_dir()]

    def agent(self, name: AgentName) -> AgentConfig:
        prefix = name.value
        return AgentConfig(
            name=name,
            bin=getattr(self, f"{prefix}_bin"),
            mode=getattr(self, f"{prefix}_mode"),
            args=getattr(self, f"{prefix}_args"),
            resume_args=getattr(self, f"{prefix}_resume_args"),
            new_session_args=getattr(self, f"{prefix}_new_session_args"),
            output_format=getattr(self, f"{prefix}_output_format"),
            model_args=getattr(self, f"{prefix}_model_args"),
            default_model=getattr(self, f"{prefix}_default_model"),
            allowed_tools=tuple(getattr(self, f"{prefix}_allowed_tools")),
            allowed_tools_args=getattr(self, f"{prefix}_allowed_tools_args"),
            allow_all_tools=getattr(self, f"{prefix}_allow_all_tools"),
            allow_all_tools_args=getattr(self, f"{prefix}_allow_all_tools_args"),
            acp_args=getattr(self, f"{prefix}_acp_args"),
            version_args=getattr(self, f"{prefix}_version_args"),
            auth_check_args=getattr(self, f"{prefix}_auth_check_args"),
            max_concurrency=self.max_concurrency_per_agent,
        )


class Workspace:
    """Validates paths against the allowed workspace roots."""

    def __init__(self, roots: list[Path]):
        if not roots:
            raise WorkspaceError("At least one allowed workspace is required")
        self.roots = roots

    @property
    def default(self) -> Path:
        return self.roots[0]

    def is_allowed(self, path: Path) -> bool:
        return any(path.is_relative_to(root) for root in self.roots)

    def resolve_dir(self, working_dir: str | None) -> Path:
        """Resolve a working directory. It must exist and sit inside an allowed root."""
        path = self.resolve_path(working_dir) if working_dir else self.default
        if not path.is_dir():
            raise WorkspaceError(f"working_dir is not an existing directory: {working_dir}")
        return path

    def resolve_path(self, raw: str, base: Path | None = None) -> Path:
        """Resolve a file path (symlinks included) and enforce the allowlist."""
        if "\x00" in raw:
            raise WorkspaceError("Path contains a NUL byte")
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = (base or self.default) / path
        resolved = path.resolve()
        if not self.is_allowed(resolved):
            raise WorkspaceError(f"Path is outside the allowed workspaces: {raw}")
        return resolved


def validate_prompt(prompt: str, max_chars: int) -> str:
    if not prompt or not prompt.strip():
        raise InputError("prompt must not be empty")
    if len(prompt) > max_chars:
        raise InputError(f"prompt exceeds {max_chars} characters")
    return prompt.replace("\x00", "")


def validate_session_id(session_id: str | None) -> str | None:
    if session_id is None or not session_id.strip():
        return None
    session_id = session_id.strip()
    if not SESSION_ID_RE.match(session_id):
        raise InputError(f"Invalid session_id: {session_id!r}")
    return session_id


def validate_model(model: str | None) -> str | None:
    if model is None or not model.strip():
        return None
    model = model.strip()
    if not _MODEL_RE.match(model):
        raise InputError(f"Invalid model name: {model!r}")
    return model


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
