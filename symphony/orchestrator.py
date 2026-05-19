from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Mapping

from symphony.config import WorkflowConfig
from symphony.tracker.models import Issue


CONTINUATION_RETRY_DELAY_MS = 1_000
FAILURE_RETRY_BASE_DELAY_MS = 10_000


class OrchestratorError(ValueError):
    """Raised when an invalid orchestration state transition is requested."""


@dataclass
class RunningEntry:
    issue: Issue
    started_at_ms: int
    retry_attempt: int | None = None
    last_event_at_ms: int | None = None
    session_id: str | None = None
    last_event: str | None = None
    last_message: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0

    @property
    def identifier(self) -> str:
        return self.issue.identifier


@dataclass(frozen=True)
class RetryEntry:
    issue_id: str
    identifier: str
    attempt: int
    due_at_ms: int
    error: str | None = None


@dataclass(frozen=True)
class ReconciliationAction:
    issue_id: str
    identifier: str
    cleanup_workspace: bool
    reason: str


@dataclass
class OrchestratorState:
    poll_interval_ms: int
    max_concurrent_agents: int
    active_states: tuple[str, ...]
    terminal_states: tuple[str, ...]
    max_concurrent_agents_by_state: Mapping[str, int] = field(default_factory=dict)
    in_progress_state: str | None = None
    queued_state: str | None = None
    running: dict[str, RunningEntry] = field(default_factory=dict)
    claimed: set[str] = field(default_factory=set)
    retry_attempts: dict[str, RetryEntry] = field(default_factory=dict)
    completed: set[str] = field(default_factory=set)

    @classmethod
    def from_config(cls, config: WorkflowConfig) -> "OrchestratorState":
        return cls(
            poll_interval_ms=config.polling.interval_ms,
            max_concurrent_agents=config.agent.max_concurrent_agents,
            active_states=config.tracker.active_states,
            terminal_states=config.tracker.terminal_states,
            max_concurrent_agents_by_state=config.agent.max_concurrent_agents_by_state,
            in_progress_state=config.tracker.in_progress_state,
            queued_state=config.tracker.queued_state,
        )

    def apply_config(self, config: WorkflowConfig) -> None:
        self.poll_interval_ms = config.polling.interval_ms
        self.max_concurrent_agents = config.agent.max_concurrent_agents
        self.active_states = config.tracker.active_states
        self.terminal_states = config.tracker.terminal_states
        self.max_concurrent_agents_by_state = config.agent.max_concurrent_agents_by_state
        self.in_progress_state = config.tracker.in_progress_state
        self.queued_state = config.tracker.queued_state

    def available_slots(self) -> int:
        return max(self.max_concurrent_agents - len(self.running), 0)

    def available_slots_for_state(self, state_name: str) -> int:
        normalized = normalize_state(state_name)
        limit = self.max_concurrent_agents_by_state.get(normalized, self.max_concurrent_agents)
        running_count = sum(1 for entry in self.running.values() if normalize_state(entry.issue.state) == normalized)
        return max(limit - running_count, 0)


def normalize_state(state_name: str | None) -> str:
    return (state_name or "").strip().lower()


def is_active_state(issue: Issue, state: OrchestratorState) -> bool:
    return normalize_state(issue.state) in {normalize_state(item) for item in state.active_states}


def is_terminal_state(issue: Issue, state: OrchestratorState) -> bool:
    return normalize_state(issue.state) in {normalize_state(item) for item in state.terminal_states}


def sort_for_dispatch(issues: list[Issue]) -> list[Issue]:
    return sorted(issues, key=_dispatch_sort_key)


def should_dispatch(issue: Issue, state: OrchestratorState, *, allow_claimed_retry: bool = False) -> bool:
    if not _base_issue_eligible(issue, state, allow_claimed_retry=allow_claimed_retry):
        return False
    if state.available_slots() <= 0:
        return False
    if state.available_slots_for_state(issue.state) <= 0:
        return False
    return True


def _base_issue_eligible(
    issue: Issue,
    state: OrchestratorState,
    *,
    allow_claimed_retry: bool = False,
) -> bool:
    if not issue.id or not issue.identifier or not issue.title or not issue.state:
        return False
    if not is_active_state(issue, state):
        return False
    if is_terminal_state(issue, state):
        return False
    if issue.id in state.running:
        return False
    if issue.id in state.claimed and not (allow_claimed_retry and issue.id in state.retry_attempts):
        return False
    if normalize_state(issue.state) == "todo" and _has_non_terminal_blocker(issue, state):
        return False
    # When the claim guard is enabled, issues already in the in-progress state
    # are skipped — they are either claimed by another instance or being worked
    # on by a human (PRD §8.5). The exception is our own continuation retries:
    # `complete_worker_success` leaves the issue in `state.retry_attempts` and
    # `state.claimed` after a successful run, but the agent has typically also
    # moved the Linear issue into `in_progress_state` during the run, so without
    # this carve-out continuation retries would be permanently filtered.
    if (
        state.in_progress_state
        and normalize_state(issue.state) == normalize_state(state.in_progress_state)
        and not (allow_claimed_retry and issue.id in state.retry_attempts)
    ):
        return False
    return True


