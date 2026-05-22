from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import logging.handlers
import os
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Sequence

from symphony import __version__
from symphony.agents.claude_code import ClaudeCodeRunner
from symphony.agents.codex import CodexRunner
from symphony.auth import (
    MissingLinearTokenError,
    TokenStore,
    default_credentials_path,
    load_local_linear_token,
    load_local_github_token,
    save_local_github_token,
    save_local_linear_token,
)
from symphony.config import ConfigError, WorkflowConfig
from symphony.github.client import GitHubClient
from symphony.http_server import StatusAPI, WebhookAPI
from symphony.onboarding import (
    DEFAULT_ACTIVE_STATES,
    DEFAULT_PRESET,
    DEFAULT_RUNNER,
    DEFAULT_TERMINAL_STATES,
    DEFAULT_WORKFLOW_PATH,
    PRESETS,
    InitConfig,
    OnboardingError,
    default_workspace_root,
    generate_workflow,
    parse_state_list,
    write_workflow,
)
from symphony.onboarding_tutorial import run_init_tutorial_once
from symphony.runtime import RuntimeTickResult, SymphonyRuntime
from symphony.tracker.linear import LinearClient, LinearClientError
from symphony.workflow import WorkflowError, load_workflow
from symphony.workflow import EffectiveWorkflow, WorkflowReloader
from symphony.workspace import WorkspaceManager


DEFAULT_PORT = 7337
_LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"
LOGGER = logging.getLogger(__name__)

_LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"

_STARTER_VIEWER_TEAMS_QUERY = """
query SymphonyStarterTeams {
  viewer { teams(first: 10) { nodes { id name } } }
}
""".strip()

_STARTER_FIND_PROJECT_QUERY = """
query SymphonyStarterFindProject($name: String!) {
  projects(filter: {name: {eq: $name}}, first: 1) {
    nodes { id name slugId }
  }
}
""".strip()

_STARTER_TEAM_STATES_QUERY = """
query SymphonyStarterStates($teamId: ID!) {
  workflowStates(
    filter: {team: {id: {eq: $teamId}}, type: {eq: "unstarted"}}
    first: 10
  ) { nodes { id name } }
}
""".strip()

_STARTER_CREATE_PROJECT_MUTATION = """
mutation SymphonyStarterCreateProject($name: String!, $teamIds: [String!]!) {
  projectCreate(input: {name: $name, teamIds: $teamIds}) {
    success
    project { id name slugId }
  }
}
""".strip()

_STARTER_FIND_ISSUE_QUERY = """
query SymphonyStarterFindIssue($projectId: ID!, $title: String!) {
  issues(filter: {project: {id: {eq: $projectId}}, title: {eq: $title}}, first: 1) {
    nodes { id identifier }
  }
}
""".strip()

_STARTER_CREATE_ISSUE_MUTATION = """
mutation SymphonyStarterCreateIssue(
  $title: String!, $description: String,
  $teamId: String!, $projectId: String!, $stateId: String
) {
  issueCreate(input: {
    title: $title, description: $description,
    teamId: $teamId, projectId: $projectId, stateId: $stateId
  }) {
    success
    issue { id identifier }
  }
}
""".strip()

_STARTER_ISSUE_DESCRIPTION = """\
Create a `hello_world.py` file in the repository root that prints `Hello, World!` when run.

Steps:
1. Create `hello_world.py` with `print("Hello, World!")`
2. Verify it runs: `python hello_world.py`
3. Push the branch and open a PR
"""
TickHook = Callable[[], Any]
StatusServer = Callable[[StatusAPI, int], Awaitable[None]]


# ---------------------------------------------------------------------------
# ANSI color helpers — disabled when NO_COLOR is set or stdout is not a TTY
# ---------------------------------------------------------------------------

def _use_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return sys.stdout.isatty()


def _clr(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _use_color() else text


def _ok(t: str) -> str:     return _clr(t, "32")   # green
def _warn(t: str) -> str:   return _clr(t, "33")   # yellow
def _fail(t: str) -> str:   return _clr(t, "31")   # red
def _cyan(t: str) -> str:   return _clr(t, "36")   # cyan
def _purple(t: str) -> str: return _clr(t, "35")   # purple/magenta
def _bold(t: str) -> str:   return _clr(t, "1")    # bold
def _dim(t: str) -> str:    return _clr(t, "2")    # dim


class _ColoredFormatter(logging.Formatter):
    """Colored console log formatter — respects NO_COLOR / FORCE_COLOR."""

    _LEVELS: dict[str, tuple[str, str]] = {
        "DEBUG":    ("2",    "DBUG"),
        "INFO":     ("36",   "INFO"),
        "WARNING":  ("33",   "WARN"),
        "ERROR":    ("31",   "ERR "),
        "CRITICAL": ("1;31", "CRIT"),
    }

    def __init__(self, *, use_color: bool) -> None:
        super().__init__()
        self._use_color = use_color

    def _c(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self._use_color else text

    def format(self, record: logging.LogRecord) -> str:
        ts = self.formatTime(record, "%H:%M:%S")
        code, label = self._LEVELS.get(record.levelname, ("", record.levelname[:4].ljust(4)))
        # Strip the "symphony." package prefix so names stay compact
        name = record.name.removeprefix("symphony.")

        msg = record.getMessage()
        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            msg = f"{msg}\n{record.exc_text}"
        if record.stack_info:
            msg = f"{msg}\n{self.formatStack(record.stack_info)}"

        msg_colored = (
            self._c(msg, "31") if record.levelno >= logging.ERROR
            else self._c(msg, "33") if record.levelno >= logging.WARNING
            else msg
        )

        return (
            f"{self._c(ts, '2')} "
            f"{self._c(label, code)} "
            f"{self._c(f'{name:<22}', '2')} "
            f"{msg_colored}"
        )


def _cli_name() -> str:
    """Return the name used to invoke this CLI (e.g. 'symphony' or 'sy')."""
    if not sys.argv:
        return "symphony"
    name = Path(sys.argv[0]).name
    return name[:-3] if name.endswith(".py") else name


@dataclass(frozen=True)
class StartupContext:
    workflow_path: Path
    logs_root: Path
    port: int
    workflow: object
    config: WorkflowConfig


class StartupError(RuntimeError):
    """Raised when Symphony cannot start with the requested configuration."""


def build_parser() -> argparse.ArgumentParser:
    cli = _cli_name()
    parser = argparse.ArgumentParser(
        prog=cli,
        description="Run the Symphony CLI MVP orchestrator for a repository WORKFLOW.md.",
        epilog=(
            f"Common commands: {cli} onboard, {cli} init, "
            f"{cli} doctor WORKFLOW.md, {cli} run WORKFLOW.md"
        ),
    )
    _add_version_argument(parser)
    parser.add_argument(
        "workflow_path",
        nargs="?",
        default="WORKFLOW.md",
        help="Path to repository WORKFLOW.md. Defaults to ./WORKFLOW.md.",
    )
    parser.add_argument(
        "--port",
        type=_port_value,
        default=DEFAULT_PORT,
        help=f"Loopback status API port. Defaults to {DEFAULT_PORT}.",
    )
    parser.add_argument(
        "--logs-root",
        default="./log",
        help="Directory for Symphony runtime logs. Defaults to ./log.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate workflow/configuration and exit without starting the daemon.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one poll/dispatch tick and exit. Useful for smoke tests.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
        help="Console log level. Defaults to INFO.",
    )
    return parser


def build_run_parser() -> argparse.ArgumentParser:
    parser = build_parser()
    parser.prog = f"{_cli_name()} run"
    parser.description = "Run the Symphony orchestrator for a repository WORKFLOW.md."
    return parser


def build_init_parser(prog: str | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog or f"{_cli_name()} init",
        description="Generate a starter WORKFLOW.md and optionally store local credentials.",
    )
    _add_version_argument(parser)
    parser.add_argument(
        "--workflow-path",
        default=DEFAULT_WORKFLOW_PATH,
        help="Where to write the generated workflow. Defaults to ./WORKFLOW.md.",
    )
    parser.add_argument(
        "--preset",
        default=DEFAULT_PRESET,
        choices=tuple(PRESETS),
        help=f"Workflow preset. Defaults to {DEFAULT_PRESET}.",
    )
    parser.add_argument("--project-slug", help="Linear project slugId to poll.")
    parser.add_argument(
        "--active-states",
        help="Comma-separated Linear state names that should dispatch work.",
    )
    parser.add_argument(
        "--terminal-states",
        help="Comma-separated Linear terminal state names.",
    )
    parser.add_argument("--workspace-root", help="Root directory for issue workspaces.")
    parser.add_argument(
        "--codex-command",
        default="codex app-server",
        help="Command used to launch Codex. Defaults to 'codex app-server'.",
    )
    parser.add_argument(
        "--linear-api-key",
        help="Store this Linear API key in the local Symphony credential file.",
    )
    parser.add_argument(
        "--credentials-path",
        help="Override the local credentials file path. Mostly useful for tests.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace an existing WORKFLOW.md.",
    )
    parser.add_argument(
        "--runner",
        default=DEFAULT_RUNNER,
        choices=("claude_code", "codex"),
        help=f"Agent runner to use. Defaults to {DEFAULT_RUNNER}.",
    )
    parser.add_argument(
        "--github-token",
        help="GitHub personal access token for PR automation (Contents + Pull requests R/W).",
    )
    parser.add_argument(
        "--github-org",
        help="GitHub organisation or user name that owns the target repositories.",
    )
    parser.add_argument(
        "--github-repo",
        help="Default GitHub repository name (without org prefix) for PR automation.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Alias for --mode automated. Requires explicit setup inputs.",
    )
    parser.add_argument(
        "--mode",
        choices=("interactive", "automated"),
        help="Setup mode. Defaults to interactive in a TTY and automated otherwise.",
    )
    return parser


def build_onboard_parser() -> argparse.ArgumentParser:
    parser = build_init_parser(prog=f"{_cli_name()} onboard")
    parser.description = (
        "Run first-time setup, skipping init when an existing WORKFLOW.md "
        "and local prerequisites already validate."
    )
    return parser


def build_doctor_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"{_cli_name()} doctor",
        description="Check workflow, Linear auth, Codex command, and workspace readiness.",
    )
    _add_version_argument(parser)
    parser.add_argument(
        "workflow_path",
        nargs="?",
        default=DEFAULT_WORKFLOW_PATH,
        help="Path to repository WORKFLOW.md. Defaults to ./WORKFLOW.md.",
    )
    parser.add_argument(
        "--port",
        type=_port_value,
        default=DEFAULT_PORT,
        help=f"Loopback status API port. Defaults to {DEFAULT_PORT}.",
    )
    parser.add_argument(
        "--logs-root",
        default="./log",
        help="Directory for Symphony runtime logs. Defaults to ./log.",
    )
    parser.add_argument(
        "--log-level",
        default="WARNING",
        choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
        help="Console log level. Defaults to WARNING.",
    )
    return parser


def _add_version_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--version",
        action="version",
        version=f"Symphony {__version__}",
    )


