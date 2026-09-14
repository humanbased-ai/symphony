from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
import shlex
from typing import Any

from jazzband.tracker.models import Issue


AgentEventCallback = Callable[["AgentEvent"], Awaitable[None]]


class AgentRunnerError(RuntimeError):
    """Raised when an agent runner cannot complete its contract."""


class AgentEventType(StrEnum):
    SESSION_STARTED = "session_started"
    TURN_COMPLETED = "turn_completed"
    TURN_FAILED = "turn_failed"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    NOTIFICATION = "notification"
    MALFORMED = "malformed"
    ERROR = "error"


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0

    def __post_init__(self) -> None:
        if self.input_tokens < 0 or self.output_tokens < 0 or self.total_tokens < 0:
            raise ValueError("token_usage_must_be_non_negative")
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("token_usage_total_mismatch")

    @classmethod
    def from_input_output(cls, input_tokens: int, output_tokens: int) -> "TokenUsage":
        return cls(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
        )

    def merge(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
        )


@dataclass(frozen=True)
class AgentEvent:
    type: AgentEventType
    message: str = ""
    issue_id: str | None = None
    issue_identifier: str | None = None
    session_id: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        object.__setattr__(self, "type", AgentEventType(self.type))


@dataclass(frozen=True)
class AgentSession:
    id: str
    workspace: Path
    worker_host: str | None = None
    process_id: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "workspace", Path(self.workspace).expanduser().resolve())


@dataclass(frozen=True)
class TurnResult:
    success: bool
    exit_reason: str
    usage: TokenUsage | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TaskResult:
    success: bool
    exit_reason: str
    output_paths: tuple[Path, ...] = field(default_factory=tuple)
    usage: TokenUsage | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        normalized = tuple(Path(path).expanduser().resolve() for path in self.output_paths)
        object.__setattr__(self, "output_paths", normalized)


class BaseRunner(ABC):
    """Shared marker contract for all runner implementations."""

    name: str = "runner"


class AgentRunner(BaseRunner):
    """Base contract for session-oriented coding agent backends."""

    name: str = "agent"

    @abstractmethod
    async def start_session(
        self,
        workspace: Path,
        *,
        worker_host: str | None = None,
    ) -> AgentSession:
        """Start a persistent agent session for one issue workspace."""

    @abstractmethod
    async def run_turn(
        self,
        session: AgentSession,
        prompt: str,
        issue: Issue,
        on_event: AgentEventCallback,
    ) -> TurnResult:
        """Run one agent turn and forward normalized runner events."""

    @abstractmethod
    async def stop_session(self, session: AgentSession) -> None:
        """Stop a session and release runner-owned process or API resources."""


class CLIAgentRunner(AgentRunner):
    """Base class for subprocess-backed session runners."""

    command: tuple[str, ...]

    def __init__(self, command: str | tuple[str, ...] | list[str]) -> None:
        if isinstance(command, str):
            command_parts = tuple(shlex.split(command))
        else:
            command_parts = tuple(command)
        if not command_parts:
            raise AgentRunnerError("agent_command_required")
        self.command = command_parts


class APIAgentRunner(BaseRunner):
    """Base contract for API-backed one-shot task runners."""

    name: str = "api_agent"

    @abstractmethod
    async def run_task(
        self,
        workspace: Path,
        prompt: str,
        issue: Issue,
        on_event: AgentEventCallback,
    ) -> TaskResult:
        """Run a single API task and return saved output artifacts if any."""
