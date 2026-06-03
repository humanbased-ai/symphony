from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from os import PathLike
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


DEFAULT_LINEAR_ENDPOINT = "https://api.linear.app/graphql"
DEFAULT_ACTIVE_STATES = ("Todo", "In Progress")
DEFAULT_TERMINAL_STATES = ("Closed", "Cancelled", "Canceled", "Duplicate", "Done")
DEFAULT_POLLING_INTERVAL_MS = 30_000
DEFAULT_HOOK_TIMEOUT_MS = 60_000
DEFAULT_MAX_CONCURRENT_AGENTS = 10
DEFAULT_MAX_TURNS = 20
DEFAULT_MAX_RETRY_BACKOFF_MS = 300_000
DEFAULT_DISPATCH_STAGGER_MS = 0
DEFAULT_CODEX_COMMAND = "codex app-server"
DEFAULT_CODEX_TURN_TIMEOUT_MS = 3_600_000
DEFAULT_CODEX_READ_TIMEOUT_MS = 5_000
DEFAULT_CODEX_STALL_TIMEOUT_MS = 300_000
DEFAULT_CLAUDE_COMMAND = "claude"
DEFAULT_CLAUDE_TURN_TIMEOUT_MS = 3_600_000
DEFAULT_CLAUDE_PERMISSION_MODE = "bypassPermissions"
DEFAULT_MAX_PR_TURNS = 10
DEFAULT_ACCEPTANCE_REVIEW_SOURCE = "auto"
DEFAULT_ACCEPTANCE_CONFIDENCE_THRESHOLD = 0.8
DEFAULT_ACCEPTANCE_QUIET_PERIOD_SECONDS = 300
# How long ``auto`` mode holds open for crosscheck before falling through to
# the silent branch. crosscheck typically takes several minutes between PR
# open and posting its ``VERDICT:`` comment; without a grace window the
# silent branch can fire first and judge before code review has happened.
DEFAULT_ACCEPTANCE_CROSSCHECK_WAIT_SECONDS = 1_200  # 20 minutes
# vf step does a full checkout + real probe execution; 15 minutes covers
# clone + setup + probes for typical repos without letting a hung run
# stall the poll loop forever.
DEFAULT_VERIFYFLOW_TIMEOUT_SECONDS = 900
DEFAULT_ACCEPTANCE_GUARD_PATHS = (
    "SPEC.md",
    "**/migrations/**",
    ".github/**",
    "**/secrets/**",
    "*.pem",
    "*.key",
)
ACCEPTANCE_REVIEW_SOURCES = ("auto", "crosscheck", "none")


class ConfigError(ValueError):
    """Raised when runtime config is missing or unsupported."""


DEFAULT_REVIEW_STATE = "In Review"


@dataclass(frozen=True)
class TrackerConfig:
    kind: str
    endpoint: str = DEFAULT_LINEAR_ENDPOINT
    api_key: str | None = None
    project_slug: str | None = None
    active_states: tuple[str, ...] = field(default_factory=lambda: DEFAULT_ACTIVE_STATES)
    terminal_states: tuple[str, ...] = field(default_factory=lambda: DEFAULT_TERMINAL_STATES)
    review_state: str = DEFAULT_REVIEW_STATE
    done_state: str = "Done"
    cancelled_state: str = "Canceled"

    @classmethod
    def from_mapping(cls, config: Mapping[str, Any]) -> "TrackerConfig":
        tracker = config.get("tracker", {})
        if tracker is None:
            tracker = {}
        if not isinstance(tracker, Mapping):
            raise ConfigError("tracker_config_must_be_map")

        kind = _string_value(tracker.get("kind")) or "linear"
        if kind != "linear":
            raise ConfigError("unsupported_tracker_kind")

        return cls(
            kind=kind,
            endpoint=_string_value(tracker.get("endpoint")) or DEFAULT_LINEAR_ENDPOINT,
            api_key=_string_value(tracker.get("api_key")),
            project_slug=_normalize_linear_slug(_string_value(tracker.get("project_slug"))),
            active_states=_string_tuple(tracker.get("active_states"), DEFAULT_ACTIVE_STATES),
            terminal_states=_string_tuple(tracker.get("terminal_states"), DEFAULT_TERMINAL_STATES),
            review_state=_string_value(tracker.get("review_state")) or DEFAULT_REVIEW_STATE,
            done_state=_string_value(tracker.get("done_state")) or "Done",
            cancelled_state=_string_value(tracker.get("cancelled_state")) or "Canceled",
        )