def load_startup_context(
    workflow_path: str | Path,
    *,
    logs_root: str | Path,
    port: int,
    environ: Mapping[str, str] | None = None,
) -> StartupContext:
    workflow_file = Path(workflow_path).expanduser().resolve()
    logs_path = _resolve_logs_root(logs_root, workflow_file)

    try:
        definition = load_workflow(workflow_file)
        config = definition.typed_config(workflow_path=workflow_file, environ=environ)
        validate_dispatch_config(config, environ=environ)
    except (WorkflowError, ConfigError, MissingLinearTokenError) as exc:
        raise StartupError(str(exc)) from exc

    return StartupContext(
        workflow_path=workflow_file,
        logs_root=logs_path,
        port=port,
        workflow=definition,
        config=config,
    )


def validate_dispatch_config(config: WorkflowConfig, *, environ: Mapping[str, str] | None = None) -> None:
    if config.tracker.kind != "linear":
        raise ConfigError("unsupported_tracker_kind")
    if not config.tracker.project_slug:
        raise ConfigError("missing_tracker_project_slug")
    TokenStore(config.tracker, environ=environ).resolve_linear_token()
    if config.agent.runner == "claude_code":
        if not config.claude_code.command.strip():
            raise ConfigError("claude_code_command_required")
    else:
        if not config.codex.command.strip():
            raise ConfigError("codex_command_required")