def select_dispatchable(issues: list[Issue], state: OrchestratorState) -> list[Issue]:
    selected: list[Issue] = []
    planned_by_state: dict[str, int] = {}
    remaining_global = state.available_slots()

    for issue in sort_for_dispatch(issues):
        if remaining_global <= 0:
            break
        if not _base_issue_eligible(issue, state):
            continue

        normalized = normalize_state(issue.state)
        remaining_for_state = state.available_slots_for_state(issue.state) - planned_by_state.get(normalized, 0)
        if remaining_for_state <= 0:
            continue

        selected.append(issue)
        planned_by_state[normalized] = planned_by_state.get(normalized, 0) + 1
        remaining_global -= 1

    return selected


def dispatch_issue(
    issue: Issue,
    state: OrchestratorState,
    *,
    now_ms: int,
    attempt: int | None = None,
) -> RunningEntry:
    if not should_dispatch(issue, state, allow_claimed_retry=attempt is not None):
        raise OrchestratorError("issue_not_dispatch_eligible")

    entry = RunningEntry(issue=issue, started_at_ms=now_ms, retry_attempt=attempt)
    state.running[issue.id] = entry
    state.claimed.add(issue.id)
    state.retry_attempts.pop(issue.id, None)
    return entry


def schedule_retry(
    issue_id: str,
    identifier: str,
    state: OrchestratorState,
    *,
    attempt: int,
    now_ms: int,
    max_retry_backoff_ms: int,
    error: str | None,
    continuation: bool = False,
) -> RetryEntry:
    delay_ms = CONTINUATION_RETRY_DELAY_MS if continuation else retry_delay_ms(attempt, max_retry_backoff_ms)
    entry = RetryEntry(
        issue_id=issue_id,
        identifier=identifier,
        attempt=attempt,
        due_at_ms=now_ms + delay_ms,
        error=error,
    )
    state.retry_attempts[issue_id] = entry
    state.claimed.add(issue_id)
    return entry


def complete_worker_success(issue_id: str, state: OrchestratorState, *, now_ms: int) -> RetryEntry:
    entry = _pop_running(issue_id, state)
    return schedule_retry(
        issue_id,
        entry.identifier,
        state,
        attempt=1,
        now_ms=now_ms,
        max_retry_backoff_ms=CONTINUATION_RETRY_DELAY_MS,
        error=None,
        continuation=True,
    )


def complete_worker_failure(
    issue_id: str,
    state: OrchestratorState,
    *,
    now_ms: int,
    max_retry_backoff_ms: int,
    error: str,
) -> RetryEntry:
    entry = _pop_running(issue_id, state)
    next_attempt = (entry.retry_attempt or 0) + 1
    return schedule_retry(
        issue_id,
        entry.identifier,
        state,
        attempt=next_attempt,
        now_ms=now_ms,
        max_retry_backoff_ms=max_retry_backoff_ms,
        error=error,
    )


def release_issue(issue_id: str, state: OrchestratorState) -> None:
    state.running.pop(issue_id, None)
    state.retry_attempts.pop(issue_id, None)
    state.claimed.discard(issue_id)


def stalled_issue_ids(state: OrchestratorState, *, now_ms: int, stall_timeout_ms: int) -> list[str]:
    stalled: list[str] = []
    for issue_id, entry in state.running.items():
        since_ms = entry.last_event_at_ms if entry.last_event_at_ms is not None else entry.started_at_ms
        if now_ms - since_ms > stall_timeout_ms:
            stalled.append(issue_id)
    return stalled


def reconcile_refreshed_issues(
    refreshed_issues: list[Issue],
    state: OrchestratorState,
) -> list[ReconciliationAction]:
    actions: list[ReconciliationAction] = []
    for issue in refreshed_issues:
        if issue.id not in state.running:
            continue
        if is_terminal_state(issue, state):
            release_issue(issue.id, state)
            actions.append(
                ReconciliationAction(
                    issue_id=issue.id,
                    identifier=issue.identifier,
                    cleanup_workspace=True,
                    reason="terminal_state",
                )
            )
            continue
        if is_active_state(issue, state):
            state.running[issue.id].issue = issue
            continue

        release_issue(issue.id, state)
        actions.append(
            ReconciliationAction(
                issue_id=issue.id,
                identifier=issue.identifier,
                cleanup_workspace=False,
                reason="non_active_state",
            )
        )
    return actions


def retry_delay_ms(attempt: int, max_retry_backoff_ms: int) -> int:
    if attempt <= 0:
        raise OrchestratorError("retry_attempt_must_be_positive")
    return min(FAILURE_RETRY_BASE_DELAY_MS * (2 ** (attempt - 1)), max_retry_backoff_ms)


def _dispatch_sort_key(issue: Issue) -> tuple[int, bool, datetime, str]:
    priority = issue.priority if isinstance(issue.priority, int) and issue.priority > 0 else 999
    created_at = issue.created_at or datetime.max.replace(tzinfo=timezone.utc)
    return (priority, issue.created_at is None, created_at, issue.identifier)


def _has_non_terminal_blocker(issue: Issue, state: OrchestratorState) -> bool:
    terminal_states = {normalize_state(item) for item in state.terminal_states}
    return any(normalize_state(blocker.state) not in terminal_states for blocker in issue.blocked_by)


def _pop_running(issue_id: str, state: OrchestratorState) -> RunningEntry:
    try:
        return state.running.pop(issue_id)
    except KeyError as exc:
        raise OrchestratorError("issue_not_running") from exc
