from __future__ import annotations

import asyncio
import inspect
import logging
import os
import socket
import time
import dataclasses
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from symphony.agents.base import AgentEvent, AgentEventCallback, TokenUsage
from symphony.config import WorkflowConfig
from symphony.orchestrator import (
    OrchestratorState,
    complete_worker_failure,
    complete_worker_success,
    dispatch_issue,
    is_terminal_state,
    normalize_state,
    reconcile_refreshed_issues,
    release_issue,
    select_dispatchable,
    should_dispatch,
    stalled_issue_ids,
)
from symphony.tracker.models import Issue
from symphony.workflow import WorkflowDefinition, render_prompt


LOGGER = logging.getLogger(__name__)
StateCallback = Callable[[OrchestratorState], Any]


@dataclass(frozen=True)
class RuntimeTickResult:
    fetched: int
    dispatched: tuple[str, ...] = ()
    completed: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    released: tuple[str, ...] = ()
    errors: dict[str, str] = field(default_factory=dict)


class SymphonyRuntime:
    """Offline-testable runtime coordinator for one Symphony poll tick."""

    def __init__(
        self,
        *,
        config: WorkflowConfig,
        workflow: WorkflowDefinition | None = None,
        prompt_template: str | None = None,
        tracker: Any,
        workspace_manager: Any,
        runner: Any,
        state: OrchestratorState | None = None,
        clock_ms: Callable[[], int] | None = None,
        on_event: AgentEventCallback | None = None,
        on_state_change: StateCallback | None = None,
    ) -> None:
        self.config = config
        self.workflow = workflow
        self.prompt_template = prompt_template if prompt_template is not None else (
            workflow.prompt_template if workflow is not None else ""
        )
        self.tracker = tracker
        self.workspace_manager = workspace_manager
        self.runner = runner
        self.state = state or OrchestratorState.from_config(config)
        self.clock_ms = clock_ms or _monotonic_epoch_ms
        self.on_event = on_event
        self.on_state_change = on_state_change
        # Tracks the issue IDs seen in the previous poll tick.  An issue is
        # eligible for dispatch only when it newly appears (present in current
        # candidates but absent from _prev_candidate_ids).  Starts empty so
        # that a bare run_tick() call (e.g. in tests or --once mode) treats every
        # issue as new.  The poll loop calls record_startup_issues() before the
        # first tick to seed this with the currently-active set, which makes
        # pre-existing issues invisible until they leave and re-enter active states.
        self._prev_candidate_ids: set[str] = set()

    async def run_tick(self) -> RuntimeTickResult:
        """Poll Linear once, dispatch eligible issues, and wait for started workers."""

        now_ms = self.clock_ms()
        await self.reconcile_running(now_ms=now_ms)
        candidates = list(await _call_sync(self.tracker.fetch_candidate_issues))

        current_ids = {issue.id for issue in candidates}
        new_issue_ids = current_ids - self._prev_candidate_ids
        self._prev_candidate_ids = current_ids

        released = await self._release_due_retries_missing_from_candidates(candidates)
        dispatched_issues = self._dispatch_due_retries(candidates, now_ms=now_ms)

        eligible = [issue for issue in candidates if issue.id in new_issue_ids]
        remaining = [issue for issue in eligible if issue.id not in {item.id for item in dispatched_issues}]
        for issue in select_dispatchable(remaining, self.state):
            dispatch_issue(issue, self.state, now_ms=now_ms)
            dispatched_issues.append(issue)

        completed: list[str] = []
        failed: list[str] = []
        errors: dict[str, str] = {}
        worker_results = await asyncio.gather(
            *(self._run_dispatched_issue(issue) for issue in dispatched_issues),
        )
        for issue, result in zip(dispatched_issues, worker_results, strict=True):
            if result.success:
                completed.append(issue.identifier)
            else:
                failed.append(issue.identifier)
                errors[issue.identifier] = result.error or "worker_failed"

        self._notify_state_change()
        return RuntimeTickResult(
            fetched=len(candidates),
            dispatched=tuple(issue.identifier for issue in dispatched_issues),
            completed=tuple(completed),
            failed=tuple(failed),
            released=tuple(released),
            errors=errors,
        )

    async def run_issue(self, issue: Issue, *, attempt: int | None = None) -> "_WorkerResult":
        """Dispatch and run a single issue immediately."""

        dispatch_issue(issue, self.state, now_ms=self.clock_ms(), attempt=attempt)
        result = await self._run_dispatched_issue(issue)
        self._notify_state_change()
        return result

    async def reconcile_running(self, *, now_ms: int | None = None) -> None:
        if not self.state.running:
            return

        now_ms = now_ms or self.clock_ms()
        stall_timeout_ms = self.config.codex.stall_timeout_ms
        for issue_id in list(stalled_issue_ids(self.state, now_ms=now_ms, stall_timeout_ms=stall_timeout_ms)):
            entry = self.state.running.get(issue_id)
            identifier = entry.identifier if entry else issue_id
            LOGGER.warning("Issue %s has stalled (no events for %ds), forcing retry.", identifier, stall_timeout_ms // 1000)
            complete_worker_failure(
                issue_id,
                self.state,
                now_ms=now_ms,
                max_retry_backoff_ms=self.config.agent.max_retry_backoff_ms,
                error="stall_timeout",
            )

        issue_ids = list(self.state.running)
        if not issue_ids:
            self._notify_state_change()
            return

        refreshed = list(await _call_sync(self.tracker.fetch_issue_states_by_ids, issue_ids))
        actions = reconcile_refreshed_issues(refreshed, self.state)
        for action in actions:
            if action.cleanup_workspace:
                await _maybe_await(self.workspace_manager.cleanup(action.identifier))
        self._notify_state_change()

    async def record_startup_issues(self) -> None:
        """Seed _prev_candidate_ids with currently-active issues.

        Call this once before the poll loop starts.  Issues that are already
        active at startup will be skipped on the first tick; they become
        eligible again only if they leave the active states and re-enter them
        while the daemon is running.
        """
        candidates = list(await _call_sync(self.tracker.fetch_candidate_issues))
        self._prev_candidate_ids = {issue.id for issue in candidates}
        LOGGER.info("Startup snapshot: %d pre-existing issue(s) will be skipped.", len(self._prev_candidate_ids))

    def snapshot(self) -> OrchestratorState:
        return self.state

    async def _release_due_retries_missing_from_candidates(self, candidates: list[Issue]) -> list[str]:
        candidate_ids = {issue.id for issue in candidates}
        missing_retries = [
            retry
            for retry in self.state.retry_attempts.values()
            if retry.issue_id not in candidate_ids
        ]
        await self._cleanup_terminal_success_retries(missing_retries)

        released: list[str] = []
        for retry in list(missing_retries):
            release_issue(retry.issue_id, self.state)
            released.append(retry.identifier)
        return released

    async def _cleanup_terminal_success_retries(self, retries: list[Any]) -> None:
        success_retry_ids = {retry.issue_id for retry in retries if retry.error is None}
        if not success_retry_ids:
            return

        try:
            refreshed = list(await _maybe_await(self.tracker.fetch_issue_states_by_ids(list(success_retry_ids))))
        except Exception as exc:  # noqa: BLE001 - cleanup is best-effort and must not crash polling.
            LOGGER.warning("Unable to refresh missing successful retries before cleanup: %s", exc)
            return

        for issue in refreshed:
            if issue.id in success_retry_ids and is_terminal_state(issue, self.state):
                await _maybe_await(self.workspace_manager.cleanup(issue.identifier))

    def _dispatch_due_retries(self, candidates: list[Issue], *, now_ms: int) -> list[Issue]:
        by_id = {issue.id: issue for issue in candidates}
        dispatched: list[Issue] = []
        due_retries = sorted(
            (
                retry
                for retry in self.state.retry_attempts.values()
                if retry.due_at_ms <= now_ms and retry.issue_id in by_id
            ),
            key=lambda retry: (retry.due_at_ms, retry.identifier),
        )

        for retry in due_retries:
            issue = by_id[retry.issue_id]
            if not should_dispatch(issue, self.state, allow_claimed_retry=True):
                continue
            dispatch_issue(issue, self.state, now_ms=now_ms, attempt=retry.attempt)
            dispatched.append(issue)

        return dispatched

    async def _run_dispatched_issue(self, issue: Issue) -> "_WorkerResult":
        entry = self.state.running[issue.id]
        workspace = None
        session = None
        claimed_issue: Issue | None = None
        claim_target = self.config.tracker.in_progress_state

        try:
            if claim_target:
                try:
                    claimed_issue = await self._claim_issue(issue, claim_target)
                except _ClaimRejectedError as exc:
                    LOGGER.warning(
                        "claim_failed: issue=%s instance=%s reason=%s",
                        issue.identifier,
                        _instance_id(),
                        exc,
                    )
                    release_issue(issue.id, self.state)
                    self._notify_state_change()
                    return _WorkerResult(success=False, error=f"claim_failed:{exc}")
                except Exception as exc:  # noqa: BLE001 - any unexpected error aborts the claim.
                    # Treat as transient (Linear 5xx, network blip, etc.) and
                    # schedule a retry. Without this, the issue is recorded in
                    # `_prev_candidate_ids` as "already seen" but stays out of
                    # `retry_attempts`, so the next tick treats it as
                    # pre-existing and never re-dispatches until it leaves and
                    # re-enters the candidate set.
                    LOGGER.warning(
                        "claim_error: issue=%s instance=%s error=%s — scheduling retry",
                        issue.identifier,
                        _instance_id(),
                        exc,
                    )
                    complete_worker_failure(
                        issue.id,
                        self.state,
                        now_ms=self.clock_ms(),
                        max_retry_backoff_ms=self.config.agent.max_retry_backoff_ms,
                        error=f"claim_error:{exc}",
                    )
                    self._notify_state_change()
                    return _WorkerResult(success=False, error=f"claim_error:{exc}")
                LOGGER.info(
                    "claim_succeeded: issue=%s instance=%s state=%s",
                    claimed_issue.identifier,
                    _instance_id(),
                    claimed_issue.state,
                )
                issue = claimed_issue
                entry.issue = issue

            try:
                workspace = await _maybe_await(self.workspace_manager.prepare_for_issue(issue))
            except Exception as workspace_exc:
                if claimed_issue is not None:
                    await self._rollback_claim_after_workspace_failure(claimed_issue, workspace_exc)
                raise
            _attach_runtime_entry_metadata(
                entry,
                workspace_path=getattr(workspace, "path", None),
                run_log_path=getattr(workspace, "run_log_path", None),
                run_id=getattr(workspace, "run_id", None),
                branch_name=getattr(workspace, "branch_name", None),
            )
            await _maybe_await(self.workspace_manager.before_run(workspace))

            enriched_issue = await self._enrich_with_comments(issue)
            prompt = render_prompt(self.prompt_template, issue=enriched_issue, attempt=entry.retry_attempt)
            if _is_api_runner(self.runner):
                result = await self.runner.run_task(Path(workspace.path), prompt, issue, self._agent_event_handler)
            else:
                session = await self.runner.start_session(Path(workspace.path))
                entry.session_id = session.id
                result = await self.runner.run_turn(session, prompt, issue, self._agent_event_handler)

            _apply_usage(entry, getattr(result, "usage", None))
            await _maybe_await(self.workspace_manager.after_run(workspace))

            if result.success:
                complete_worker_success(issue.id, self.state, now_ms=self.clock_ms())
                return _WorkerResult(success=True)

            error = str(result.exit_reason or "worker_failed")
            complete_worker_failure(
                issue.id,
                self.state,
                now_ms=self.clock_ms(),
                max_retry_backoff_ms=self.config.agent.max_retry_backoff_ms,
                error=error,
            )
            return _WorkerResult(success=False, error=error)
        except Exception as exc:  # noqa: BLE001 - runtime must convert worker failures into retry state.
            error = str(exc) or exc.__class__.__name__
            if workspace is not None:
                await _best_effort_after_run(self.workspace_manager, workspace)
            if issue.id in self.state.running:
                complete_worker_failure(
                    issue.id,
                    self.state,
                    now_ms=self.clock_ms(),
                    max_retry_backoff_ms=self.config.agent.max_retry_backoff_ms,
                    error=error,
                )
            return _WorkerResult(success=False, error=error)
        finally:
            if session is not None and hasattr(self.runner, "stop_session"):
                await _maybe_await(self.runner.stop_session(session))
            self._notify_state_change()

    async def _claim_issue(self, issue: Issue, claim_target: str) -> Issue:
        """Move ``issue`` to ``claim_target`` and re-fetch to verify ownership (PRD §8.5)."""

        move = getattr(self.tracker, "move_issue_to_state", None)
        if move is None:
            raise _ClaimRejectedError("tracker_missing_move_method")

        await _call_sync(move, issue.id, issue.team_id, claim_target)
        refreshed_list = list(
            await _call_sync(self.tracker.fetch_issue_states_by_ids, [issue.id])
        )
        refreshed = next((item for item in refreshed_list if item.id == issue.id), None)
        if refreshed is None:
            raise _ClaimRejectedError("post_claim_refetch_empty")
        if normalize_state(refreshed.state) != normalize_state(claim_target):
            raise _ClaimRejectedError(
                f"post_claim_state_mismatch:{refreshed.state}!={claim_target}"
            )
        return refreshed

    async def _rollback_claim_after_workspace_failure(
        self, claimed_issue: Issue, workspace_exc: BaseException
    ) -> None:
        """Apply PRD §8.5 step 4 rollback semantics on workspace-setup failure.

        ``tracker.queued_state`` is the explicit rollback target. When unset
        (the default, safe for multi-instance deployments), Symphony logs
        ``claim_abandoned`` and leaves the issue in the in-progress state for
        operator inspection — rolling back unconditionally could re-queue a
        ticket that a concurrent instance is actively running.
        """

        queued_state = self.config.tracker.queued_state
        if not queued_state:
            LOGGER.warning(
                "claim_abandoned: issue=%s instance=%s reason=%s",
                claimed_issue.identifier,
                _instance_id(),
                workspace_exc,
            )
            return
        move = getattr(self.tracker, "move_issue_to_state", None)
        if move is None:
            LOGGER.warning(
                "claim_abandoned: issue=%s instance=%s reason=tracker_missing_move_method",
                claimed_issue.identifier,
                _instance_id(),
            )
            return
        try:
            await _call_sync(move, claimed_issue.id, claimed_issue.team_id, queued_state)
        except Exception as exc:  # noqa: BLE001 - rollback is best-effort by design.
            LOGGER.warning(
                "claim_rollback_failed: issue=%s instance=%s error=%s",
                claimed_issue.identifier,
                _instance_id(),
                exc,
            )
            return
        LOGGER.warning(
            "claim_rollback: issue=%s instance=%s state=%s reason=%s",
            claimed_issue.identifier,
            _instance_id(),
            queued_state,
            workspace_exc,
        )

    async def _enrich_with_comments(self, issue: Issue) -> Issue:
        if not hasattr(self.tracker, "fetch_issue_comments"):
            return issue
        try:
            comments = await _maybe_await(self.tracker.fetch_issue_comments(issue.id))
            return dataclasses.replace(issue, comments=tuple(comments))
        except Exception:
            return issue

    async def _agent_event_handler(self, event: AgentEvent) -> None:
        issue_id = event.issue_id
        if issue_id is not None and issue_id in self.state.running:
            entry = self.state.running[issue_id]
            entry.last_event_at_ms = self.clock_ms()
            entry.last_event = event.type.value
            entry.last_message = event.message
            if event.session_id is not None:
                entry.session_id = event.session_id
            if event.type.value in {"turn_completed", "turn_failed", "task_completed", "task_failed"}:
                entry.turn_count = getattr(entry, "turn_count", 0) + 1
            _append_recent_event(entry, event)

        _append_recent_event(self.state, event)
        if self.on_event is not None:
            await self.on_event(event)
        self._notify_state_change()

    def _notify_state_change(self) -> None:
        if self.on_state_change is not None:
            self.on_state_change(self.state)


@dataclass(frozen=True)
class _WorkerResult:
    success: bool
    error: str | None = None


class _ClaimRejectedError(RuntimeError):
    """Raised when the post-claim re-fetch shows the ticket is not owned by us."""


def _instance_id() -> str:
    """Return a host:pid string used to tag claim events for multi-instance triage."""

    try:
        host = socket.gethostname()
    except OSError:
        host = "unknown"
    return f"{host}:{os.getpid()}"


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _best_effort_after_run(workspace_manager: Any, workspace: Any) -> None:
    try:
        await _maybe_await(workspace_manager.after_run(workspace))
    except Exception:
        return


def _is_api_runner(runner: Any) -> bool:
    return hasattr(runner, "run_task") and not hasattr(runner, "start_session")


def _attach_runtime_entry_metadata(
    entry: Any,
    *,
    workspace_path: Any,
    run_log_path: Any = None,
    run_id: Any = None,
    branch_name: Any = None,
) -> None:
    if workspace_path is not None:
        entry.workspace_path = Path(workspace_path)
    if run_log_path is not None:
        entry.run_log_path = Path(run_log_path)
    if run_id is not None:
        entry.run_id = str(run_id)
    if branch_name is not None:
        entry.branch_name = str(branch_name)
    if not hasattr(entry, "turn_count"):
        entry.turn_count = 0
    if not hasattr(entry, "recent_events"):
        entry.recent_events = []


def _append_recent_event(target: Any, event: AgentEvent) -> None:
    if not hasattr(target, "recent_events"):
        target.recent_events = []
    target.recent_events.append(
        {
            "event": event.type.value,
            "message": event.message,
            "issue_identifier": event.issue_identifier,
            "session_id": event.session_id,
        }
    )
    if len(target.recent_events) > 50:
        del target.recent_events[:-50]


def _apply_usage(entry: Any, usage: TokenUsage | None) -> None:
    if usage is None:
        return
    entry.input_tokens += usage.input_tokens
    entry.output_tokens += usage.output_tokens
    entry.total_tokens += usage.total_tokens


def _monotonic_epoch_ms() -> int:
    return int(time.time() * 1000)


async def _call_sync(fn: Any, *args: Any) -> Any:
    """Run a sync callable in a thread pool so it doesn't block the event loop."""
    if asyncio.iscoroutinefunction(fn):
        return await fn(*args)
    return await asyncio.to_thread(fn, *args)