def configure_logging(level: str, logs_root: Path | None = None) -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level))

    if not root.handlers:
        use_color = (
            not os.environ.get("NO_COLOR")
            and (bool(os.environ.get("FORCE_COLOR")) or sys.stderr.isatty())
        )
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(_ColoredFormatter(use_color=use_color))
        root.addHandler(stream_handler)

    if logs_root is not None and not any(isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers):
        logs_root.mkdir(parents=True, exist_ok=True)
        plain_fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        file_handler = logging.handlers.RotatingFileHandler(
            logs_root / "symphony.log",
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(plain_fmt)
        root.addHandler(file_handler)


def create_runtime(context: StartupContext) -> SymphonyRuntime:
    linear_client = create_tracker(context.config)
    workspace_manager = create_workspace_manager(context.config, logs_root=context.logs_root)
    runner = create_runner(context.config, linear_client)
    github_client = create_github_client(context.config)
    return SymphonyRuntime(
        config=context.config,
        workflow=context.workflow,
        tracker=linear_client,
        workspace_manager=workspace_manager,
        runner=runner,
        github_client=github_client,
    )


def create_github_client(config: WorkflowConfig) -> GitHubClient | None:
    gh = config.github
    if gh.token and gh.owner and gh.repo:
        return GitHubClient(token=gh.token, owner=gh.owner, repo=gh.repo)
    return None


def create_status_api(runtime: SymphonyRuntime) -> StatusAPI:
    return StatusAPI(runtime.snapshot, refresh_callback=runtime.run_tick)


def create_tracker(config: WorkflowConfig) -> LinearClient:
    return LinearClient(config.tracker)


def create_workspace_manager(
    config: WorkflowConfig,
    *,
    logs_root: Path | None = None,
) -> WorkspaceManager:
    return WorkspaceManager(
        config.workspace,
        config.hooks,
        logs_root=logs_root,
    )


def create_runner(config: WorkflowConfig, linear_client: LinearClient) -> CodexRunner | ClaudeCodeRunner:
    if config.agent.runner == "claude_code":
        linear_api_key = _resolve_linear_token(config)
        github_token = _resolve_github_token()
        return ClaudeCodeRunner(
            config.claude_code.command,
            model=config.claude_code.model,
            permission_mode=config.claude_code.permission_mode,
            turn_timeout_ms=config.claude_code.turn_timeout_ms,
            linear_api_key=linear_api_key,
            github_token=github_token,
        )
    return CodexRunner(
        config.codex.command,
        approval_policy=config.codex.approval_policy or "on-request",
        thread_sandbox=config.codex.thread_sandbox or "workspace-write",
        turn_sandbox_policy=_codex_turn_sandbox_policy(config),
        read_timeout_ms=config.codex.read_timeout_ms,
        turn_timeout_ms=config.codex.turn_timeout_ms,
        linear_client=linear_client,
    )


async def run_once(runtime: SymphonyRuntime) -> RuntimeTickResult:
    return await runtime.run_tick()


async def _startup_workspace_sweep(workspace_manager: WorkspaceManager) -> None:
    """Force-clean orphan per-run worktrees from prior crashed dispatches.

    Symphony does not persist its in-memory running set across restarts, so any
    per-run workspace that exists at startup is stale by definition (PRD §8.1).
    """

    sweep = getattr(workspace_manager, "sweep_stale_worktrees", None)
    if sweep is None:
        return
    try:
        removed = await sweep()
    except Exception as exc:  # noqa: BLE001 - sweep is best-effort and must not block startup.
        LOGGER.warning("Startup workspace sweep failed: %s", exc)
        return
    if removed:
        LOGGER.info("Startup workspace sweep removed %d stale per-run director%s.", removed, "y" if removed == 1 else "ies")


def _log_tick(result) -> None:
    parts = [f"fetched={result.fetched}"]
    if getattr(result, "active", 0):
        parts.append(f"active={result.active}")
    if result.dispatched:
        parts.append(f"dispatched={','.join(result.dispatched)}")
    if result.completed:
        parts.append(f"completed={','.join(result.completed)}")
    if result.failed:
        parts.append(f"failed={','.join(result.failed)}")
    if result.released:
        parts.append(f"released={','.join(result.released)}")
    if result.errors:
        for ident, reason in result.errors.items():
            parts.append(f"error[{ident}]={reason}")
    if result.failed:
        LOGGER.warning("Tick  %s", "  ".join(parts))
    else:
        LOGGER.info("Tick  %s", "  ".join(parts))


async def run_poll_loop(
    runtime: SymphonyRuntime,
    *,
    before_tick: TickHook | None = None,
    tick_lock: asyncio.Lock | None = None,
) -> None:
    # Guard the startup snapshot with the shared lock so that a webhook-triggered
    # tick cannot fire while _prev_candidate_ids is still empty and pre-existing
    # issues have not yet been excluded.
    if tick_lock is not None:
        async with tick_lock:
            await _startup_workspace_sweep(runtime.workspace_manager)
            await runtime.record_startup_issues()
    else:
        await _startup_workspace_sweep(runtime.workspace_manager)
        await runtime.record_startup_issues()
    while True:
        try:
            if before_tick is not None:
                result = before_tick()
                if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
                    await result
            if tick_lock is not None:
                async with tick_lock:
                    result = await runtime.run_tick()
            else:
                result = await runtime.run_tick()
        except LinearClientError as exc:
            LOGGER.warning("Linear API error during poll tick, will retry next interval: %s", exc)
            await asyncio.sleep(runtime.state.poll_interval_ms / 1000)
            continue
        except Exception as exc:
            LOGGER.error("Unexpected error during poll tick, will retry next interval: %s", exc, exc_info=True)
            await asyncio.sleep(runtime.state.poll_interval_ms / 1000)
            continue
        _log_tick(result)
        await asyncio.sleep(runtime.state.poll_interval_ms / 1000)


async def serve_status_api(
    status_api: StatusAPI,
    port: int,
    *,
    webhook_api: WebhookAPI | None = None,
) -> None:
    loop = asyncio.get_running_loop()
    server = create_status_http_server(
        status_api, port, loop=loop,
        webhook_api=webhook_api,
    )
    LOGGER.info("Status API listening on http://127.0.0.1:%s", port)
    serve_task = asyncio.create_task(asyncio.to_thread(server.serve_forever, 0.25))
    try:
        await serve_task
    finally:
        server.shutdown()
        server.server_close()
        await asyncio.gather(serve_task, return_exceptions=True)


def create_status_http_server(
    status_api: StatusAPI,
    port: int,
    *,
    loop: asyncio.AbstractEventLoop,
    host: str = "127.0.0.1",
    webhook_api: WebhookAPI | None = None,
) -> ThreadingHTTPServer:
    _webhook_route = "/api/v1/webhooks/linear"

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API.
            self._send_status_response("GET")

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API.
            self._send_status_response("POST")

        def do_PUT(self) -> None:  # noqa: N802 - stdlib handler API.
            self._send_status_response("PUT")

        def do_DELETE(self) -> None:  # noqa: N802 - stdlib handler API.
            self._send_status_response("DELETE")

        def log_message(self, fmt: str, *args: object) -> None:
            LOGGER.debug("Status API request: " + fmt, *args)

        def _send_status_response(self, method: str) -> None:
            body = self.rfile.read(_content_length(self.headers.get("content-length")))
            route = self.path.split("?", 1)[0]

            if method == "POST" and route == _webhook_route and webhook_api is not None:
                header_map = {k.lower(): v for k, v in self.headers.items()}
                future = asyncio.run_coroutine_threadsafe(
                    webhook_api.async_handle_request(method, self.path, body, header_map),
                    loop,
                )
                response = future.result()
            elif method == "POST" and route == "/api/v1/refresh":
                future = asyncio.run_coroutine_threadsafe(
                    status_api.async_handle_request(method, self.path, body),
                    loop,
                )
                response = future.result()
            else:
                response = status_api.handle_request(method, self.path, body)

            payload = response.json_bytes()
            self.send_response(response.status_code)
            for header, value in response.headers.items():
                self.send_header(header, value)
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return ThreadingHTTPServer((host, port), Handler)


async def run_daemon(
    runtime: SymphonyRuntime,
    context: StartupContext,
    *,
    workflow_reloader: "RuntimeWorkflowReloader | None" = None,
    status_server: StatusServer = serve_status_api,
    webhook_api: WebhookAPI | None = None,
) -> None:
    status_api = create_status_api(runtime)
    # Single lock shared by the poll loop and every webhook-triggered tick so
    # they cannot execute concurrently against the same runtime state.
    tick_lock = asyncio.Lock()
    # Build a WebhookAPI from config when not explicitly provided.
    if webhook_api is None and context.config.webhook.secret:
        webhook_api = WebhookAPI(
            webhook_secret=context.config.webhook.secret,
            on_event=_make_webhook_event_handler(runtime, tick_lock=tick_lock),
        )
    elif webhook_api is not None:
        # Re-inject on_event with the shared lock regardless of how the API was built.
        webhook_api.on_event = _make_webhook_event_handler(runtime, tick_lock=tick_lock)
    # Auto-register the webhook with Linear when url + team_id + secret are all configured.
    wh = context.config.webhook
    if wh.url and wh.team_id and wh.secret:
        from symphony.tracker.webhooks import WebhookRegistrar, WebhookRegistrarError  # noqa: PLC0415
        try:
            api_key = TokenStore(context.config.tracker).resolve_linear_token()
            registrar = WebhookRegistrar(api_token=api_key)
            webhook_id = await registrar.register(wh.url, wh.team_id, wh.secret)
            LOGGER.info("Webhook registered: id=%s url=%s", webhook_id, wh.url)
        except (MissingLinearTokenError, WebhookRegistrarError) as exc:
            LOGGER.warning("Webhook auto-registration failed (falling back to polling): %s", exc)

    import functools as _functools  # noqa: PLC0415
    _status_server = _functools.partial(
        status_server,
        webhook_api=webhook_api,
    )
    status_task = asyncio.create_task(_status_server(status_api, context.port))
    poll_task = asyncio.create_task(
        run_poll_loop(
            runtime,
            before_tick=workflow_reloader.reload_if_changed if workflow_reloader is not None else None,
            tick_lock=tick_lock,
        )
    )
    tasks = {status_task, poll_task}

    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for task in done:
            task.result()
        for task in pending:
            task.cancel()
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _inject_github_config(workflow_path: Path, owner: str, repo: str) -> None:
    """Insert a github: block into an existing WORKFLOW.md front matter."""
    content = workflow_path.read_text(encoding="utf-8")
    # Front matter is bounded by the first and second '---' lines.
    # Find the closing delimiter (skip the opening one at index 0).
    close_idx = content.index("---", 3)
    github_block = f"github:\n  token: $GITHUB_TOKEN\n  owner: {owner}\n  repo: {repo}\n"
    workflow_path.write_text(
        content[:close_idx] + github_block + content[close_idx:],
        encoding="utf-8",
    )


def _maybe_upgrade_github_config(
    workflow_path: Path,
    args: argparse.Namespace,
    *,
    detected_github_org: str | None,
    detected_github_repo: str | None,
) -> None:
    """Inject a missing github: block into an existing WORKFLOW.md.

    Prints a confirmation when the block is injected, or a hint when auto-detection
    fails so the user knows what to do next.
    """
    try:
        ctx = load_startup_context(workflow_path, logs_root="./log", port=DEFAULT_PORT)
    except StartupError:
        return  # Let doctor_checks surface the error properly.

    cfg = ctx.config
    if cfg.agent.runner != "claude_code":
        return
    if cfg.github.owner and cfg.github.repo:
        return  # Already configured.

    automated = True
    try:
        automated = _resolve_init_mode(args) == "automated"
    except OnboardingError:
        pass

    github_org = (
        getattr(args, "github_org", None)
        or detected_github_org
        or ("" if automated else _prompt("GitHub org/user for PR polling (blank to skip)").strip())
    )
    github_repo = (
        getattr(args, "github_repo", None)
        or detected_github_repo
        or ("" if automated else _prompt("Repository name (blank to skip)").strip())
    )

    if github_org and github_repo:
        _inject_github_config(workflow_path, github_org, github_repo)
        print(f"Updated {workflow_path} with GitHub config ({github_org}/{github_repo}).")
    else:
        print(
            "Note: github: block is missing from WORKFLOW.md."
            " Rerun with --github-org and --github-repo to add it automatically,"
            " or edit WORKFLOW.md manually."
        )


def _make_webhook_event_handler(
    runtime: SymphonyRuntime,
    *,
    tick_lock: asyncio.Lock | None = None,
) -> "Callable[[Any], Awaitable[None]]":
    """Return an async callback that triggers an immediate run_tick on issue state-change events.

    tick_lock — shared with run_poll_loop so webhook ticks cannot race with the
    polling tick.  When locked (poll in progress), incoming webhook events are
    silently dropped rather than queued.
    """

    async def on_webhook_event(event: Any) -> None:
        # Only dispatch on Issue events — state changes are the primary trigger.
        if not hasattr(event, "type") or event.type != "Issue":
            return
        LOGGER.info(
            "Webhook event received: action=%s type=%s — triggering immediate tick",
            getattr(event, "action", "?"),
            getattr(event, "type", "?"),
        )
        # Coalesce: drop the webhook tick when polling is already running.
        if tick_lock is not None and tick_lock.locked():
            LOGGER.debug("Webhook tick skipped — tick already in progress")
            return
        lock_ctx: asyncio.Lock = tick_lock if tick_lock is not None else asyncio.Lock()
        async with lock_ctx:
            try:
                await runtime.run_tick()
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("Webhook-triggered tick failed: %s", exc)

    return on_webhook_event


@dataclass
class RuntimeWorkflowReloader:
    runtime: SymphonyRuntime
    workflow_path: Path
    environ: Mapping[str, str] | None = None
    reloader: WorkflowReloader | None = None
    last_observed_mtime_ns: int | None = None
    webhook_api: "WebhookAPI | None" = None

    @classmethod
    def from_context(
        cls,
        runtime: SymphonyRuntime,
        context: StartupContext,
        *,
        environ: Mapping[str, str] | None = None,
        webhook_api: "WebhookAPI | None" = None,
    ) -> "RuntimeWorkflowReloader":
        reloader = WorkflowReloader.for_path(context.workflow_path)
        effective = EffectiveWorkflow(definition=context.workflow, config=context.config)
        reloader.last_good = context.workflow
        reloader.last_good_effective = effective
        return cls(
            runtime=runtime,
            workflow_path=context.workflow_path,
            environ=environ,
            reloader=reloader,
            last_observed_mtime_ns=_workflow_mtime_ns(context.workflow_path),
            webhook_api=webhook_api,
        )

    def reload_if_changed(self) -> bool:
        mtime_ns = _workflow_mtime_ns(self.workflow_path)
        if mtime_ns == self.last_observed_mtime_ns:
            return False
        self.last_observed_mtime_ns = mtime_ns
        return self.reload_now()

    def reload_now(self) -> bool:
        active_reloader = self.reloader or WorkflowReloader.for_path(self.workflow_path)
        try:
            definition = load_workflow(self.workflow_path)
            config = definition.typed_config(workflow_path=self.workflow_path, environ=self.environ)
            validate_dispatch_config(config, environ=self.environ)
        except (WorkflowError, ConfigError, MissingLinearTokenError) as exc:
            active_reloader.last_error = exc
            LOGGER.error("Rejected WORKFLOW.md reload for %s: %s", self.workflow_path, exc)
            return False

        effective = EffectiveWorkflow(definition=definition, config=config)
        active_reloader.last_good = definition
        active_reloader.last_good_effective = effective
        active_reloader.last_error = None
        self.reloader = active_reloader
        apply_runtime_workflow(self.runtime, effective)
        if self.webhook_api is not None:
            if config.webhook.secret is not None:
                self.webhook_api.webhook_secret = config.webhook.secret
            else:
                # Fail closed: secret removed from config — reject all incoming
                # requests rather than silently accepting unsigned POSTs.
                self.webhook_api.webhook_secret = secrets.token_hex(32)
        LOGGER.info("Reloaded WORKFLOW.md from %s", self.workflow_path)
        return True


def apply_runtime_workflow(runtime: SymphonyRuntime, effective: EffectiveWorkflow) -> None:
    linear_client = create_tracker(effective.config)
    runtime.config = effective.config
    runtime.workflow = effective.definition
    runtime.prompt_template = effective.definition.prompt_template
    runtime.tracker = linear_client
    logs_root = getattr(runtime.workspace_manager, "logs_root", None)
    runtime.workspace_manager = create_workspace_manager(effective.config, logs_root=logs_root)
    runtime.runner = create_runner(effective.config, linear_client)
    runtime.state.apply_config(effective.config)
    runtime._notify_state_change()


def main(argv: Sequence[str] | None = None) -> int:
    raw_args = list(argv) if argv is not None else sys.argv[1:]
    if raw_args:
        command = raw_args[0]
        if command == "init":
            return init_main(raw_args[1:])
        if command == "onboard":
            return onboard_main(raw_args[1:])
        if command == "doctor":
            return doctor_main(raw_args[1:])
        if command == "run":
            return run_main(raw_args[1:])
        if command == "webhooks":
            return webhooks_main(raw_args[1:])
        if command == "project":
            return project_main(raw_args[1:])

    parser = build_parser()
    args = parser.parse_args(raw_args)
    return run_with_args(args, parser)


def run_main(argv: Sequence[str] | None = None) -> int:
    parser = build_run_parser()
    args = parser.parse_args(argv)
    return run_with_args(args, parser)


def run_with_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    configure_logging(args.log_level)

    try:
        context = load_startup_context(
            args.workflow_path,
            logs_root=args.logs_root,
            port=args.port,
        )
    except StartupError as exc:
        if "missing_workflow_file" in str(exc) and sys.stdin.isatty():
            if not _run_first_run_wizard(Path(args.workflow_path)):
                return 1
            try:
                context = load_startup_context(
                    args.workflow_path,
                    logs_root=args.logs_root,
                    port=args.port,
                )
            except StartupError as exc2:
                parser.exit(2, f"symphony: {exc2}\n")
                return 1
        else:
            parser.exit(2, f"symphony: {exc}\n")

    configure_logging(args.log_level, logs_root=context.logs_root)

    if args.check:
        print(f"Workflow OK: {context.workflow_path}")
        print(f"Logs root: {context.logs_root}")
        print(f"Status API port: {context.port}")
        return 0

    runtime = create_runtime(context)
    if args.once:
        result = asyncio.run(run_once(runtime))
        print(
            "Tick OK: "
            f"fetched={result.fetched} "
            f"dispatched={len(result.dispatched)} "
            f"completed={len(result.completed)} "
            f"failed={len(result.failed)} "
            f"released={len(result.released)}"
        )
        return 0

    port_ok, port_detail = _check_port_available(context.port)
    if not port_ok:
        parser.exit(
            1,
            f"{_fail('Error')}: {port_detail}\n"
            f"{_dim('Tip: each symphony process needs its own port. '
                    'Run multiple projects with different --port values:')}\n"
            f"  symphony run project-a/WORKFLOW.md --port {context.port + 1}\n"
            f"  symphony run project-b/WORKFLOW.md --port {context.port + 2}\n",
        )

    project_slug = context.config.tracker.project_slug or "unknown"
    poll_interval_s = context.config.polling.interval_ms // 1000
    print(
        f"{_bold('Symphony')} {_dim(__version__)}  |  "
        f"project: {_cyan(project_slug)}  |  "
        f"poll every {poll_interval_s}s  |  "
        f"status: {_dim(f'http://127.0.0.1:{context.port}')}"
    )
    print(_dim(f"Workflow: {context.workflow_path}"))
    print(_dim(f"Logs:     {context.logs_root}"))
    print(_dim("Stop: Ctrl-C  |  Running multiple projects? Use a separate process per WORKFLOW.md with a unique --port"))
    print()

    # Always mount the webhook route so that adding webhook.secret to WORKFLOW.md
    # via a hot reload activates it without restarting the daemon.  When no secret
    # is configured at startup the route is fail-closed (random secret rejects all
    # unsigned requests); reload_now() installs the real secret when one appears.
    _webhook_api = WebhookAPI(
        webhook_secret=context.config.webhook.secret if context.config.webhook.secret else secrets.token_hex(32),
        on_event=_make_webhook_event_handler(runtime),
    )
    workflow_reloader = RuntimeWorkflowReloader.from_context(
        runtime, context, webhook_api=_webhook_api
    )
    asyncio.run(run_daemon(runtime, context, workflow_reloader=workflow_reloader, webhook_api=_webhook_api))
    return 0


