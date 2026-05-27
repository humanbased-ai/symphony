from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from symphony.agents.base import (
    AgentEvent,
    AgentEventCallback,
    AgentEventType,
    AgentRunnerError,
    AgentSession,
    CLIAgentRunner,
    TokenUsage,
    TurnResult,
)
from symphony.tracker.models import Issue


DEFAULT_CLAUDE_COMMAND = "claude"
DEFAULT_TURN_TIMEOUT_MS = 3_600_000
DEFAULT_PERMISSION_MODE = "bypassPermissions"

_LINEAR_SYSTEM_PROMPT = (
    "Access Linear via LINEAR_API_KEY using GraphQL: "
    "curl -s -X POST https://api.linear.app/graphql "
    '-H "Authorization: $LINEAR_API_KEY" '
    '-H "Content-Type: application/json" '
    "-d '{\"query\":\"GRAPHQL\"}'. "
    "Only call when reporting meaningful progress or updating state."
)


@dataclass
class _ClaudeSessionState:
    session_id: str | None = None


class ClaudeCodeRunner(CLIAgentRunner):
    """Claude Code CLI runner using --print --output-format stream-json."""

    name = "claude_code"

    def __init__(
        self,
        command: str = DEFAULT_CLAUDE_COMMAND,
        *,
        model: str | None = None,
        permission_mode: str = DEFAULT_PERMISSION_MODE,
        turn_timeout_ms: int = DEFAULT_TURN_TIMEOUT_MS,
        linear_api_key: str | None = None,
        github_token: str | None = None,
    ) -> None:
        super().__init__(command)
        self.model = model
        self.permission_mode = permission_mode
        self.turn_timeout_ms = turn_timeout_ms
        self.linear_api_key = linear_api_key
        self.github_token = github_token

    async def start_session(
        self,
        workspace: Path,
        *,
        worker_host: str | None = None,
    ) -> AgentSession:
        if worker_host is not None:
            raise AgentRunnerError("remote_claude_worker_not_supported")

        workspace = Path(workspace).expanduser().resolve()
        if not workspace.exists() or not workspace.is_dir():
            raise AgentRunnerError("invalid_workspace_cwd")

        state = _ClaudeSessionState()
        return AgentSession(
            id=f"claude-{workspace.name}",
            workspace=workspace,
            metadata={"claude_state": state},
        )

    async def run_turn(
        self,
        session: AgentSession,
        prompt: str,
        issue: Issue,
        on_event: AgentEventCallback,
    ) -> TurnResult:
        state: _ClaudeSessionState = session.metadata["claude_state"]
        cmd = self._build_command(session.workspace, session_id=state.session_id)
        env = self._build_env()

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(session.workspace),
                env=env,
                limit=4 * 1024 * 1024,  # 4 MB; default 64 KB triggers LimitOverrunError on large JSON lines
            )
        except FileNotFoundError as exc:
            raise AgentRunnerError("claude_not_found") from exc
        except OSError as exc:
            raise AgentRunnerError(f"claude_launch_failed:{exc}") from exc

        if process.stdin is None:
            raise AgentRunnerError("claude_stdin_unavailable")

        process.stdin.write(prompt.encode())
        await process.stdin.drain()
        process.stdin.close()

        stderr_task = asyncio.create_task(_drain_stderr(process))
        try:
            result = await asyncio.wait_for(
                self._read_events(process, issue, session, on_event, state),
                timeout=self.turn_timeout_ms / 1000,
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            return TurnResult(success=False, exit_reason="turn_timeout")
        finally:
            await asyncio.gather(stderr_task, return_exceptions=True)

        return result

    async def stop_session(self, session: AgentSession) -> None:
        pass  # process exits after each --print invocation

    def _build_command(self, workspace: Path, *, session_id: str | None) -> tuple[str, ...]:
        cmd: list[str] = list(self.command)
        cmd += [
            "--print",
            "--verbose",
            "--output-format", "stream-json",
            "--permission-mode", self.permission_mode,
            "--add-dir", str(workspace),
        ]
        if self.model:
            cmd += ["--model", self.model]
        if session_id:
            cmd += ["--resume", session_id]
        if self.linear_api_key:
            cmd += ["--append-system-prompt", _LINEAR_SYSTEM_PROMPT]
        return tuple(cmd)

    def _build_env(self) -> dict[str, str]:
        env = dict(os.environ)
        if self.linear_api_key:
            env["LINEAR_API_KEY"] = self.linear_api_key
        if self.github_token:
            env["GITHUB_TOKEN"] = self.github_token
        return env

    async def _read_events(
        self,
        process: asyncio.subprocess.Process,
        issue: Issue,
        session: AgentSession,
        on_event: AgentEventCallback,
        state: _ClaudeSessionState,
    ) -> TurnResult:
        if process.stdout is None:
            raise AgentRunnerError("claude_stdout_unavailable")

        usage: TokenUsage | None = None
        session_started = False

        while True:
            line = await process.stdout.readline()
            if not line:
                break

            try:
                event = json.loads(line.decode())
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue

            event_type = event.get("type")

            if event_type == "system" and event.get("subtype") == "init":
                sid = event.get("session_id")
                if sid:
                    state.session_id = sid
                if not session_started:
                    session_started = True
                    await on_event(AgentEvent(
                        type=AgentEventType.SESSION_STARTED,
                        issue_id=issue.id,
                        issue_identifier=issue.identifier,
                        session_id=sid or session.id,
                        message="Claude Code session started.",
                        data={"event": "session_started", "session_id": sid},
                    ))

            elif event_type == "assistant":
                content = event.get("message", {}).get("content", [])
                text = _extract_text(content)
                if text:
                    await on_event(AgentEvent(
                        type=AgentEventType.NOTIFICATION,
                        issue_id=issue.id,
                        issue_identifier=issue.identifier,
                        session_id=state.session_id or session.id,
                        message=text[:500],
                        data={"event": "assistant_message"},
                    ))
                # Tool routing: each tool_use block becomes its own NOTIFICATION
                # so operators can see "Claude called Bash: …" in the dashboard.
                for tool_use in _extract_tool_uses(content):
                    summary = _summarize_tool_input(tool_use["name"], tool_use["input"])
                    await on_event(AgentEvent(
                        type=AgentEventType.NOTIFICATION,
                        issue_id=issue.id,
                        issue_identifier=issue.identifier,
                        session_id=state.session_id or session.id,
                        message=f"{tool_use['name']}: {summary}"[:500],
                        data={
                            "event": "tool_use",
                            "tool_name": tool_use["name"],
                            "tool_use_id": tool_use.get("id"),
                            "tool_input": tool_use.get("input"),
                        },
                    ))

            elif event_type == "user":
                # Tool results return as user-role messages with tool_result
                # blocks. Surface only failures by default (success is implied
                # by the next assistant message); errors are worth observing.
                content = event.get("message", {}).get("content", [])
                for tool_result in _extract_tool_results(content):
                    if not tool_result.get("is_error"):
                        continue
                    snippet = _extract_text_content(tool_result.get("content"))
                    await on_event(AgentEvent(
                        type=AgentEventType.NOTIFICATION,
                        issue_id=issue.id,
                        issue_identifier=issue.identifier,
                        session_id=state.session_id or session.id,
                        message=f"tool_error: {snippet[:400]}",
                        data={
                            "event": "tool_result_error",
                            "tool_use_id": tool_result.get("tool_use_id"),
                        },
                    ))

            elif event_type == "result":
                sid = event.get("session_id")
                if sid:
                    state.session_id = sid
                raw_usage = event.get("usage") or {}
                if raw_usage:
                    usage = TokenUsage.from_input_output(
                        raw_usage.get("input_tokens", 0),
                        raw_usage.get("output_tokens", 0),
                    )
                subtype = event.get("subtype", "")
                stop_reason = event.get("stop_reason", "")
                is_error = event.get("is_error", False)
                result_text = event.get("result", "")
                cache_creation = raw_usage.get("cache_creation_input_tokens", 0)
                cache_read = raw_usage.get("cache_read_input_tokens", 0)

                exit_reason = _claude_exit_reason(
                    is_error=is_error,
                    subtype=subtype,
                    stop_reason=stop_reason,
                )

                event_data: dict[str, Any] = {
                    "subtype": subtype,
                    "stop_reason": stop_reason,
                }
                if cache_creation or cache_read:
                    event_data["cache_tokens"] = {
                        "creation": cache_creation,
                        "read": cache_read,
                    }

                if is_error or subtype.startswith("error") or stop_reason in {"max_tokens", "stop_sequence"}:
                    event_data["event"] = "turn_failed"
                    await on_event(AgentEvent(
                        type=AgentEventType.TURN_FAILED,
                        issue_id=issue.id,
                        issue_identifier=issue.identifier,
                        session_id=state.session_id or session.id,
                        message=result_text or exit_reason or "claude_error",
                        data=event_data,
                    ))
                    return TurnResult(
                        success=False,
                        exit_reason=exit_reason,
                        usage=usage,
                    )

                event_data["event"] = "turn_completed"
                await on_event(AgentEvent(
                    type=AgentEventType.TURN_COMPLETED,
                    issue_id=issue.id,
                    issue_identifier=issue.identifier,
                    session_id=state.session_id or session.id,
                    message=(result_text[:500] if result_text else "Turn completed."),
                    data=event_data,
                ))
                return TurnResult(success=True, exit_reason=exit_reason, usage=usage)

        returncode = await process.wait()
        return TurnResult(
            success=returncode == 0,
            exit_reason=f"claude_exited:{returncode}",
            usage=usage,
        )


def _extract_text(content: list[Any]) -> str:
    parts = [
        block["text"]
        for block in content
        if isinstance(block, dict) and block.get("type") == "text" and block.get("text")
    ]
    return " ".join(parts)


def _extract_tool_uses(content: list[Any]) -> list[dict[str, Any]]:
    """Pull tool_use blocks out of an assistant message's content."""
    return [
        block
        for block in content
        if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name")
    ]


def _extract_tool_results(content: list[Any]) -> list[dict[str, Any]]:
    """Pull tool_result blocks out of a user message's content."""
    return [
        block
        for block in content
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]


def _extract_text_content(content: Any) -> str:
    """Normalize tool_result.content (string or block list) to a string."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return _extract_text(content)
    return ""


def _summarize_tool_input(tool_name: str, tool_input: Any) -> str:
    """Produce a short, log-friendly summary of a tool call's input.

    Avoids dumping full Edit diffs or Read content into the event log while
    still giving operators a recognizable signal (file path, bash command,
    glob pattern, etc.). Falls back to the tool name when nothing matches.
    """

    if not isinstance(tool_input, dict):
        return ""
    # Common Claude Code tool input shapes.
    for key in ("command", "file_path", "path", "pattern", "url", "query", "description"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:200]
    return ""


# Map Claude's stop_reason values into a stable Symphony exit_reason string.
# `end_turn` is the normal completion; `tool_use` means the turn ended because
# Claude wants a tool result (handled separately in the streaming loop, so by
# the time we see it in the result event it's the terminal stop).
_CLAUDE_STOP_REASON_MAP = {
    "end_turn": "turn_completed",
    "tool_use": "turn_completed",
    "stop_sequence": "stop_sequence",
    "max_tokens": "max_tokens",
    "refusal": "safety_refusal",
}


def _claude_exit_reason(*, is_error: bool, subtype: str, stop_reason: str) -> str:
    if is_error:
        # subtype="success" with is_error=True is a contradiction — return a
        # clear error name rather than the misleading "success" string.
        return subtype if subtype and subtype != "success" else "claude_error"
    if subtype.startswith("error"):
        return subtype
    if stop_reason in _CLAUDE_STOP_REASON_MAP:
        return _CLAUDE_STOP_REASON_MAP[stop_reason]
    # subtype="success" with no stop_reason is the common success path —
    # preserve the legacy `turn_completed` exit_reason for back-compat with
    # `complete_worker_success` and dashboard counters keyed on it.
    return "turn_completed"


async def _drain_stderr(process: asyncio.subprocess.Process) -> None:
    if process.stderr is None:
        return
    try:
        data = await process.stderr.read()
        if data:
            import logging
            logging.getLogger(__name__).debug("claude stderr: %s", data.decode(errors="replace").strip())
    except Exception:
        pass