@dataclass(frozen=True)
class PollingConfig:
    interval_ms: int = DEFAULT_POLLING_INTERVAL_MS

    @classmethod
    def from_mapping(cls, config: Mapping[str, Any]) -> "PollingConfig":
        polling = _mapping(config.get("polling"), "polling_config_must_be_map")
        return cls(
            interval_ms=_positive_int(
                polling.get("interval_ms"),
                DEFAULT_POLLING_INTERVAL_MS,
                "polling_interval_ms",
            )
        )


@dataclass(frozen=True)
class WorkspaceConfig:
    root: Path
    repo_url: str | None = None
    default_branch: str = "main"
    branch_prefix: str = ""

    @classmethod
    def from_mapping(
        cls,
        config: Mapping[str, Any],
        *,
        workflow_dir: str | Path | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> "WorkspaceConfig":
        workspace = _mapping(config.get("workspace"), "workspace_config_must_be_map")
        configured_root = _string_value(workspace.get("root")) or str(
            Path(tempfile.gettempdir()) / "symphony_workspaces"
        )
        root = _resolve_path(configured_root, workflow_dir=workflow_dir, environ=environ)
        raw_repo_url = _string_value(workspace.get("repo_url"))
        repo_url = resolve_env_reference(raw_repo_url, environ) if raw_repo_url is not None else None
        default_branch = _string_value(workspace.get("default_branch")) or "main"
        branch_prefix = _string_value(workspace.get("branch_prefix")) or ""
        return cls(
            root=root,
            repo_url=repo_url,
            default_branch=default_branch,
            branch_prefix=branch_prefix,
        )


@dataclass(frozen=True)
class HooksConfig:
    after_create: str | None = None
    before_run: str | None = None
    after_run: str | None = None
    before_remove: str | None = None
    timeout_ms: int = DEFAULT_HOOK_TIMEOUT_MS

    @classmethod
    def from_mapping(cls, config: Mapping[str, Any]) -> "HooksConfig":
        hooks = _mapping(config.get("hooks"), "hooks_config_must_be_map")
        return cls(
            after_create=_string_value(hooks.get("after_create")),
            before_run=_string_value(hooks.get("before_run")),
            after_run=_string_value(hooks.get("after_run")),
            before_remove=_string_value(hooks.get("before_remove")),
            timeout_ms=_positive_int(hooks.get("timeout_ms"), DEFAULT_HOOK_TIMEOUT_MS, "hooks_timeout_ms"),
        )


@dataclass(frozen=True)
class AgentConfig:
    max_concurrent_agents: int = DEFAULT_MAX_CONCURRENT_AGENTS
    max_turns: int = DEFAULT_MAX_TURNS
    max_retry_backoff_ms: int = DEFAULT_MAX_RETRY_BACKOFF_MS
    max_concurrent_agents_by_state: Mapping[str, int] = field(default_factory=lambda: MappingProxyType({}))
    runner: str = "codex"
    dispatch_stagger_ms: int = DEFAULT_DISPATCH_STAGGER_MS

    @classmethod
    def from_mapping(cls, config: Mapping[str, Any]) -> "AgentConfig":
        agent = _mapping(config.get("agent"), "agent_config_must_be_map")
        runner = _string_value(agent.get("runner")) or "codex"
        if runner not in ("codex", "claude_code"):
            raise ConfigError(f"unsupported_agent_runner:{runner}")
        return cls(
            max_concurrent_agents=_positive_int(
                agent.get("max_concurrent_agents"), DEFAULT_MAX_CONCURRENT_AGENTS, "agent_max_concurrent_agents"
            ),
            max_turns=_positive_int(agent.get("max_turns"), DEFAULT_MAX_TURNS, "agent_max_turns"),
            max_retry_backoff_ms=_positive_int(
                agent.get("max_retry_backoff_ms"), DEFAULT_MAX_RETRY_BACKOFF_MS, "agent_max_retry_backoff_ms"
            ),
            max_concurrent_agents_by_state=_state_limit_map(agent.get("max_concurrent_agents_by_state")),
            runner=runner,
            dispatch_stagger_ms=_non_negative_int(
                agent.get("dispatch_stagger_ms"), DEFAULT_DISPATCH_STAGGER_MS, "agent_dispatch_stagger_ms"
            ),
        )


@dataclass(frozen=True)
class CodexConfig:
    command: str = DEFAULT_CODEX_COMMAND
    approval_policy: str | None = None
    thread_sandbox: str | None = None
    turn_sandbox_policy: str | None = None
    turn_timeout_ms: int = DEFAULT_CODEX_TURN_TIMEOUT_MS
    read_timeout_ms: int = DEFAULT_CODEX_READ_TIMEOUT_MS
    stall_timeout_ms: int = DEFAULT_CODEX_STALL_TIMEOUT_MS

    @classmethod
    def from_mapping(cls, config: Mapping[str, Any]) -> "CodexConfig":
        codex = _mapping(config.get("codex"), "codex_config_must_be_map")
        return cls(
            command=_string_value(codex.get("command")) or DEFAULT_CODEX_COMMAND,
            approval_policy=_string_value(codex.get("approval_policy")),
            thread_sandbox=_string_value(codex.get("thread_sandbox")),
            turn_sandbox_policy=_string_value(codex.get("turn_sandbox_policy")),
            turn_timeout_ms=_positive_int(
                codex.get("turn_timeout_ms"),
                DEFAULT_CODEX_TURN_TIMEOUT_MS,
                "codex_turn_timeout_ms",
            ),
            read_timeout_ms=_positive_int(
                codex.get("read_timeout_ms"),
                DEFAULT_CODEX_READ_TIMEOUT_MS,
                "codex_read_timeout_ms",
            ),
            stall_timeout_ms=_positive_int(
                codex.get("stall_timeout_ms"),
                DEFAULT_CODEX_STALL_TIMEOUT_MS,
                "codex_stall_timeout_ms",
            ),
        )


@dataclass(frozen=True)
class ClaudeCodeConfig:
    command: str = DEFAULT_CLAUDE_COMMAND
    model: str | None = None
    permission_mode: str = DEFAULT_CLAUDE_PERMISSION_MODE
    turn_timeout_ms: int = DEFAULT_CLAUDE_TURN_TIMEOUT_MS

    @classmethod
    def from_mapping(cls, config: Mapping[str, Any]) -> "ClaudeCodeConfig":
        claude = _mapping(config.get("claude_code"), "claude_code_config_must_be_map")
        return cls(
            command=_string_value(claude.get("command")) or DEFAULT_CLAUDE_COMMAND,
            model=_string_value(claude.get("model")),
            permission_mode=_string_value(claude.get("permission_mode")) or DEFAULT_CLAUDE_PERMISSION_MODE,
            turn_timeout_ms=_positive_int(
                claude.get("turn_timeout_ms"),
                DEFAULT_CLAUDE_TURN_TIMEOUT_MS,
                "claude_code_turn_timeout_ms",
            ),
        )


@dataclass(frozen=True)
class GitHubConfig:
    """Optional GitHub integration for the PR comment polling feedback loop.

    Supports $VAR syntax for environment variable resolution.

    Example WORKFLOW.md / config section::

        github:
          token: $GITHUB_TOKEN
          owner: myorg
          repo: myrepo
          max_pr_turns: 10
    """

    token: str | None = None
    owner: str | None = None
    repo: str | None = None
    max_pr_turns: int = DEFAULT_MAX_PR_TURNS

    @classmethod
    def from_mapping(
        cls,
        config: Mapping[str, Any],
        *,
        environ: Mapping[str, str] | None = None,
    ) -> "GitHubConfig":
        github = _mapping(config.get("github"), "github_config_must_be_map")
        raw_token = _string_value(github.get("token"))
        token: str | None = None
        if raw_token is not None:
            try:
                token = resolve_env_reference(raw_token, environ)
            except ConfigError:
                pass  # env var not set; runtime resolves token from credentials file
        return cls(
            token=token,
            owner=_string_value(github.get("owner")),
            repo=_string_value(github.get("repo")),
            max_pr_turns=_positive_int(github.get("max_pr_turns"), DEFAULT_MAX_PR_TURNS, "github_max_pr_turns"),
        )


@dataclass(frozen=True)
class AcceptanceConfig:
    """Optional final-acceptance gate.

    The acceptance agent re-checks a converged PR against the original issue
    ("did it do the right thing"), distinct from code review ("is the code
    good"). It is pluggable on the source of the convergence signal:

    - ``review_source: auto``      use crosscheck's VERDICT when a ``[crosscheck]``
                                    comment is present, otherwise fall back to the
                                    silent-runtime signal.
    - ``review_source: crosscheck`` require a crosscheck ``VERDICT: APPROVE``.
    - ``review_source: none``       no external reviewer; converge on Symphony
                                    going quiet (no new feedback, CI green, quiet
                                    period elapsed).

    Disabled by default. ``auto_merge`` stays ``False`` in Phase 1 — the gate
    only judges and escalates to a human; it never merges.

    Example WORKFLOW.md / config section::

        acceptance:
          enabled: true
          review_source: auto
          vendor: claude_code
          auto_merge: false
          confidence_threshold: 0.8
          quiet_period_seconds: 300
          guard_paths:
            - SPEC.md
            - "**/migrations/**"
    """

    enabled: bool = False
    review_source: str = DEFAULT_ACCEPTANCE_REVIEW_SOURCE
    vendor: str | None = None
    auto_merge: bool = False
    # When the judge returns ``fail``, ``bounce_back_on_fail=True`` forwards
    # the unmet checks through ``_handle_pr_feedback`` so the implementer
    # gets another turn (bounded by ``max_pr_turns``). Default OFF — early
    # rollouts keep "judge → comment → human decides" semantics so an
    # operator confirms each failed verdict before the loop spends agent
    # budget on a retry. Flip to True once verdict quality is trusted.
    bounce_back_on_fail: bool = False
    confidence_threshold: float = DEFAULT_ACCEPTANCE_CONFIDENCE_THRESHOLD
    quiet_period_seconds: int = DEFAULT_ACCEPTANCE_QUIET_PERIOD_SECONDS
    crosscheck_wait_seconds: int = DEFAULT_ACCEPTANCE_CROSSCHECK_WAIT_SECONDS
    guard_paths: tuple[str, ...] = DEFAULT_ACCEPTANCE_GUARD_PATHS

    @classmethod
    def from_mapping(cls, config: Mapping[str, Any]) -> "AcceptanceConfig":
        acceptance = _mapping(config.get("acceptance"), "acceptance_config_must_be_map")
        review_source = _string_value(acceptance.get("review_source")) or DEFAULT_ACCEPTANCE_REVIEW_SOURCE
        if review_source not in ACCEPTANCE_REVIEW_SOURCES:
            raise ConfigError(f"unsupported_acceptance_review_source:{review_source}")
        threshold = _ratio_value(
            acceptance.get("confidence_threshold"),
            DEFAULT_ACCEPTANCE_CONFIDENCE_THRESHOLD,
            "acceptance_confidence_threshold",
        )
        return cls(
            enabled=_bool_value(acceptance.get("enabled"), False, "acceptance_enabled"),
            review_source=review_source,
            vendor=_string_value(acceptance.get("vendor")),
            auto_merge=_bool_value(acceptance.get("auto_merge"), False, "acceptance_auto_merge"),
            bounce_back_on_fail=_bool_value(
                acceptance.get("bounce_back_on_fail"), False, "acceptance_bounce_back_on_fail",
            ),
            confidence_threshold=threshold,
            quiet_period_seconds=_non_negative_int(
                acceptance.get("quiet_period_seconds"),
                DEFAULT_ACCEPTANCE_QUIET_PERIOD_SECONDS,
                "acceptance_quiet_period_seconds",
            ),
            crosscheck_wait_seconds=_non_negative_int(
                acceptance.get("crosscheck_wait_seconds"),
                DEFAULT_ACCEPTANCE_CROSSCHECK_WAIT_SECONDS,
                "acceptance_crosscheck_wait_seconds",
            ),
            guard_paths=_string_tuple(acceptance.get("guard_paths"), DEFAULT_ACCEPTANCE_GUARD_PATHS),
        )


@dataclass(frozen=True)
class VerifyflowConfig:
    """Optional post-Crosscheck delivery-verification step (VerifyFlow, IN-569).

    Phase 1 is **advisory-only**: after Crosscheck approves a PR's current
    head, Symphony spawns ``vf step --pr <url>`` once per head SHA. VerifyFlow
    checks out the PR, really executes probes against the acceptance criteria
    of the linked Linear issue, keeps the evidence, and posts/updates its
    delivery-report comment on the PR. Symphony only logs the JSON result —
    it never merges, never blocks, and never transitions Linear state on it.

    Independent of the ``acceptance`` subsystem (which stays disabled).

    Example WORKFLOW.md / config section::

        verifyflow:
          enabled: true
          command: vf          # binary on PATH
          level: functional
          timeout_seconds: 900
    """

    enabled: bool = False
    command: str = "vf"
    level: str = "functional"
    timeout_seconds: int = DEFAULT_VERIFYFLOW_TIMEOUT_SECONDS

    @classmethod
    def from_mapping(cls, config: Mapping[str, Any]) -> "VerifyflowConfig":
        verifyflow = _mapping(config.get("verifyflow"), "verifyflow_config_must_be_map")
        return cls(
            enabled=_bool_value(verifyflow.get("enabled"), False, "verifyflow_enabled"),
            command=_string_value(verifyflow.get("command")) or "vf",
            level=_string_value(verifyflow.get("level")) or "functional",
            timeout_seconds=_positive_int(
                verifyflow.get("timeout_seconds"),
                DEFAULT_VERIFYFLOW_TIMEOUT_SECONDS,
                "verifyflow_timeout_seconds",
            ),
        )


@dataclass(frozen=True)
class WebhookConfig:
    """Optional webhook configuration for receiving Linear events via HTTP push.

    Supports $VAR syntax for environment variable resolution.

    Example WORKFLOW.md / config section::

        webhook:
          secret: $LINEAR_WEBHOOK_SECRET
          url: https://example.com/api/v1/webhooks/linear   # optional, enables auto-register
          team_id: abc123                                    # required for auto-register
    """

    secret: str | None = None
    url: str | None = None
    team_id: str | None = None

    @classmethod
    def from_mapping(
        cls,
        config: Mapping[str, Any],
        *,
        environ: Mapping[str, str] | None = None,
    ) -> "WebhookConfig":
        webhook = _mapping(config.get("webhook"), "webhook_config_must_be_map")
        raw_secret = _string_value(webhook.get("secret"))
        secret: str | None = None
        if raw_secret is not None:
            # Let ConfigError propagate — a missing env var for webhook.secret is a
            # misconfiguration, not a soft fallback. Silently setting secret=None
            # would disable the webhook route while startup succeeds, leaving
            # operators on polling without any error surfaced.
            secret = resolve_env_reference(raw_secret, environ)
        return cls(
            secret=secret,
            url=_string_value(webhook.get("url")),
            team_id=_string_value(webhook.get("team_id")),
        )


@dataclass(frozen=True)
class WorkflowConfig:
    tracker: TrackerConfig
    polling: PollingConfig = field(default_factory=PollingConfig)
    workspace: WorkspaceConfig = field(
        default_factory=lambda: WorkspaceConfig(Path(tempfile.gettempdir()) / "symphony_workspaces")
    )
    hooks: HooksConfig = field(default_factory=HooksConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    codex: CodexConfig = field(default_factory=CodexConfig)
    claude_code: ClaudeCodeConfig = field(default_factory=ClaudeCodeConfig)
    webhook: WebhookConfig = field(default_factory=WebhookConfig)
    github: GitHubConfig = field(default_factory=GitHubConfig)
    acceptance: AcceptanceConfig = field(default_factory=AcceptanceConfig)
    verifyflow: VerifyflowConfig = field(default_factory=VerifyflowConfig)

    @classmethod
    def from_mapping(
        cls,
        config: Mapping[str, Any],
        *,
        workflow_path: str | Path | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> "WorkflowConfig":
        workflow_dir = Path(workflow_path).expanduser().resolve().parent if workflow_path is not None else None
        return cls(
            tracker=TrackerConfig.from_mapping(config),
            polling=PollingConfig.from_mapping(config),
            workspace=WorkspaceConfig.from_mapping(config, workflow_dir=workflow_dir, environ=environ),
            hooks=HooksConfig.from_mapping(config),
            agent=AgentConfig.from_mapping(config),
            codex=CodexConfig.from_mapping(config),
            claude_code=ClaudeCodeConfig.from_mapping(config),
            webhook=WebhookConfig.from_mapping(config, environ=environ),
            github=GitHubConfig.from_mapping(config, environ=environ),
            acceptance=AcceptanceConfig.from_mapping(config),
            verifyflow=VerifyflowConfig.from_mapping(config),
        )


def _normalize_linear_slug(value: str | None) -> str | None:
    """Extract the slugId from a full Linear project URL, or return value as-is."""
    import re
    if value is None:
        return None
    v = value.strip().rstrip("/")
    m = re.match(r"https?://linear\.app/[^/]+/project/([^/]+)", v, re.IGNORECASE)
    return m.group(1) if m else v


def resolve_env_reference(value: str, environ: Mapping[str, str] | None = None) -> str:
    if not value.startswith("$"):
        return value

    env = environ if environ is not None else os.environ
    var_name = value[1:]
    result = env.get(var_name)
    if result is None:
        raise ConfigError(f"env_var_not_set:{var_name}")
    return result


def _string_value(value: Any) -> str | None:
    if isinstance(value, str):
        trimmed = value.strip()
        return trimmed or None
    if isinstance(value, PathLike):
        return str(value)
    return None


def _string_tuple(value: Any, default: tuple[str, ...]) -> tuple[str, ...]:
    if value is None:
        return default
    if not isinstance(value, list):
        raise ConfigError("tracker_states_must_be_list")

    states = tuple(item.strip() for item in value if isinstance(item, str) and item.strip())
    return states if states else default


def _mapping(value: Any, error_code: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ConfigError(error_code)
    return value


def _resolve_path(value: str, *, workflow_dir: str | Path | None, environ: Mapping[str, str] | None) -> Path:
    resolved = resolve_env_reference(value, environ)
    if not resolved.strip():
        raise ConfigError("workspace_root_required")

    path = Path(resolved).expanduser()
    if not path.is_absolute():
        base = Path(workflow_dir) if workflow_dir is not None else Path.cwd()
        path = base / path

    return path.resolve()


def _positive_int(value: Any, default: int, field_name: str) -> int:
    parsed = _int_value(value, default, field_name)
    if parsed <= 0:
        raise ConfigError(f"{field_name}_must_be_positive")
    return parsed


def _non_negative_int(value: Any, default: int, field_name: str) -> int:
    parsed = _int_value(value, default, field_name)
    if parsed < 0:
        raise ConfigError(f"{field_name}_must_be_non_negative")
    return parsed


def _bool_value(value: Any, default: bool, field_name: str) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "yes", "1", "on"):
            return True
        if lowered in ("false", "no", "0", "off"):
            return False
    raise ConfigError(f"{field_name}_must_be_boolean")


def _ratio_value(value: Any, default: float, field_name: str) -> float:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ConfigError(f"{field_name}_must_be_number")
    if isinstance(value, (int, float)):
        parsed = float(value)
    elif isinstance(value, str):
        try:
            parsed = float(value.strip())
        except ValueError as exc:
            raise ConfigError(f"{field_name}_must_be_number") from exc
    else:
        raise ConfigError(f"{field_name}_must_be_number")
    if not 0.0 <= parsed <= 1.0:
        raise ConfigError(f"{field_name}_must_be_between_0_and_1")
    return parsed


def _int_value(value: Any, default: int, field_name: str) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ConfigError(f"{field_name}_must_be_integer")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    raise ConfigError(f"{field_name}_must_be_integer")


def _state_limit_map(value: Any) -> Mapping[str, int]:
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping):
        raise ConfigError("agent_state_limits_must_be_map")

    limits: dict[str, int] = {}
    for raw_state, raw_limit in value.items():
        state = _string_value(raw_state)
        if state is None:
            raise ConfigError("agent_state_limit_state_required")

        limit = _positive_int(raw_limit, 0, "agent_state_limit")
        limits[state.lower()] = limit

    return MappingProxyType(limits)