def init_main(argv: Sequence[str] | None = None) -> int:
    parser = build_init_parser()
    args = parser.parse_args(argv)
    return _run_init_with_args(args, parser, command_name="init")


def onboard_main(argv: Sequence[str] | None = None) -> int:
    parser = build_onboard_parser()
    args = parser.parse_args(argv)

    # Auto-detect GitHub org/repo from the git remote before scanning.
    # Track which values came from detection (not explicit flags) so Step 3
    # can confirm them interactively without re-prompting explicit flags.
    detected_github_org: str | None = None
    detected_github_repo: str | None = None
    if not args.github_org or not args.github_repo:
        detected_org, detected_repo = _detect_github_from_remote()
        if detected_org and not args.github_org:
            args.github_org = detected_org
            detected_github_org = detected_org
        if detected_repo and not args.github_repo:
            args.github_repo = detected_repo
            detected_github_repo = detected_repo

    _env_checks = setup_environment_checks(args)
    print_setup_checks("Environment scan", _env_checks)

    # Propagate env-detected credentials into args so _run_init_with_args
    # does not re-prompt for auth the scan already validated.
    if not getattr(args, "linear_api_key", None) and os.environ.get("LINEAR_API_KEY"):
        args.linear_api_key = os.environ["LINEAR_API_KEY"]
    if not getattr(args, "github_token", None) and os.environ.get("GITHUB_TOKEN"):
        args.github_token = os.environ["GITHUB_TOKEN"]

    workflow_path = Path(args.workflow_path).expanduser()
    if workflow_path.exists() and not args.overwrite:
        checks = doctor_checks(workflow_path, logs_root="./log", port=DEFAULT_PORT, skip_port_check=True)
        print_setup_checks("Existing setup", checks)
        if all(ok for ok, _, _ in checks):
            _maybe_upgrade_github_config(
                workflow_path,
                args,
                detected_github_org=detected_github_org,
                detected_github_repo=detected_github_repo,
            )
            print(f"\nOnboarding already complete: {workflow_path}")
            print("Skipped init because WORKFLOW.md and local prerequisites validated.")
            _offer_tutorial(args)
            _offer_starter_mission(args)
            return 0

        message = (
            "existing workflow needs attention; run `symphony doctor "
            f"{workflow_path}` or rerun onboarding with --overwrite"
        )
        parser.exit(2, f"symphony onboard: {message}\n")

    return _run_init_with_args(
        args,
        parser,
        command_name="onboard",
        show_environment_scan=False,
        show_tutorial_before=False,
        detected_github_org=detected_github_org,
        detected_github_repo=detected_github_repo,
    )


def _run_init_with_args(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
    *,
    command_name: str,
    show_environment_scan: bool = True,
    show_tutorial_before: bool = True,
    detected_github_org: str | None = None,
    detected_github_repo: str | None = None,
) -> int:
    try:
        mode = _resolve_init_mode(args)
        automated = mode == "automated"

        if show_environment_scan:
            print_setup_checks("Environment scan", setup_environment_checks(args))

        if not automated and show_tutorial_before:
            run_init_tutorial_once()

        # --- Step 1: Linear project slug ---
        if not args.project_slug and not automated:
            print("Step 1/5 — Linear project slug")
            print("  The short identifier for your Linear project.")
            print("  Find it at: your project → Settings, or in the project URL:")
            print("    linear.app/YOUR-TEAM/project/NAME-<slug>")
        project_slug = _parse_linear_slug(args.project_slug or ("" if automated else _prompt("Linear project slug")))
        if not project_slug and not automated:
            raise OnboardingError("missing_project_slug — pass --project-slug your-linear-project-slug")

        if not args.active_states and not automated:
            print("\nStep 2/5 — Linear workflow states")
            print("  Active states: issues in these states will be picked up and worked on.")
            print("  Terminal states: issues in these states are considered done and won't be retried.")
            print("  Check your team's workflow at: linear.app → your team → Settings → Workflow.")
        active_states = parse_state_list(
            args.active_states
            or (None if automated else _prompt_default("Active states (comma-separated)", ", ".join(DEFAULT_ACTIVE_STATES))),
            DEFAULT_ACTIVE_STATES,
        )
        terminal_states = parse_state_list(
            args.terminal_states
            or (None if automated else _prompt_default("Terminal states (comma-separated)", ", ".join(DEFAULT_TERMINAL_STATES))),
            DEFAULT_TERMINAL_STATES,
        )
        if not args.workspace_root and not automated:
            print("  Workspace root: the local directory where per-issue workspaces are created.")
            print("  Each issue gets its own isolated subdirectory under this path.")
        workspace_root = args.workspace_root or (
            default_workspace_root(project_slug)
            if automated
            else _prompt_default("Workspace root directory", default_workspace_root(project_slug))
        )

        # --- Step 2: GitHub org + repo (claude_code runner only) ---
        runner = args.runner
        github_org = args.github_org or ""
        github_repo = args.github_repo or ""
        if runner == "claude_code" and not automated:
            # Only show Step 3 when at least one value needs input or confirmation.
            # Explicit CLI flags are accepted as-is; only auto-detected values are
            # shown as editable defaults so the user can correct a wrong detection.
            org_needs_input = not github_org or github_org == detected_github_org
            repo_needs_input = not github_repo or github_repo == detected_github_repo
            if org_needs_input or repo_needs_input:
                print("\nStep 3/5 — GitHub repository for PR automation")
                print("  Agents will clone this repo, push a branch, and open a PR.")
                print("  Example: for github.com/acme-corp/my-backend, org = 'acme-corp', repo = 'my-backend'")
            if org_needs_input:
                github_org = _prompt_default("GitHub org/user", github_org) if github_org else _prompt("GitHub org/user (blank to fill in later)").strip()
            if repo_needs_input:
                github_repo = _prompt_default("Repository name", github_repo) if github_repo else _prompt("Repository name (blank to fill in later)").strip()

        # --- Step 3: Linear API key ---
        linear_token = args.linear_api_key
        if linear_token is None and not automated:
            print("\nStep 4/5 — Linear API key")
            print("  Symphony uses this key to poll issues and post progress comments.")
            print("  Create one at: linear.app/settings/api → Personal API keys")
            print("  The key starts with lin_api_...")
            linear_token = getpass.getpass("Linear API key (blank to skip): ").strip()

        # --- Step 4: GitHub token (optional, for PR automation) ---
        github_token = args.github_token
        if github_token is None and runner == "claude_code" and not automated:
            print("\nStep 5/5 — GitHub personal access token (for PR automation)")
            print("  Agents need this to push branches and open pull requests.")
            print("  Create a fine-grained token at: github.com/settings/tokens")
            print("  Required permissions: Contents (Read/Write), Pull requests (Read/Write)")
            print("  Scope it to the specific repository if possible.")
            github_token = getpass.getpass("GitHub token (blank to skip): ").strip()

        if automated:
            failures = _automated_setup_failures(
                args,
                project_slug=project_slug,
                runner=runner,
                github_org=github_org,
                github_repo=github_repo,
                linear_token=linear_token,
                github_token=github_token,
            )
            if failures:
                raise OnboardingError(_format_setup_failures(failures))

        workflow = generate_workflow(
            InitConfig(
                project_slug=project_slug,
                preset=args.preset,
                active_states=active_states,
                terminal_states=terminal_states,
                workspace_root=workspace_root,
                codex_command=args.codex_command,
                runner=runner,
                github_org=github_org,
                github_repo=github_repo,
            )
        )
        workflow_path = write_workflow(args.workflow_path, workflow, overwrite=args.overwrite)
    except OnboardingError as exc:
        parser.exit(2, f"symphony {command_name}: {exc}\n")

    if linear_token:
        credentials_path = save_local_linear_token(linear_token, path=args.credentials_path)
        print(f"Stored Linear credentials: {credentials_path}")
    elif automated:
        print("Linear credentials not stored. Using existing LINEAR_API_KEY or local credentials.")
    else:
        print("Linear credentials not stored. Set LINEAR_API_KEY or re-run with --linear-api-key.")
        print(f"Default credentials path: {default_credentials_path()}")

    if github_token:
        gh_user = "provided" if automated else _validate_github_token(github_token)
        if gh_user:
            credentials_path = save_local_github_token(github_token, path=args.credentials_path)
            print(f"Stored GitHub credentials: {credentials_path}")
            print(f"  Connected as: {gh_user}")
        else:
            print("  GitHub token validation failed — token not stored.")
            print("  Check permissions or re-run with --github-token.")
    elif runner == "claude_code" and automated:
        print("GitHub token not stored. Using gh auth, GITHUB_TOKEN, or local credentials.")
    elif runner == "claude_code":
        print("GitHub token not stored. Set GITHUB_TOKEN or re-run with --github-token.")

    print(f"\nWrote workflow: {workflow_path}")
    print(f"Next: {_cyan(_cli_name() + ' doctor ' + str(workflow_path))}")
    print(_dim(
        "Tip: each WORKFLOW.md targets one Linear project. "
        "To run multiple projects in parallel, start one process per WORKFLOW.md "
        "and assign each a unique port (--port). "
        "Stop a process with Ctrl-C, or find its PID with: ps aux | grep 'symphony run'"
    ))

    if not automated and not show_tutorial_before:
        _offer_tutorial(args)
        _offer_starter_mission(args)

    return 0


