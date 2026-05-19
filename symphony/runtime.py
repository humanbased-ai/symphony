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
    complete_worker_terminal_failure,
    dispatch_issue,
    is_active_state,
    is_terminal_state,
    normalize_state,
    reconcile_refreshed_issues,
    release_issue,
    select_dispatchable,
    should_dispatch,
    stalled_issue_ids,
    unresolved_blockers,
)


# Exit reasons that mean "the agent is blocked waiting for human approval".
# Includes Codex's protocol-level ``turn_input_required`` frames produced by
# CodexRunner._needs_input — without it, those exits fall through to the
# normal retry path even when the operator has configured an approval gate.
APPROVAL_REQUIRED_EXIT_REASONS = {
    "approval_required",
    "approval_unreachable",
    "turn_input_required",
}
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
        # Track candidates that were filtered out by the blocker gate (PRD §8.2)
        # last tick. When an issue transitions from blocked → unblocked across
        # ticks, we re-add it to ``new_issue_ids`` so dispatch reconsiders it
        # even though it's not technically a new arrival — otherwise non-Todo
        # blocked candidates that became unblocked while their candidate row
        # stayed stable would be stranded forever.
        self._prev_blocked_ids: set[str] = set()

    async def run_tick(self) -> RuntimeTickResult:
        """Poll Linear once, dispatch eligible issues, and wait for started workers."""

        now_ms = self.clock_ms()
        await self.reconcile_running(now_ms=now_ms)
        candidates = list(await _call_sync(self.tracker.fetch_candidate_issues))

        # Release stale claims from prior terminal failures BEFORE computing
        # `new_issue_ids` so the unclaimed issue can be picked up as new.
        self._release_zombie_claims(candidates)

        current_ids = {issue.id for issue in candidates}
        new_issue_ids = current_ids - self._prev_candidate_ids

        # PR #36 recheck: issues whose blockers resolved between ticks are
        # NOT in new_issue_ids (they were marked seen last tick), but they
        # ARE eligible now. Detect blocked → unblocked transitions and
        # re-add them so the dispatch path reconsiders them this tick.
        currently_blocked_ids = {
            issue.id
            for issue in candidates
            if is_active_state(issue, self.state)
            and unresolved_blockers(issue, self.state)
        }
        newly_unblocked_ids = (self._prev_blocked_ids - currently_blocked_ids) & current_ids
        new_issue_ids |= newly_unblocked_ids
        self._prev_blocked_ids = currently_blocked_ids
        self._prev_candidate_ids = current_ids

        released = await self._release_due_retries_missing_from_candidates(candidates)
        dispatched_issues = self._dispatch_due_retries(candidates, now_ms=now_ms)

        self._emit_blocker_skip_events(candidates, new_issue_ids)

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
            LOGGER.warning(
                "Issue %s has stalled (no events for %ds).", identifier, stall_timeout_ms // 1000
            )
            workspace_path = getattr(entry, "workspace_path", None) if entry else None
            stall_workspace = _StalledWorkspaceHandle.from_entry(entry) if workspace_path else None
            await self._terminate_run(
                issue=entry.issue if entry else Issue(
                    id=issue_id,
                    identifier=identifier,
                    title="stalled",
                    description=None,
                    priority=None,
                    state="",
                    branch_name=None,
                    url=None,
                ),
                workspace=stall_workspace,
                reason="stall_timeout",
                last_message="stall_timeout",
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
                    # The claim flow owns the workspace-failure cleanup path:
                    # rollback to queued_state if safe, otherwise log
                    # claim_abandoned. The outer retry handler MUST NOT
                    # additionally route through `_terminate_run` (would move
                    # to failure_state, overriding the rollback) or
                    # `complete_worker_failure` (with allow_claimed_retry=True
                    # the next tick would re-dispatch and duplicate an agent
                    # if a concurrent instance is already running the issue).
                    rolled_back = await self._rollback_claim_after_workspace_failure(
                        claimed_issue, workspace_exc
                    )
                    release_issue(issue.id, self.state)
                    if rolled_back:
                        # The issue is back in queued_state but still in
                        # `_prev_candidate_ids` from this tick. Without the
                        # discard, next tick's candidate diff treats it as
                        # not-new and the dispatch path skips it until the
                        # operator nudges state externally (PR #38 recheck).
                        self._prev_candidate_ids.discard(issue.id)
                    self._notify_state_change()
                    return _WorkerResult(
                        success=False, error=f"workspace_failed:{workspace_exc}"
                    )
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
            # Approval-required exits go through their own handler. When
            # approval_state is set, the handler moves the issue there and
            # returns a result — `_terminate_run` is NOT called, so
            # failure_state does not override the approval resolution
            # (PR #38 review fix #1). When both approval_state and
            # failure_state are unset, the handler parks the issue. When
            # approval_state is unset but failure_state is set, the handler
            # returns None and we fall through to `_terminate_run` with the
            # canonical `approval_unreachable` reason so PRD §8.3's "move
            # to failure state" semantics fire.
            if error in APPROVAL_REQUIRED_EXIT_REASONS:
                handled = await self._handle_approval_required(issue, error)
                if handled is not None:
                    return handled
                error = "approval_unreachable"
            await self._terminate_run(
                issue=issue,
                workspace=workspace,
                reason=error,
                last_message=getattr(result, "exit_reason", None) or error,
            )
            return _WorkerResult(success=False, error=error)
        except Exception as exc:  # noqa: BLE001 - runtime must convert worker failures into retry state.
            error = str(exc) or exc.__class__.__name__
            if workspace is not None:
                await _best_effort_after_run(self.workspace_manager, workspace)
            await self._terminate_run(issue=issue, workspace=workspace, reason=error, last_message=error)
            return _WorkerResult(success=False, error=error)
        finally:
            if session is not None and hasattr(self.runner, "stop_session"):
                await _maybe_await(self.runner.stop_session(session))
            self._notify_state_change()

    async def _terminate_run(
        self,
        *,
        issue: Issue,
        workspace: Any,
        reason: str,
        last_message: str | None,
    ) -> None:
        """Resolve a non-recoverable run failure per PRD §8.4.

        When ``tracker.failure_state`` is configured:
          - move the issue to ``failure_state`` via the tracker;
          - log a structured ``run_failed`` event with the reason;
          - clean up the per-run workspace (unless ``workspace.keep_on_failure``
            is set);
          - release the issue from running/claimed/retry state — no auto-retry.

        When ``tracker.failure_state`` is unset, fall back to the legacy
        retry-scheduling path (``complete_worker_failure``) so deployments that
        have not yet adopted the failure-state model continue to work
        unchanged. Approval-unreachable runs are always terminal — they stay
        in ``claimed`` to prevent re-dispatch in the same daemon.
        """

        if issue.id not in self.state.running:
            return

        failure_state = self.config.tracker.failure_state
        keep = self.config.workspace.keep_on_failure
        if failure_state:
            LOGGER.warning(
                "run_failed: issue=%s reason=%s failure_state=%s last_message=%s",
                issue.identifier,
                reason,
                failure_state,
                last_message,
            )
            move = getattr(self.tracker, "move_issue_to_state", None)
            move_failed = False
            if move is not None and issue.team_id:
                try:
                    await _call_sync(move, issue.id, issue.team_id, failure_state)
                except Exception as exc:  # noqa: BLE001 - retry on transient errors.
                    LOGGER.warning(
                        "failure_state_move_failed: issue=%s error=%s — scheduling retry",
                        issue.identifier,
                        exc,
                    )
                    move_failed = True
            elif move is None:
                LOGGER.warning(
                    "failure_state_move_skipped: issue=%s reason=tracker_missing_move_method",
                    issue.identifier,
                )
            else:
                LOGGER.warning(
                    "failure_state_move_skipped: issue=%s reason=issue_missing_team_id",
                    issue.identifier,
                )

            if move_failed:
                # Transient: don't clean up the workspace, don't release. A
                # normal retry is scheduled so the next tick re-attempts the
                # failure_state move. Otherwise the ticket sits in its
                # original active state with `_prev_candidate_ids` already
                # containing it — stranded until external state churn.
                complete_worker_failure(
                    issue.id,
                    self.state,
                    now_ms=self.clock_ms(),
                    max_retry_backoff_ms=self.config.agent.max_retry_backoff_ms,
                    error=f"failure_state_move_failed:{reason}",
                )
                return

            if workspace is not None:
                await self._cleanup_run_workspace(workspace, keep_on_failure=keep)
            release_issue(issue.id, self.state)
            return

        if reason == "approval_unreachable":
            # Approval-unreachable is always non-recoverable; keep in claimed
            # even without an explicit failure_state.
            complete_worker_terminal_failure(issue.id, self.state)
            return

        complete_worker_failure(
            issue.id,
            self.state,
            now_ms=self.clock_ms(),
            max_retry_backoff_ms=self.config.agent.max_retry_backoff_ms,
            error=reason,
        )

    async def _cleanup_run_workspace(self, workspace: Any, *, keep_on_failure: bool) -> None:
        cleanup_for_run = getattr(self.workspace_manager, "cleanup_for_run", None)
        try:
            if cleanup_for_run is not None:
                await _maybe_await(
                    cleanup_for_run(workspace, failed=True, keep_on_failure=keep_on_failure)
                )
                return
            # Legacy workspace managers (test fakes, etc.) — fall back to the
            # identifier-based cleanup if they expose it.
            cleanup = getattr(self.workspace_manager, "cleanup", None)
            if cleanup is not None and not keep_on_failure:
                identifier = getattr(workspace, "workspace_key", None) or getattr(workspace, "path", None)
                if identifier is not None:
                    await _maybe_await(cleanup(str(identifier)))
        except Exception as exc:  # noqa: BLE001 - cleanup must not propagate.
            LOGGER.warning(
                "workspace_cleanup_failed: workspace=%s error=%s",
                getattr(workspace, "path", workspace),
                exc,
            )

    async def _handle_approval_required(
        self, issue: Issue, exit_reason: str
    ) -> "_WorkerResult | None":
        """Resolve an approval-required turn failure per PRD §8.3.

        Three configurations:

        * ``tracker.approval_state`` set — move the issue to that state via
          Linear and release the dispatch without scheduling a retry. An
          out-of-band resolver (operator action, Phase 3 mobile push, etc.)
          will move the issue out of ``approval_state`` when ready, at which
          point Symphony picks it up again. If the move raises (Linear
          outage, unresolvable state name, etc.) we schedule a normal retry
          instead of releasing, so the next tick re-attempts the move rather
          than stranding the ticket. Returns a ``_WorkerResult`` so the
          caller short-circuits and ``_terminate_run`` does NOT fire —
          approval has priority over failure_state when both are set.
        * ``tracker.approval_state`` unset, ``tracker.failure_state`` set —
          per PRD §8.3 the unresolvable approval is treated as a
          non-recoverable failure that moves to ``failure_state``. We
          return ``None`` so the caller falls through to ``_terminate_run``
          with reason ``approval_unreachable``.
        * Both unset — fail-closed park: log ``approval_unreachable`` and
          park the issue via ``complete_worker_terminal_failure`` (no retry,
          kept in ``claimed``). A subsequent re-appearance in candidates
          after operator intervention releases the zombie claim via
          ``_release_zombie_claims`` on the next tick (intervention is
          detected by the issue being absent from the prior tick's
          candidate snapshot).
        """

        approval_state = self.config.tracker.approval_state
        if approval_state:
            LOGGER.warning(
                "approval_pending: issue=%s exit_reason=%s approval_state=%s",
                issue.identifier,
                exit_reason,
                approval_state,
            )
            move = getattr(self.tracker, "move_issue_to_state", None)
            move_attempt_failed = False
            terminal_misconfig = False
            if move is not None and issue.team_id:
                try:
                    await _call_sync(move, issue.id, issue.team_id, approval_state)
                except Exception as exc:  # noqa: BLE001 - retry on transient errors.
                    LOGGER.warning(
                        "approval_state_move_failed: issue=%s error=%s — scheduling retry",
                        issue.identifier,
                        exc,
                    )
                    move_attempt_failed = True
            elif move is None:
                LOGGER.warning(
                    "approval_state_move_skipped: issue=%s reason=tracker_missing_move_method",
                    issue.identifier,
                )
                terminal_misconfig = True
            else:
                LOGGER.warning(
                    "approval_state_move_skipped: issue=%s reason=issue_missing_team_id",
                    issue.identifier,
                )
                terminal_misconfig = True

            if move_attempt_failed:
                # Transient: schedule a normal retry so the next tick retries
                # the move. The ticket is still in the active candidate set,
                # `complete_worker_failure` keeps it in `claimed` and adds a
                # `RetryEntry` that `_dispatch_due_retries` picks up.
                complete_worker_failure(
                    issue.id,
                    self.state,
                    now_ms=self.clock_ms(),
                    max_retry_backoff_ms=self.config.agent.max_retry_backoff_ms,
                    error="approval_state_move_failed",
                )
                return _WorkerResult(success=False, error="approval_state_move_failed")
            if terminal_misconfig:
                # The runtime cannot move this issue without operator action
                # (no tracker mutation API, or no team_id on the issue). Park
                # rather than retrying — looping won't help.
                complete_worker_terminal_failure(issue.id, self.state)
                return _WorkerResult(
                    success=False, error="approval_state_move_skipped"
                )

            release_issue(issue.id, self.state)
            return _WorkerResult(success=False, error="approval_pending")

        if self.config.tracker.failure_state:
            # PRD §8.3 explicit: unresolvable approval moves to failure_state.
            # Caller routes through `_terminate_run`, which performs the move,
            # workspace cleanup, and release. Returning None signals the
            # fall-through.
            return None

        LOGGER.warning(
            "approval_unreachable: issue=%s exit_reason=%s — tracker.approval_state "
            "is not set; parking issue without retry (PRD §8.3)",
            issue.identifier,
            exit_reason,
        )
        complete_worker_terminal_failure(issue.id, self.state)
        return _WorkerResult(success=False, error="approval_unreachable")

    def _release_zombie_claims(self, candidates: list[Issue]) -> None:
        """Release stale claims after operator intervention.

        After a terminal failure (e.g., IN-288 ``approval_unreachable``),
        Symphony leaves the issue in ``state.claimed`` so the same daemon
        does not re-dispatch it on the next tick — that would loop the
        same failure on every poll. We only release the claim when the
        operator has clearly intervened: the issue must currently be in
        the candidate set AND have been ABSENT on the prior tick
        (i.e., the operator moved it out of the active states and then
        back in). That out-and-back motion is the only externally
        observable signal of intent to re-dispatch.

        Without this gate, a parked-approval issue that stays in the
        active state would be released every tick and immediately
        re-dispatched, defeating the no-retry contract of
        ``complete_worker_terminal_failure``.
        """

        zombie_ids = {
            issue.id
            for issue in candidates
            if (
                issue.id in self.state.claimed
                and issue.id not in self.state.running
                and issue.id not in self.state.retry_attempts
                # Operator intervention signal: issue was OUT of the
                # candidate set last tick. Without this guard the release
                # fires every tick a parked issue stays active.
                and issue.id not in self._prev_candidate_ids
            )
        }
        for zombie_id in zombie_ids:
            LOGGER.info(
                "claim_released: issue=%s reason=operator_reintroduced_after_terminal_failure",
                zombie_id,
            )
            self.state.claimed.discard(zombie_id)

    def _emit_blocker_skip_events(self, candidates: list[Issue], new_issue_ids: set[str]) -> None:
        """Log ``blocker_skip`` for newly-visible candidates held by upstream work (PRD §8.2).

        Only emitted for newly-visible candidates (issues not seen on the prior
        tick) to keep log volume bounded — a long-blocked ticket would
        otherwise log every poll interval. Reconsidered on each new arrival.
        """

        for issue in candidates:
            if issue.id not in new_issue_ids:
                continue
            if not is_active_state(issue, self.state):
                continue
            if issue.id in self.state.running or issue.id in self.state.claimed:
                continue
            blockers = unresolved_blockers(issue, self.state)
            if not blockers:
                continue
            blocker_ids = ",".join(
                blocker.identifier or blocker.id or "?" for blocker in blockers
            )
            LOGGER.info(
                "blocker_skip: issue=%s blockers=%s",
                issue.identifier,
                blocker_ids,
            )

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
    ) -> bool:
        """Apply PRD §8.5 step 4 rollback semantics on workspace-setup failure.

        ``tracker.queued_state`` is the explicit rollback target. When unset
        (the default, safe for multi-instance deployments), Symphony logs
        ``claim_abandoned`` and leaves the issue in the in-progress state for
        operator inspection — rolling back unconditionally could re-queue a
        ticket that a concurrent instance is actively running.

        Returns ``True`` when the issue was successfully moved back to
        ``queued_state`` (the caller should re-mark it dispatchable so the
        next tick picks it up); ``False`` for the abandoned/no-rollback
        paths (caller leaves the issue alone for operator inspection).
        """

        queued_state = self.config.tracker.queued_state
        if not queued_state:
            LOGGER.warning(
                "claim_abandoned: issue=%s instance=%s reason=%s",
                claimed_issue.identifier,
                _instance_id(),
                workspace_exc,
            )
            return False
        move = getattr(self.tracker, "move_issue_to_state", None)
        if move is None:
            LOGGER.warning(
                "claim_abandoned: issue=%s instance=%s reason=tracker_missing_move_method",
                claimed_issue.identifier,
                _instance_id(),
            )
            return False
        try:
            await _call_sync(move, claimed_issue.id, claimed_issue.team_id, queued_state)
        except Exception as exc:  # noqa: BLE001 - rollback is best-effort by design.
            LOGGER.warning(
                "claim_rollback_failed: issue=%s instance=%s error=%s",
                claimed_issue.identifier,
                _instance_id(),
                exc,
            )
            return False
        LOGGER.warning(
            "claim_rollback: issue=%s instance=%s state=%s reason=%s",
            claimed_issue.identifier,
            _instance_id(),
            queued_state,
            workspace_exc,
        )
        return True

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


@dataclass(frozen=True)
class _StalledWorkspaceHandle:
    """Minimal Workspace-shaped surface used to reconstruct cleanup context for stalled runs."""

    path: Path
    workspace_key: str
    run_id: str
    branch_name: str | None

    @classmethod
    def from_entry(cls, entry: Any) -> "_StalledWorkspaceHandle":
        return cls(
            path=Path(getattr(entry, "workspace_path", "")),
            workspace_key=getattr(entry, "identifier", "") or "",
            run_id=getattr(entry, "run_id", "") or "",
            branch_name=getattr(entry, "branch_name", None),
        )


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