def _offer_tutorial(args: argparse.Namespace) -> None:
    """Prompt to show the Symphony intro tutorial; only runs in interactive TTY mode."""
    if not sys.stdin.isatty():
        return
    try:
        mode = _resolve_init_mode(args)
    except OnboardingError:
        return
    if mode == "automated":
        return
    try:
        answer = input("\nShow a quick intro to Symphony? [Y/n] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return
    if answer in {"", "y", "yes"}:
        run_init_tutorial_once(force=True)


def _offer_starter_mission(
    args: argparse.Namespace,
    *,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Prompt for the optional hello-world demo; only runs in interactive TTY mode."""
    if not sys.stdin.isatty():
        return
    try:
        mode = _resolve_init_mode(args)
    except OnboardingError:
        return
    if mode == "automated":
        return

    from symphony.onboarding_tutorial import should_show_starter_mission  # noqa: PLC0415

    if not should_show_starter_mission(environ=environ):
        return

    print("\n★  Starter Mission (optional, runs once)")
    print("   Symphony creates a sample Linear project with 1 ticket and dispatches")
    print("   an agent on it — so you can see the full delivery loop.")
    try:
        answer = input("\nRun the demo? [Y/n] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return
    if answer in {"", "y", "yes"}:
        _run_starter_mission(args, environ=environ)


def _run_starter_mission(
    args: argparse.Namespace,
    *,
    environ: Mapping[str, str] | None = None,
    history_path: str | Path | None = None,
) -> bool:
    """Create the hello-world Linear project, issues, WORKFLOW.md, and run --once.

    Returns True if the mission completed (project + issues created).
    """
    import json as _json
    import urllib.request as _req
    import urllib.error as _urlerr

    env = environ if environ is not None else os.environ

    token = (
        (getattr(args, "linear_api_key", None) or "").strip()
        or env.get("LINEAR_API_KEY", "").strip()
        or load_local_linear_token(path=getattr(args, "credentials_path", None), environ=env)
        or ""
    )
    if not token:
        print("  No Linear API key — Starter Mission requires a Linear token.")
        return False

    runner = getattr(args, "runner", DEFAULT_RUNNER) or DEFAULT_RUNNER
    github_org = getattr(args, "github_org", None) or ""
    github_repo = getattr(args, "github_repo", None) or ""

    def _gql(query: str, variables: dict | None = None) -> dict:
        payload = _json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
        req = _req.Request(
            _LINEAR_GRAPHQL_URL,
            data=payload,
            headers={"Authorization": token, "Content-Type": "application/json"},
            method="POST",
        )
        with _req.urlopen(req, timeout=15) as resp:
            return _json.loads(resp.read())

    try:
        # Step 1: resolve team
        print("\nSetting up Starter Mission on Linear...")
        body = _gql(_STARTER_VIEWER_TEAMS_QUERY)
        teams = (((body.get("data") or {}).get("viewer") or {}).get("teams") or {}).get("nodes") or []
        if not teams:
            print("  No Linear teams found — Starter Mission requires at least one team.")
            return False
        team_id: str = teams[0]["id"]
        team_name: str = teams[0].get("name", "")
        print(f"  Using team: {team_name}")

        # Step 2: find a Todo-like state (unstarted)
        body = _gql(_STARTER_TEAM_STATES_QUERY, {"teamId": team_id})
        states = ((body.get("data") or {}).get("workflowStates") or {}).get("nodes") or []
        state_id: str | None = states[0]["id"] if states else None
        state_name: str | None = states[0].get("name") if states else None

        # Step 3: find or create project
        body = _gql(_STARTER_FIND_PROJECT_QUERY, {"name": "symphony-hello-world"})
        existing = ((body.get("data") or {}).get("projects") or {}).get("nodes") or []
        if existing:
            project = existing[0]
            project_id: str = project["id"]
            project_slug: str = project.get("slugId") or "symphony-hello-world"
            print(f"  Using existing project (slug: {project_slug})")
        else:
            print("  Creating Linear project: symphony-hello-world …")
            body = _gql(
                _STARTER_CREATE_PROJECT_MUTATION,
                {"name": "symphony-hello-world", "teamIds": [team_id]},
            )
            project_result = (body.get("data") or {}).get("projectCreate") or {}
            if not project_result.get("success"):
                errors = body.get("errors") or []
                msg = errors[0].get("message") if errors else "unknown error"
                print(f"  Failed to create project: {msg}")
                return False
            project = project_result.get("project") or {}
            project_id = project["id"]
            project_slug = project.get("slugId") or "symphony-hello-world"
            print(f"  Project created (slug: {project_slug})")

        # Step 4: find or create sample issue
        issue_title = "write hello world app"
        body = _gql(_STARTER_FIND_ISSUE_QUERY, {"projectId": project_id, "title": issue_title})
        existing_issues = ((body.get("data") or {}).get("issues") or {}).get("nodes") or []
        if existing_issues:
            print(f"  Using existing issue: {issue_title}")
        else:
            print("  Creating sample issue …")
            issue_body = _gql(
                _STARTER_CREATE_ISSUE_MUTATION,
                {
                    "title": issue_title,
                    "description": _STARTER_ISSUE_DESCRIPTION,
                    "teamId": team_id,
                    "projectId": project_id,
                    "stateId": state_id,
                },
            )
            issue_result = (issue_body.get("data") or {}).get("issueCreate") or {}
            if not issue_result.get("success"):
                errors = issue_body.get("errors") or []
                msg = errors[0].get("message") if errors else "unknown error"
                print(f"  Failed to create issue: {msg}")
                return False
            print(f"    ✓ {issue_title}")

        # Step 5: generate WORKFLOW.md
        demo_dir = Path("symphony-hello-world")
        demo_dir.mkdir(exist_ok=True)
        demo_workflow = demo_dir / "WORKFLOW.md"
        demo_config = InitConfig(
            project_slug=project_slug,
            runner=runner,
            github_org=github_org,
            github_repo=github_repo,
            active_states=(state_name,) if state_name else DEFAULT_ACTIVE_STATES,
        )
        import re as _re
        demo_content = _re.sub(
            r"(max_turns:\s*)\d+", r"\g<1>5", generate_workflow(demo_config)
        )
        write_workflow(demo_workflow, demo_content, overwrite=True)
        print(f"  Generated: {demo_workflow}")

        # Step 6: run --once if runner and repo are ready
        if runner == "claude_code" and github_org and github_repo:
            print(f"\nDispatching agent on first issue (runner: {runner}) …")
            print(_dim("This may take a few minutes. Press Ctrl-C to cancel."))
            main([str(demo_workflow), "--once"])
        else:
            print(f"\nDemo project ready. Run the agent with:")
            print(f"  {_cyan(_cli_name() + ' run ' + str(demo_workflow) + ' --once')}")

        # Step 7: record done
        from symphony.onboarding_tutorial import record_starter_mission_done  # noqa: PLC0415

        record_starter_mission_done(path=history_path, environ=environ)
        print("\nStarter Mission recorded — won't repeat on future onboard runs.")
        return True

    except _urlerr.HTTPError as exc:
        print(f"  Linear API error {exc.code}: {exc.read().decode('utf-8', errors='replace')[:200]}")
        return False
    except Exception as exc:
        print(f"  Starter Mission failed: {exc}")
        return False


def _resolve_init_mode(args: argparse.Namespace) -> str:
    if args.yes and args.mode == "interactive":
        raise OnboardingError("mode_conflict — --yes cannot be combined with --mode interactive")
    if args.yes:
        return "automated"
    if args.mode is not None:
        return args.mode
    return "interactive" if sys.stdin.isatty() else "automated"


def _automated_setup_failures(
    args: argparse.Namespace,
    *,
    project_slug: str,
    runner: str,
    github_org: str,
    github_repo: str,
    linear_token: str | None,
    github_token: str | None,
) -> list[str]:
    failures: list[str] = []
    if not project_slug.strip():
        failures.append("project slug: pass --project-slug your-linear-project-slug")

    if not _has_linear_setup_auth(linear_token, credentials_path=args.credentials_path):
        failures.append(
            "linear auth: pass --linear-api-key, set LINEAR_API_KEY, "
            "or run interactive setup"
        )

    if runner == "claude_code":
        if not github_org.strip() or not github_repo.strip():
            failures.append("github repo: pass both --github-org and --github-repo")
        command_ok, command_detail = _check_command("claude")
        if not command_ok:
            failures.append(f"claude command: {command_detail}; install Claude Code and run: claude login")
        if not _has_github_setup_auth(github_token, credentials_path=args.credentials_path):
            failures.append(
                "github auth: run: gh auth login, pass --github-token, "
                "set GITHUB_TOKEN, or run interactive setup"
            )
    else:
        command_ok, command_detail = _check_command(args.codex_command)
        if not command_ok:
            failures.append(
                f"codex command: {command_detail}; install Codex or pass --codex-command"
            )

    return failures


def _format_setup_failures(failures: Sequence[str]) -> str:
    lines = ["automated setup failed"]
    lines.extend(f"- {failure}" for failure in failures)
    return "\n".join(lines)


def setup_environment_checks(
    args: argparse.Namespace,
    *,
    environ: Mapping[str, str] | None = None,
) -> list[tuple[bool, str, str]]:
    env = environ if environ is not None else os.environ
    workflow_path = Path(args.workflow_path).expanduser()
    workflow_detail = str(workflow_path) if workflow_path.exists() else f"will create {workflow_path}"
    checks: list[tuple[bool, str, str]] = [(True, "workflow", workflow_detail)]

    linear_source = _linear_setup_auth_source(
        getattr(args, "linear_api_key", None),
        credentials_path=getattr(args, "credentials_path", None),
        environ=env,
    )
    checks.append(
        (
            linear_source is not None,
            "linear auth",
            linear_source
            or "not found — pass --linear-api-key, set LINEAR_API_KEY, or run interactive setup",
        )
    )
    if linear_source is not None:
        linear_token = (
            (getattr(args, "linear_api_key", None) or "").strip()
            or env.get("LINEAR_API_KEY", "").strip()
            or load_local_linear_token(path=getattr(args, "credentials_path", None), environ=env)
            or ""
        )
        if linear_token:
            valid_ok, valid_detail = _check_linear_key_valid(linear_token)
            checks.append((valid_ok, "linear key validity", valid_detail))

    runner = getattr(args, "runner", DEFAULT_RUNNER)
    if runner == "claude_code":
        command_ok, command_detail = _check_command("claude")
        checks.append((command_ok, "claude command", command_detail))
        if command_ok:
            login_ok, login_detail = _check_claude_login()
            checks.append((login_ok, "claude login", login_detail))
        gh_ok, gh_detail = _check_command("gh")
        checks.append((gh_ok, "gh command", gh_detail if gh_ok else f"{gh_detail} — install from cli.github.com"))
        github_source = _github_auth_source(
            getattr(args, "github_token", None),
            credentials_path=getattr(args, "credentials_path", None),
            environ=env,
        )
        checks.append(
            (
                github_source is not None,
                "github auth",
                github_source
                or "not found — run: gh auth login, pass --github-token, or set GITHUB_TOKEN",
            )
        )
        if github_source is not None:
            github_token = (
                (getattr(args, "github_token", None) or "").strip()
                or env.get("GITHUB_TOKEN", "").strip()
                or load_local_github_token(path=getattr(args, "credentials_path", None), environ=env)
                or ""
            )
            if github_token:
                scope_ok, scope_detail = _check_github_token_scopes(github_token)
                checks.append((scope_ok, "github token scopes", scope_detail))
        github_org = getattr(args, "github_org", None) or ""
        github_repo = getattr(args, "github_repo", None) or ""
        if github_org and github_repo:
            repo_detail = f"{github_org}/{github_repo}"
        else:
            repo_detail = "not configured — pass --github-org and --github-repo"
        checks.append((bool(github_org and github_repo), "github repo", repo_detail))
    else:
        command_ok, command_detail = _check_command(getattr(args, "codex_command", "codex app-server"))
        checks.append((command_ok, "codex command", command_detail))

    return checks


CHECK_WARN_PREFIX = "warn:"


def print_setup_checks(title: str, checks: Sequence[tuple[bool, str, str]]) -> None:
    """Render a tri-state status table (green=pass, yellow=warn, red=fail).

    A check is rendered as a warning when ``ok=True`` and its detail starts with
    the ``warn:`` prefix (the existing convention used by `claim guard`,
    `failure state`, and similar doctor rows that are non-fatal but worth
    surfacing). The prefix is stripped from the displayed detail so callers
    do not need to format it themselves.
    """

    print(f"\n{_bold(title)}:")
    pass_count = 0
    warn_count = 0
    fail_count = 0
    for ok, label, detail in checks:
        is_warn = bool(ok) and isinstance(detail, str) and detail.lstrip().lower().startswith(CHECK_WARN_PREFIX)
        if is_warn:
            stripped = detail.lstrip()[len(CHECK_WARN_PREFIX):].lstrip()
            icon = _warn("⚠")
            detail_text = _warn(stripped)
            warn_count += 1
        elif ok:
            icon = _ok("✓")
            detail_text = _dim(detail)
            pass_count += 1
        else:
            icon = _fail("✗")
            detail_text = _fail(detail)
            fail_count += 1
        print(f"  {icon} {label:<20} {detail_text}")

    # Single-line tally so the operator can see at a glance whether the
    # configuration is ready before reading the per-row detail. Always
    # emitted (even when the list is empty) so test harnesses can assert
    # presence; suppressed for empty lists to avoid a stray line.
    if checks:
        parts = []
        if pass_count:
            parts.append(_ok(f"{pass_count} ok"))
        if warn_count:
            parts.append(_warn(f"{warn_count} warning{'' if warn_count == 1 else 's'}"))
        if fail_count:
            parts.append(_fail(f"{fail_count} missing"))
        if parts:
            print(_dim("  ") + " · ".join(parts))


def _linear_setup_auth_source(
    token: str | None,
    *,
    credentials_path: str | Path | None,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    env = environ if environ is not None else os.environ
    if token and token.strip():
        return "--linear-api-key"
    if env.get("LINEAR_API_KEY", "").strip():
        return "LINEAR_API_KEY"
    if load_local_linear_token(path=credentials_path, environ=env) is not None:
        path = Path(credentials_path).expanduser() if credentials_path else default_credentials_path(env)
        return f"local credentials file ({path})"
    return None


def _github_auth_source(
    token: str | None,
    *,
    credentials_path: str | Path | None,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    env = environ if environ is not None else os.environ
    if token and token.strip():
        return "--github-token"
    if env.get("GITHUB_TOKEN", "").strip():
        return "GITHUB_TOKEN"
    if load_local_github_token(path=credentials_path, environ=env) is not None:
        path = Path(credentials_path).expanduser() if credentials_path else default_credentials_path(env)
        return f"local credentials file ({path})"
    gh_ok, gh_detail = _check_gh_auth()
    return f"gh ({gh_detail})" if gh_ok else None


def _linear_runtime_auth_source(
    config: WorkflowConfig,
    *,
    environ: Mapping[str, str] | None = None,
) -> str:
    env = environ if environ is not None else os.environ
    if env.get("LINEAR_API_KEY", "").strip():
        return "LINEAR_API_KEY"

    configured = config.tracker.api_key
    if configured:
        if configured.startswith("$"):
            env_name = configured[1:]
            if env.get(env_name, "").strip():
                return f"WORKFLOW.md env var {configured}"
        else:
            return "WORKFLOW.md tracker.api_key"

    if load_local_linear_token(environ=env) is not None:
        return f"local credentials file ({default_credentials_path(env)})"

    return "token resolved"


def _has_linear_setup_auth(token: str | None, *, credentials_path: str | Path | None) -> bool:
    return _linear_setup_auth_source(token, credentials_path=credentials_path) is not None


def _has_github_setup_auth(token: str | None, *, credentials_path: str | Path | None) -> bool:
    return _github_auth_source(token, credentials_path=credentials_path) is not None


def doctor_main(argv: Sequence[str] | None = None) -> int:
    parser = build_doctor_parser()
    args = parser.parse_args(argv)
    configure_logging(args.log_level)

    checks = doctor_checks(args.workflow_path, logs_root=args.logs_root, port=args.port)
    for ok, label, detail in checks:
        marker = "ok" if ok else "fail"
        print(f"[{marker}] {label}: {detail}")
    return 0 if all(ok for ok, _, _ in checks) else 2


def doctor_checks(
    workflow_path: str | Path,
    *,
    logs_root: str | Path,
    port: int,
    environ: Mapping[str, str] | None = None,
    skip_port_check: bool = False,
) -> list[tuple[bool, str, str]]:
    checks: list[tuple[bool, str, str]] = []
    try:
        context = load_startup_context(workflow_path, logs_root=logs_root, port=port, environ=environ)
    except StartupError as exc:
        checks.append((False, "workflow", str(exc)))
        return checks

    checks.append((True, "workflow", str(context.workflow_path)))
    checks.append((True, "linear auth", _linear_runtime_auth_source(context.config, environ=environ)))
    linear_token = _resolve_linear_token(context.config)
    if linear_token:
        valid_ok, valid_detail = _check_linear_key_valid(linear_token)
        checks.append((valid_ok, "linear key validity", valid_detail))

    if context.config.agent.runner == "claude_code":
        command_ok, command_check = _check_command(context.config.claude_code.command)
        checks.append((command_ok, "claude command", command_check))
        if command_ok:
            login_ok, login_detail = _check_claude_login()
            checks.append((login_ok, "claude login", login_detail))
        gh_ok, gh_check = _check_command("gh")
        checks.append((gh_ok, "gh command", gh_check if gh_ok else f"{gh_check} — install from cli.github.com"))
        github_source = _github_auth_source(None, credentials_path=None, environ=environ)
        if github_source:
            checks.append((True, "github auth", github_source))
            github_token = _resolve_github_token(environ=environ)
            if github_token:
                scope_ok, scope_detail = _check_github_token_scopes(github_token)
                checks.append((scope_ok, "github token scopes", scope_detail))
        else:
            checks.append(
                (
                    False,
                    "github auth",
                    "not found — run: gh auth login, run symphony init --github-token, or set GITHUB_TOKEN",
                )
            )
    else:
        command_ok, command_check = _check_command(context.config.codex.command)
        checks.append((command_ok, "codex command", command_check))

    workspace_ok, workspace_check = _check_workspace_root(context.config.workspace.root)
    checks.append((workspace_ok, "workspace root", workspace_check))

    logs_root = context.logs_root
    checks.append((True, "logs root", str(logs_root)))
    if not skip_port_check:
        port_ok, port_detail = _check_port_available(context.port)
        checks.append((port_ok, "status api port", port_detail))
    return checks


def build_webhooks_parser() -> argparse.ArgumentParser:
    cli = _cli_name()
    parser = argparse.ArgumentParser(
        prog=f"{cli} webhooks",
        description="Manage Linear webhooks registered against a team.",
    )
    _add_version_argument(parser)
    sub = parser.add_subparsers(dest="webhooks_command", required=True)

    # register sub-command
    reg = sub.add_parser("register", help="Register a webhook URL with a Linear team.")
    reg.add_argument("--url", required=True, help="Public HTTPS URL Linear will POST events to.")
    reg.add_argument("--team-id", required=True, help="Linear team ID.")
    reg.add_argument(
        "--secret",
        default="",
        help="HMAC secret for signature verification. Defaults to LINEAR_WEBHOOK_SECRET env var.",
    )
    reg.add_argument("--api-key", default="", help="Linear API key. Defaults to LINEAR_API_KEY env var.")

    # list sub-command
    lst = sub.add_parser("list", help="List webhooks registered for a Linear team.")
    lst.add_argument("--team-id", required=True, help="Linear team ID.")
    lst.add_argument("--api-key", default="", help="Linear API key. Defaults to LINEAR_API_KEY env var.")

    return parser


def webhooks_main(argv: Sequence[str] | None = None) -> int:
    parser = build_webhooks_parser()
    args = parser.parse_args(argv)

    api_key = args.api_key or os.environ.get("LINEAR_API_KEY", "")
    if not api_key:
        parser.exit(2, "symphony webhooks: LINEAR_API_KEY not set and --api-key not provided\n")

    from symphony.tracker.webhooks import WebhookRegistrar, WebhookRegistrarError  # noqa: PLC0415

    registrar = WebhookRegistrar(api_token=api_key)

    if args.webhooks_command == "register":
        secret = args.secret or os.environ.get("LINEAR_WEBHOOK_SECRET", "")
        if not secret:
            parser.exit(2, "symphony webhooks register: --secret or LINEAR_WEBHOOK_SECRET required\n")
        try:
            webhook_id = asyncio.run(registrar.register(args.url, args.team_id, secret))
            print(f"Registered webhook: {webhook_id}")
        except WebhookRegistrarError as exc:
            parser.exit(2, f"symphony webhooks register: {exc}\n")
        return 0

    if args.webhooks_command == "list":
        try:
            webhooks = asyncio.run(registrar.list_webhooks(args.team_id))
            if not webhooks:
                print("No webhooks registered for this team.")
            else:
                for wh in webhooks:
                    enabled = "enabled" if wh.get("enabled") else "disabled"
                    print(f"  {wh.get('id')} [{enabled}] {wh.get('url')}")
        except WebhookRegistrarError as exc:
            parser.exit(2, f"symphony webhooks list: {exc}\n")
        return 0

    return 0


def _resolve_logs_root(logs_root: str | Path, workflow_file: Path) -> Path:
    path = Path(logs_root).expanduser()
    if not path.is_absolute():
        path = workflow_file.parent / path
    return path.resolve()


def _port_value(raw: str) -> int:
    try:
        port = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port_must_be_integer") from exc
    if port <= 0 or port > 65_535:
        raise argparse.ArgumentTypeError("port_must_be_1_to_65535")
    return port


def _check_port_available(port: int) -> tuple[bool, str]:
    """Return (available, message). Tries to bind the port to detect conflicts."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", port))
        return True, f"http://127.0.0.1:{port}"
    except OSError:
        return (
            False,
            f"port {port} already in use — another symphony process may be running. "
            f"Use --port to pick a different port (e.g. --port {port + 1}).",
        )


def _check_gh_auth() -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return False, "not authenticated — run: gh auth login"
        output = result.stdout + result.stderr
        m = re.search(r"Logged in to \S+ account (\S+)", output)
        account = m.group(1) if m else None
        detail = f"authenticated ({account})" if account else "authenticated"
        return True, detail
    except FileNotFoundError:
        return False, "gh CLI not found — install from cli.github.com"
    except Exception as exc:
        return False, str(exc)


def _detect_github_from_remote() -> tuple[str, str] | tuple[None, None]:
    """Parse GitHub org and repo from the `origin` git remote URL."""
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return None, None
        url = result.stdout.strip()
        # SSH: git@github.com:org/repo.git
        m = re.match(r"git@github\.com:([^/]+)/([^/]+?)(?:\.git)?$", url)
        if m:
            return m.group(1), m.group(2)
        # ssh:// URL: ssh://git@github.com/org/repo.git
        m = re.match(r"ssh://git@github\.com/([^/]+)/([^/]+?)(?:\.git)?$", url)
        if m:
            return m.group(1), m.group(2)
        # HTTPS: https://github.com/org/repo.git
        m = re.match(r"https://github\.com/([^/]+)/([^/]+?)(?:\.git)?$", url)
        if m:
            return m.group(1), m.group(2)
        return None, None
    except Exception:
        return None, None


def _resolve_linear_token(config: WorkflowConfig) -> str | None:
    try:
        return TokenStore(config.tracker).resolve_linear_token()
    except Exception:
        return None


def _resolve_github_token(
    credentials_path: Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    env = environ if environ is not None else os.environ
    token = env.get("GITHUB_TOKEN")
    if token:
        return token
    return load_local_github_token(path=credentials_path, environ=env)


def _validate_github_token(token: str) -> str | None:
    """Return the authenticated GitHub username, or None if the token is invalid."""
    import json as _json
    import urllib.request as _req
    try:
        request = _req.Request(
            "https://api.github.com/user",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        )
        with _req.urlopen(request, timeout=10) as resp:
            data = _json.loads(resp.read())
            return data.get("login") or "authenticated"
    except Exception:
        return None


def _codex_turn_sandbox_policy(config: WorkflowConfig) -> dict[str, object] | None:
    if config.codex.turn_sandbox_policy is None:
        return None
    return {"type": config.codex.turn_sandbox_policy}


def _workflow_mtime_ns(workflow_path: Path) -> int | None:
    try:
        return workflow_path.stat().st_mtime_ns
    except FileNotFoundError:
        return None


def _prompt(label: str) -> str:
    return input(f"{label}: ").strip()


def _prompt_default(label: str, default: str) -> str:
    value = input(f"{label} [{default}]: ").strip()
    return value or default


def _parse_github_input(value: str) -> tuple[str, str]:
    """Extract (org, repo) from a raw user input string.

    Handles full HTTPS URLs, SSH remote URLs, and 'org/repo' shorthand.
    Returns ('', '') when nothing useful can be parsed.
    """
    import re
    v = value.strip().rstrip("/").removesuffix(".git")
    # https://github.com/org  or  https://github.com/org/repo
    m = re.match(r"https?://(?:www\.)?github\.com/([^/]+)(?:/([^/]+))?", v, re.IGNORECASE)
    if m:
        return m.group(1), m.group(2) or ""
    # git@github.com:org/repo
    m = re.match(r"git@github\.com:([^/]+)/([^/]+)", v, re.IGNORECASE)
    if m:
        return m.group(1), m.group(2)
    # org/repo shorthand
    if "/" in v:
        parts = v.split("/", 1)
        return parts[0], parts[1]
    return v, ""


def _parse_linear_slug(value: str) -> str:
    """Extract a project slug from a raw user input string.

    Handles full Linear project URLs like:
      https://linear.app/team/project/name-abc123def
    Strips surrounding whitespace and internal spaces so accidental
    copy-paste padding does not produce an invalid slug.
    Returns the slug portion (last path segment) or the cleaned value.
    """
    import re
    v = value.strip().rstrip("/")
    m = re.match(r"https?://linear\.app/[^/]+/project/([^/]+)", v, re.IGNORECASE)
    slug = m.group(1) if m else v
    return slug.replace(" ", "")


def _check_command(command: str) -> tuple[bool, str]:
    try:
        parts = shlex.split(command)
    except ValueError as exc:
        return False, f"invalid command: {exc}"
    if not parts:
        return False, "missing command"
    executable = shutil.which(parts[0])
    if executable is None:
        return False, f"missing executable: {parts[0]}"
    return True, executable


def _check_claude_login() -> tuple[bool, str]:
    """Heuristic: check if Claude CLI has been configured by looking for its config directory."""
    home = Path.home()
    candidates = [home / ".config" / "claude", home / ".claude"]
    for config_dir in candidates:
        if config_dir.is_dir() and any(config_dir.iterdir()):
            return True, f"config dir found ({config_dir})"
    if (home / ".claude.json").exists():
        return True, "config found (~/.claude.json)"
    return False, "not configured — run: claude login"


def _check_linear_key_valid(token: str, endpoint: str = "https://api.linear.app/graphql") -> tuple[bool, str]:
    """Validate Linear API key by calling viewer { id name }. Only fails on auth errors, not network errors."""
    import json as _json
    import urllib.request as _req
    import urllib.error as _urlerr

    payload = _json.dumps({"query": "query { viewer { id name } }"}).encode("utf-8")
    request = _req.Request(
        endpoint,
        data=payload,
        headers={"Authorization": token, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with _req.urlopen(request, timeout=10) as resp:
            data = _json.loads(resp.read())
            viewer = (data.get("data") or {}).get("viewer")
            if not viewer:
                return False, "invalid key — check LINEAR_API_KEY or run: symphony init --linear-api-key"
            name = viewer.get("name") or viewer.get("id") or "authenticated"
            return True, f"valid (logged in as {name})"
    except _urlerr.HTTPError as exc:
        if exc.code in (401, 403):
            return False, "invalid key — check LINEAR_API_KEY or run: symphony init --linear-api-key"
        return True, f"key present (http {exc.code} — could not verify)"
    except Exception:
        return True, "key present (network check skipped)"


def _check_github_token_scopes(token: str) -> tuple[bool, str]:
    """Check GitHub token has repo write scopes via X-OAuth-Scopes header.

    Fine-grained PATs return an empty scopes header — treated as passing since
    scope verification is not available for them via this endpoint.
    Only fails when classic token scopes are present and repo write is absent.
    """
    import urllib.request as _req
    import urllib.error as _urlerr

    request = _req.Request(
        "https://api.github.com/user",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
    )
    try:
        with _req.urlopen(request, timeout=10) as resp:
            scopes_raw = resp.headers.get("X-OAuth-Scopes", "")
            scopes = {s.strip() for s in scopes_raw.split(",") if s.strip()}
            if not scopes:
                return True, "fine-grained PAT — scopes not verifiable via API"
            if "repo" in scopes or "public_repo" in scopes:
                return True, f"write scopes confirmed ({', '.join(sorted(scopes))})"
            return (
                False,
                f"missing repo write scope — found: {', '.join(sorted(scopes))}; "
                "token needs 'repo' (classic) or Contents + Pull-requests write (fine-grained)",
            )
    except _urlerr.HTTPError as exc:
        if exc.code in (401, 403):
            return False, "invalid token"
        return True, f"token present (http {exc.code} — scope check skipped)"
    except Exception:
        return True, "token present (network check skipped)"


def _check_workspace_root(path: Path) -> tuple[bool, str]:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".symphony-write-check"
        probe.write_text("ok\n", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return False, f"not writable: {exc}"
    return True, str(path)


def _content_length(raw: str | None) -> int:
    if raw is None:
        return 0
    try:
        return max(int(raw), 0)
    except ValueError:
        return 0


# ---------------------------------------------------------------------------
# sy project — dashboard listing all configured and unconfigured projects
# ---------------------------------------------------------------------------

_PROJECT_LIST_GQL = """
query ProjectList($cursor: String) {
  projects(first: 50, after: $cursor) {
    nodes {
      id
      name
      slugId
    }
    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
"""

_PROJECT_ISSUES_GQL = """
query ProjectIssues($projectId: ID!, $cursor: String) {
  project(id: $projectId) {
    issues(first: 500, after: $cursor) {
      nodes {
        state { type }
      }
      pageInfo {
        hasNextPage
        endCursor
      }
    }
  }
}
"""


def build_project_parser() -> argparse.ArgumentParser:
    cli = _cli_name()
    parser = argparse.ArgumentParser(
        prog=f"{cli} project",
        description="List all Linear projects and their workflow status.",
    )
    parser.add_argument(
        "--linear-api-key",
        metavar="KEY",
        default=None,
        help="Linear personal API key. Defaults to LINEAR_API_KEY env var.",
    )
    return parser


def project_main(argv: Sequence[str] | None = None) -> int:
    parser = build_project_parser()
    args = parser.parse_args(argv)

    token: str | None = getattr(args, "linear_api_key", None) or os.environ.get("LINEAR_API_KEY")
    if not token:
        try:
            from symphony.auth import TokenStore
            from symphony.config import TrackerConfig
            dummy = TrackerConfig(kind="linear")
            token = TokenStore(dummy).resolve_linear_token()
        except Exception:
            pass
    if not token:
        print(_fail("No Linear API key found."))
        print(_dim("  Set LINEAR_API_KEY or run: sy onboard"))
        return 1

    cwd = Path.cwd()

    # --- find all WORKFLOW.md files up to depth 3 ---
    workflow_files: list[Path] = []
    seen: set[Path] = set()
    for pat in ("WORKFLOW.md", "*/WORKFLOW.md", "*/*/WORKFLOW.md", "*/*/*/WORKFLOW.md"):
        for p in cwd.glob(pat):
            if p not in seen:
                seen.add(p)
                workflow_files.append(p)
    workflow_files.sort()

    # --- parse slugs from each WORKFLOW.md ---
    slug_to_path: dict[str, str] = {}
    for wf_path in workflow_files:
        try:
            wf_def = load_workflow(wf_path)
            slug = WorkflowConfig.from_mapping(wf_def.config).tracker.project_slug
            if slug:
                rel_str = str(wf_path.relative_to(cwd))
                display = "./" + rel_str if not rel_str.startswith("..") else rel_str
                slug_to_path[slug] = display
        except Exception:
            pass

    # --- detect running symphony processes ---
    running_paths: set[str] = _detect_running_workflow_paths()

    # --- fetch Linear data ---
    import json as _json
    import urllib.request as _req

    def _gql(query: str, variables: dict | None = None) -> dict:
        payload = _json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
        req = _req.Request(
            _LINEAR_GRAPHQL_URL,
            data=payload,
            headers={"Authorization": token, "Content-Type": "application/json"},
            method="POST",
        )
        with _req.urlopen(req, timeout=15) as resp:
            body = _json.loads(resp.read())
        if "errors" in body:
            msgs = "; ".join(e.get("message", str(e)) for e in body["errors"])
            raise RuntimeError(f"Linear API error: {msgs}")
        return body

    try:
        linear_projects: list[dict] = []
        cursor: str | None = None
        while True:
            body = _gql(_PROJECT_LIST_GQL, {"cursor": cursor})
            page = (body.get("data") or {}).get("projects") or {}
            linear_projects.extend(page.get("nodes") or [])
            page_info = page.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                break
            cursor = page_info.get("endCursor")
    except Exception as exc:
        print(_fail(f"Failed to reach Linear API: {exc}"))
        return 1

    # --- build index: slug → linear project data ---
    # WORKFLOW.md stores the full slug (name-shortid), but Linear's GraphQL slugId
    # returns only the short ID portion. Match by exact or suffix.
    linear_by_slug: dict[str, dict] = {p["slugId"]: p for p in linear_projects if p.get("slugId")}

    def _find_linear_project(workflow_slug: str) -> dict | None:
        if workflow_slug in linear_by_slug:
            return linear_by_slug[workflow_slug]
        for api_slug, proj in linear_by_slug.items():
            if workflow_slug.endswith(api_slug):
                return proj
        return None

    # Cache to avoid duplicate API calls for the same project
    _issue_counts_cache: dict[str, tuple[int, int, int]] = {}

    def _issue_counts(project_id: str) -> tuple[int, int, int]:
        if project_id in _issue_counts_cache:
            return _issue_counts_cache[project_id]
        done = active = open_ = 0
        page_cursor: str | None = None
        while True:
            try:
                resp = _gql(_PROJECT_ISSUES_GQL, {"projectId": project_id, "cursor": page_cursor})
                issues_data = (((resp.get("data") or {}).get("project") or {}).get("issues") or {})
                nodes = issues_data.get("nodes") or []
            except Exception:
                break
            for issue in nodes:
                t = (issue.get("state") or {}).get("type") or ""
                if t in ("completed", "canceled"):
                    done += 1
                elif t == "started":
                    active += 1
                else:
                    open_ += 1
            page_info = issues_data.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                break
            page_cursor = page_info.get("endCursor")
        result = (done, active, open_)
        _issue_counts_cache[project_id] = result
        return result

    def _is_running(display_path: str) -> bool:
        abs_path = str((cwd / display_path).resolve())
        return abs_path in running_paths

    def _fmt_issue_counts(done: int, active: int, open_: int) -> str:
        parts = [_ok(f"{done} done")]
        sep = _dim(" · ")
        if active:
            parts.append(_purple(f"{active} active"))
        else:
            parts.append(_dim(f"{active} active"))
        parts.append(_cyan(f"{open_} open"))
        return sep.join(parts)

    # --- render configured projects ---
    # Each row: (indicator, name, workflow, issues, status, next_step)
    configured_rows: list[tuple[str, str, str, str, str, str]] = []
    configured_slugs: set[str] = set()

    for slug, wf_str in slug_to_path.items():
        configured_slugs.add(slug)
        linear_proj = _find_linear_project(slug)
        if linear_proj:
            configured_slugs.add(linear_proj["slugId"])
        raw_name = linear_proj["name"] if linear_proj else slug
        if linear_proj:
            done, active, open_ = _issue_counts(linear_proj["id"])
            issues_str = _fmt_issue_counts(done, active, open_)
        else:
            issues_str = _dim("project not found in Linear")

        running = _is_running(wf_str)
        cli = _cli_name()
        if running:
            indicator = _ok("▶")
            name = _bold(raw_name)
            wf_col = _dim(wf_str)
            status = _ok("running")
            next_step = _dim(f"sy doctor {wf_str}")
        else:
            indicator = _dim("○")
            name = _dim(raw_name)
            wf_col = _dim(wf_str)
            status = _dim("stopped")
            next_step = _dim(f"{cli} run {wf_str}")
        configured_rows.append((indicator, name, wf_col, issues_str, status, next_step))

    # --- unconfigured: Linear projects with no WORKFLOW.md ---
    unconfigured_rows: list[tuple[str, str, str]] = []
    for proj in linear_projects:
        slug = proj.get("slugId") or ""
        if slug and slug not in configured_slugs:
            name = proj.get("name") or slug
            done, active, open_ = _issue_counts(proj["id"])
            total = done + active + open_
            issues_str = _dim(f"{total} issues · no workflow")
            cli = _cli_name()
            next_step = _dim(f"{cli} run {slug}/WORKFLOW.md")
            unconfigured_rows.append((name, issues_str, next_step))

    # --- print ---
    _print_project_table(configured_rows, unconfigured_rows)
    return 0


def _proc_cwd(pid: str) -> str | None:
    """Return the working directory of a process by PID (Linux /proc or macOS lsof)."""
    try:
        link = Path(f"/proc/{pid}/cwd")
        if link.is_symlink():
            return str(link.resolve())
    except Exception:
        pass
    try:
        out = subprocess.run(
            ["lsof", "-p", pid, "-a", "-d", "cwd", "-Fn"],
            capture_output=True, text=True, timeout=3,
        ).stdout
        for ln in out.splitlines():
            if ln.startswith("n"):
                return ln[1:]
    except Exception:
        pass
    return None


def _detect_running_workflow_paths() -> set[str]:
    """Return set of absolute WORKFLOW.md paths found in running symphony processes."""
    try:
        result = subprocess.run(["ps", "aux"], capture_output=True, text=True, timeout=5)
        paths: set[str] = set()
        for line in result.stdout.splitlines():
            if ("symphony" in line or "sy " in line) and "run" in line:
                pid_m = re.match(r'\S+\s+(\d+)', line)
                pid = pid_m.group(1) if pid_m else None
                m = re.search(r'(?:symphony|sy)\s+run\s+(\S+\.md)', line)
                wf_arg = m.group(1) if m else "WORKFLOW.md"
                if os.path.isabs(wf_arg):
                    paths.add(wf_arg)
                elif pid:
                    proc_dir = _proc_cwd(pid)
                    if proc_dir:
                        paths.add(str(Path(proc_dir).joinpath(wf_arg).resolve()))
        return paths
    except Exception:
        return set()


def _print_project_table(
    configured: list[tuple[str, str, str, str, str, str]],
    unconfigured: list[tuple[str, str, str]],
) -> None:
    if not configured and not unconfigured:
        print(_dim("No Linear projects found."))
        return

    COL_SEP = "  "
    # Columns: indicator | name | workflow | issues | status | next_step
    headers = ("", "LINEAR PROJECT", "WORKFLOW", "ISSUES", "STATUS", "NEXT STEP")

    def _vis(s: str) -> int:
        return len(re.sub(r'\033\[[0-9;]*m', '', s))

    def _pad(s: str, width: int) -> str:
        return s + " " * max(0, width - _vis(s))

    if configured:
        col_widths = [len(h) for h in headers]
        for row in configured:
            for i, cell in enumerate(row):
                col_widths[i] = max(col_widths[i], _vis(cell))

        # Print header (skip indicator column)
        header_cells = [_pad(h, col_widths[i]) for i, h in enumerate(headers)]
        print(_dim(COL_SEP.join(header_cells)))

        for indicator, name, wf, issues, status, next_step in configured:
            cells = [indicator, name, wf, issues, status, next_step]
            print(COL_SEP.join(_pad(cell, col_widths[i]) for i, cell in enumerate(cells)))

    if unconfigured:
        if configured:
            print()
        print(_dim("── Linear projects with no workflow configured ──"))
        unc_widths = [2, 20, 20, 20]  # icon+name | issues | next_step
        rows_display = [(_warn("! ") + name, issues, next_step) for name, issues, next_step in unconfigured]
        widths = [0] * 3
        for row in rows_display:
            for i, cell in enumerate(row):
                widths[i] = max(widths[i], _vis(cell))
        for row in rows_display:
            print(COL_SEP.join(_pad(cell, widths[i]) for i, cell in enumerate(row)))

    print()
    legend = (
        _ok("▶ running") + _dim(" = dispatch loop active  ·  ") +
        _dim("○ stopped") + _dim(" = configured, not running  ·  ") +
        _warn("! not configured") + _dim(" = first run opens setup wizard")
    )
    print(legend)
    print(
        _dim("Stop:  ") + _cyan("Ctrl-C") + _dim("  ·  kill <PID>  ·  pkill -f 'symphony run'")
    )
    print(_dim("PID:   ps aux | grep 'symphony run'"))


# ---------------------------------------------------------------------------
# sy run first-run wizard — triggered when WORKFLOW.md is missing
# ---------------------------------------------------------------------------

def _run_first_run_wizard(workflow_path: Path) -> bool:
    """Interactive setup wizard invoked by `sy run` when WORKFLOW.md is missing.

    Collects project slug, runner, GitHub repo, and workspace root, then writes
    the WORKFLOW.md. Returns True on success, False if the user aborts.
    """
    cli = _cli_name()
    print(f"\n{_bold('No WORKFLOW.md found at')} {_cyan(str(workflow_path))}")
    print(_dim("Let's set up this project. Press Ctrl-C at any time to cancel.\n"))

    try:
        answer = input("Set up a new project here? [Y/n] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if answer and answer not in ("y", "yes"):
        return False

    # Auto-detect GitHub org/repo from git remote
    detected_org, detected_repo = _detect_github_from_remote()

    try:
        # Step 1: project slug
        print(f"\n{_bold('Step 1/4')} — Linear project slug")
        print(_dim("  Find it in the project URL: linear.app/YOUR-TEAM/project/NAME-") + _cyan("<slug>"))
        project_slug = _parse_linear_slug(_prompt("Linear project slug"))
        if not project_slug:
            print(_fail("  Project slug is required."))
            return False

        # Step 2: runner
        print(f"\n{_bold('Step 2/4')} — Agent runner")
        runner_input = _prompt_default("Runner (claude_code / codex)", DEFAULT_RUNNER).strip().lower().replace("-", "_")
        if runner_input not in ("claude_code", "codex"):
            print(_warn(f"  Unknown runner '{runner_input}', using {DEFAULT_RUNNER}."))
            runner_input = DEFAULT_RUNNER
        runner = runner_input

        # Step 3: GitHub repo (shown for claude_code; optional for codex)
        github_org = detected_org or ""
        github_repo = detected_repo or ""
        print(f"\n{_bold('Step 3/4')} — GitHub repository")
        if detected_org and detected_repo:
            print(_dim(f"  Detected from git remote: {detected_org}/{detected_repo}"))
        print(_dim("  Agents will clone this repo, push a branch, and open a PR."))
        if detected_org:
            raw_org = _prompt_default("GitHub org/user", detected_org)
        else:
            raw_org = _prompt("GitHub org/user (blank to fill in later)")
        parsed_org, parsed_repo_from_org = _parse_github_input(raw_org)
        github_org = parsed_org
        # If user pasted a full URL containing the repo too, carry it forward as default.
        if parsed_repo_from_org:
            github_repo = parsed_repo_from_org

        repo_default = github_repo or detected_repo or ""
        if repo_default:
            raw_repo = _prompt_default("Repository name", repo_default)
        else:
            raw_repo = _prompt("Repository name (blank to fill in later)")
        org_or_name, parsed_repo = _parse_github_input(raw_repo)
        github_repo = parsed_repo or org_or_name or raw_repo

        # Step 4: workspace root
        print(f"\n{_bold('Step 4/4')} — Workspace root")
        print(_dim("  Local directory where per-issue worktrees are created."))
        workspace_root = _prompt_default(
            "Workspace root", default_workspace_root(project_slug)
        ).strip() or default_workspace_root(project_slug)

    except (EOFError, KeyboardInterrupt):
        print()
        print(_dim("Setup cancelled."))
        return False

    # Write WORKFLOW.md
    workflow_path.parent.mkdir(parents=True, exist_ok=True)
    content = generate_workflow(
        InitConfig(
            project_slug=project_slug,
            preset=DEFAULT_PRESET,
            active_states=DEFAULT_ACTIVE_STATES,
            terminal_states=DEFAULT_TERMINAL_STATES,
            workspace_root=workspace_root,
            runner=runner,
            github_org=github_org,
            github_repo=github_repo,
        )
    )
    try:
        write_workflow(str(workflow_path), content, overwrite=False)
    except OnboardingError:
        print(_fail(f"  {workflow_path} already exists."))
        return False
    except OSError as exc:
        print(_fail(f"  Failed to write {workflow_path}: {exc}"))
        return False
    print(f"\n{_ok('✓')} Wrote {workflow_path}")
    print(_dim(f"  Tip: run `{cli} doctor {workflow_path}` to validate before the loop starts.\n"))
    return True


if __name__ == "__main__":
    sys.exit(main())
