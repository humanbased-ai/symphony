from __future__ import annotations

import asyncio
import inspect
import logging
import os
import time
import dataclasses
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from symphony.agents.base import AgentEvent, AgentEventCallback, TokenUsage
from symphony.config import WorkflowConfig
from symphony.github.webhooks import GitHubEvent, PRClosedEvent, PRCommentEvent, PRReviewEvent
from symphony.orchestrator import (
    OrchestratorState,
    complete_worker_failure,
    complete_worker_success,
    dispatch_issue,
    is_terminal_state,
    park_issue_for_pr,
    reconcile_refreshed_issues,
    release_issue,
    select_dispatchable,
    should_dispatch,
    stalled_issue_ids,
)
from symphony.feedback import ClassifyError, FeedbackSignal, classify_feedback
from symphony.tracker.models import Issue
from symphony.workflow import WorkflowDefinition, render_prompt


LOGGER = logging.getLogger(__name__)
StateCallback = Callable[[OrchestratorState], Any]
_MAX_CLASSIFY_COMMENTS = 20
_PR_DIFF_TRUNCATE = 8_000
# HTML comment prefix injected into every PR comment Symphony posts.
# Filtering is done on this marker so that a user whose personal GitHub token
# is also the Symphony bot token can still have their own comments picked up.
SYMPHONY_BOT_MARKER = "<!-- symphony -->"


def _fmt_duration_ms(ms: int) -> str:
    s = ms // 1000
    if s < 60:
        return f"{s}s"
    return f"{s // 60}m {s % 60}s"


@dataclass(frozen=True)
class RuntimeTickResult:
    fetched: int
    dispatched: tuple[str, ...] = ()
    completed: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    released: tuple[str, ...] = ()
    errors: dict[str, str] = field(default_factory=dict)
    active: int = 0


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
        on_pr_update: Callable[[str, int, str], None] | None = None,
        github_client: Any | None = None,
        manifest_writer: Any | None = None,
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
        self.on_pr_update = on_pr_update
        self.github_client = github_client
        self.manifest_writer = manifest_writer
        # Tracks the issue IDs seen in the previous poll tick.  An issue is
        # eligible for dispatch only when it newly appears (present in current
        # candidates but absent from _prev_candidate_ids).  Starts empty so
        # that a bare run_tick() call (e.g. in tests or --once mode) treats every
        # issue as new.  The poll loop calls record_startup_issues() before the
        # first tick to seed this with the currently-active set, which makes
        # pre-existing issues invisible until they leave and re-enter active states.
        self._prev_candidate_ids: set[str] = set()
        # Maps issue_id → frozenset of comment IDs already processed for feedback.
        self._feedback_seen: dict[str, frozenset[str]] = {}
        # Maps workspace branch_name → Issue, populated after workspace creation.
        # Used to route GitHub PR events back to the originating Linear issue.
        self._branch_to_issue: dict[str, Issue] = {}
        # Tracks how many PR feedback agent runs have been triggered per branch.
        self._pr_turns: dict[str, int] = {}
        # Maps branch → frozenset of comment/review IDs already processed in polling.
        self._pr_comment_seen: dict[str, frozenset[int]] = {}
        # Cached PR numbers discovered by find_open_pr_for_branch.
        self._branch_pr_numbers: dict[str, int] = {}
        # Maps branch → frozenset of check-run IDs already processed for CI failures.
        self._pr_ci_seen: dict[str, frozenset[int]] = {}
        # Branches where a conflict-resolution agent run has been dispatched and not yet resolved.
        self._pr_conflict_dispatched: set[str] = set()
        # All RunningEntry objects ever dispatched, keyed by issue.id.
        self._dispatched_entries: dict[str, Any] = {}

    async def run_tick(self) -> RuntimeTickResult:
        """Poll Linear once, dispatch eligible issues, and wait for started workers."""

        now_ms = self.clock_ms()
        await self.reconcile_running(now_ms=now_ms)
        candidates = list(await _call_sync(self.tracker.fetch_candidate_issues))

        current_ids = {issue.id for issue in candidates}
        new_issue_ids = current_ids - self._prev_candidate_ids

        self._refresh_running_issue_states(candidates)
        released = await self._release_due_retries_missing_from_candidates(candidates)
        dispatched_issues = self._dispatch_due_retries(candidates, now_ms=now_ms)

        eligible = [issue for issue in candidates if issue.id in new_issue_ids]
        remaining = [issue for issue in eligible if issue.id not in {item.id for item in dispatched_issues}]
        for issue in select_dispatchable(remaining, self.state, now_ms=now_ms):
            dispatch_issue(issue, self.state, now_ms=now_ms)
            dispatched_issues.append(issue)

        # Mark new issues as seen only after dispatch so that issues that could
        # not be dispatched this tick (concurrency limit full) remain eligible
        # on the next tick rather than being permanently dropped.
        dispatched_new_ids = {i.id for i in dispatched_issues if i.id in new_issue_ids}
        self._prev_candidate_ids = current_ids - (new_issue_ids - dispatched_new_ids)

        for issue in dispatched_issues:
            LOGGER.info("Dispatching  %s — %s", issue.identifier, issue.title)
            if issue.id in self.state.running:
                self._dispatched_entries[issue.id] = self.state.running[issue.id]

        completed: list[str] = []
        failed: list[str] = []
        errors: dict[str, str] = {}
        worker_results = await asyncio.gather(
            *(self._run_dispatched_issue(issue) for issue in dispatched_issues),
        )
        for issue, result in zip(dispatched_issues, worker_results, strict=True):
            if result.success:
                completed.append(issue.identifier)
                LOGGER.info("Completed    %s", issue.identifier)
            else:
                error = result.error or "worker_failed"
                failed.append(issue.identifier)
                errors[issue.identifier] = error
                LOGGER.warning("Failed       %s — %s", issue.identifier, error)

        await self.poll_feedback()
        await self.poll_github_pr_feedback()
        self._notify_state_change()
        return RuntimeTickResult(
            fetched=len(candidates),
            dispatched=tuple(issue.identifier for issue in dispatched_issues),
            completed=tuple(completed),
            failed=tuple(failed),
            released=tuple(released),
            errors=errors,
            active=len(self.state.running),
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

    def _resolve_pr_url(self, branch_name: str | None) -> str:
        if branch_name is None or self.github_client is None:
            return ""
        pr_number = self._branch_pr_numbers.get(branch_name)
        if pr_number is None:
            return ""
        gh = self.github_client
        return f"https://github.com/{gh.owner}/{gh.repo}/pull/{pr_number}"

    def _record_manifest(
        self,
        issue: Any,
        session: Any,
        *,
        start_time: float,
        end_time: float,
        status: str,
        usage: Any,
        pr_url: str,
    ) -> None:
        if self.manifest_writer is None:
            return
        entry = self.state.running.get(issue.id)
        self.manifest_writer.record(
            ticket_id=issue.identifier,
            session_id=getattr(session, "id", None) or (entry.session_id if entry else None),
            started_at=start_time,
            ended_at=end_time,
            status=status,
            input_tokens=getattr(usage, "input_tokens", 0) if usage else 0,
            output_tokens=getattr(usage, "output_tokens", 0) if usage else 0,
            pr_url=pr_url,
        )

    async def _run_dispatched_issue(self, issue: Issue) -> "_WorkerResult":
        entry = self.state.running[issue.id]
        workspace = None
        session = None
        start_ms = self.clock_ms()

        try:
            if self.github_client is not None:
                existing_pr = await asyncio.to_thread(
                    self.github_client.find_open_pr_for_issue, issue.identifier
                )
                if existing_pr is not None:
                    pr_data = await asyncio.to_thread(self.github_client.get_pr, existing_pr)
                    head_branch = ((pr_data or {}).get("head") or {}).get("ref")
                    LOGGER.info(
                        "Issue %s already has open PR #%d on branch %r — skipping new dispatch.",
                        issue.identifier, existing_pr, head_branch,
                    )
                    if head_branch:
                        self._branch_pr_numbers[head_branch] = existing_pr
                        self._branch_to_issue[head_branch] = issue
                    park_issue_for_pr(issue.id, self.state)
                    return _WorkerResult(success=True)

            workspace = await _maybe_await(self.workspace_manager.prepare_for_issue(issue))
            _attach_runtime_entry_metadata(
                entry,
                workspace_path=getattr(workspace, "path", None),
                run_log_path=getattr(workspace, "run_log_path", None),
                run_id=getattr(workspace, "run_id", None),
                branch_name=getattr(workspace, "branch_name", None),
            )
            branch_name = getattr(workspace, "branch_name", None)
            if branch_name:
                self._branch_to_issue[branch_name] = issue
                LOGGER.info(
                    "Workspace    %s  branch: %s  path: %s",
                    issue.identifier,
                    branch_name,
                    getattr(workspace, "path", "?"),
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

            elapsed = _fmt_duration_ms(self.clock_ms() - start_ms)
            end_time = time.time()
            usage = getattr(result, "usage", None)
            pr_url = self._resolve_pr_url(branch_name)
            if result.success:
                self._record_manifest(issue, session, start_time=start_ms / 1000, end_time=end_time, status="completed", usage=usage, pr_url=pr_url)
                complete_worker_success(issue.id, self.state, now_ms=self.clock_ms())
                LOGGER.info("Completed    %s — %s  (%s)", issue.identifier, issue.title, elapsed)
                return _WorkerResult(success=True)

            error = str(result.exit_reason or "worker_failed")
            self._record_manifest(issue, session, start_time=start_ms / 1000, end_time=end_time, status="failed", usage=usage, pr_url=pr_url)
            complete_worker_failure(
                issue.id,
                self.state,
                now_ms=self.clock_ms(),
                max_retry_backoff_ms=self.config.agent.max_retry_backoff_ms,
                error=error,
            )
            LOGGER.warning("Failed       %s — %s  (%s)  reason: %s", issue.identifier, issue.title, elapsed, error)
            return _WorkerResult(success=False, error=error)
        except Exception as exc:  # noqa: BLE001 - runtime must convert worker failures into retry state.
            error = str(exc) or exc.__class__.__name__
            elapsed = _fmt_duration_ms(self.clock_ms() - start_ms)
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
            LOGGER.warning("Failed       %s — %s  (%s)  reason: %s", issue.identifier, issue.title, elapsed, error)
            return _WorkerResult(success=False, error=error)
        finally:
            if session is not None and hasattr(self.runner, "stop_session"):
                await _maybe_await(self.runner.stop_session(session))
            self._notify_state_change()

    async def _enrich_with_comments(self, issue: Issue) -> Issue:
        if not hasattr(self.tracker, "fetch_issue_comments"):
            return issue
        try:
            comments = await _maybe_await(self.tracker.fetch_issue_comments(issue.id))
            enriched = dataclasses.replace(issue, comments=tuple(comments))
            self._detect_pr_from_comments(enriched)
            return enriched
        except Exception:
            return issue

    def _refresh_running_issue_states(self, candidates: list[Issue]) -> None:
        """Update issue state for all dispatched entries every tick.

        Running issues are updated from the already-fetched candidates list at
        no extra cost. Completed/failed issues that are no longer candidates are
        batch-fetched separately so their state stays current in the dashboard.
        """
        by_id = {issue.id: issue for issue in candidates}

        # Update running entries from candidates (free — already fetched).
        for entry in self.state.running.values():
            fresh = by_id.get(entry.issue.id)
            if fresh is not None and fresh.state != entry.issue.state:
                entry.issue = dataclasses.replace(entry.issue, state=fresh.state)

        # Batch-refresh completed/failed entries that are no longer candidates.
        stale_ids = [
            issue_id for issue_id in self._dispatched_entries
            if issue_id not in by_id and issue_id not in self.state.running
        ]
        if not stale_ids or not hasattr(self.tracker, "fetch_issue_states_by_ids"):
            return
        try:
            fresh_issues: list[Issue] = self.tracker.fetch_issue_states_by_ids(stale_ids)
            fresh_by_id = {i.id: i for i in fresh_issues}
            for issue_id, entry in self._dispatched_entries.items():
                fresh = fresh_by_id.get(issue_id)
                if fresh is not None and fresh.state != entry.issue.state:
                    entry.issue = dataclasses.replace(entry.issue, state=fresh.state)
        except Exception:
            pass

    def _detect_pr_from_comments(self, issue: Issue) -> None:
        import re
        if self.on_pr_update is None or not issue.branch_name:
            return
        branch = issue.branch_name
        if branch in self._branch_pr_numbers:
            return
        for text in issue.comments:
            m = re.search(r"github\.com/[^/]+/[^/]+/pull/(\d+)", text)
            if m:
                pr_number = int(m.group(1))
                self._branch_pr_numbers[branch] = pr_number
                self.on_pr_update(branch, pr_number, "open")
                return

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

    async def poll_feedback(self) -> None:
        """Check issues in the review state for human feedback signals and act on them."""
        if not hasattr(self.tracker, "fetch_issues_by_states"):
            return

        review_state = self.config.tracker.review_state
        try:
            review_issues = list(await _call_sync(self.tracker.fetch_issues_by_states, [review_state]))
        except Exception:  # noqa: BLE001 - feedback polling is best-effort; must not crash the tick.
            return

        for issue in review_issues:
            try:
                await self._handle_feedback_for_issue(issue)
            except Exception:  # noqa: BLE001
                pass

    async def poll_github_pr_feedback(self) -> None:
        """Poll GitHub API for new PR comments on branches we're currently tracking."""
        if self.github_client is None or not self._branch_to_issue:
            return

        for branch in list(self._branch_to_issue):
            issue = self._branch_to_issue.get(branch)
            if issue is None:
                continue
            try:
                await self._poll_pr_for_branch(branch, issue)
            except Exception:  # noqa: BLE001
                LOGGER.debug("PR polling error for branch %r", branch, exc_info=True)

    async def _poll_pr_for_branch(self, branch: str, issue: Issue) -> None:
        gh = self.github_client
        cached_pr = self._branch_pr_numbers.get(branch)

        if cached_pr is not None:
            open_pr = await asyncio.to_thread(gh.find_open_pr_for_branch, branch)
            if open_pr is None:
                # PR was closed or merged — synthesise a closed event
                pr_data = await asyncio.to_thread(gh.get_pr, cached_pr)
                merged = bool((pr_data or {}).get("merged", False))
                LOGGER.info(
                    "Polling detected PR #%d %s for %s",
                    cached_pr, "merged" if merged else "closed", issue.identifier,
                )
                event = PRClosedEvent(
                    pr_number=cached_pr, pr_head_branch=branch, merged=merged,
                    repo_owner=gh.owner, repo_name=gh.repo,
                )
                if self.on_pr_update is not None:
                    self.on_pr_update(branch, cached_pr, "merged" if merged else "closed")
                await self._handle_pr_closed(event)
                self._branch_pr_numbers.pop(branch, None)
                self._pr_comment_seen.pop(branch, None)
                self._pr_conflict_dispatched.discard(branch)
                return
            pr_number = cached_pr
        else:
            pr_number_found = await asyncio.to_thread(gh.find_open_pr_for_branch, branch)
            if pr_number_found is None:
                return
            pr_number = pr_number_found
            self._branch_pr_numbers[branch] = pr_number
            if self.on_pr_update is not None:
                self.on_pr_update(branch, pr_number, "open")

        pr_data = await asyncio.to_thread(gh.get_pr, pr_number)
        if pr_data:
            mergeable_state = pr_data.get("mergeable_state", "")
            if pr_data.get("mergeable") is False and mergeable_state == "dirty":
                await self._handle_pr_conflict(branch, pr_number, issue)
            elif mergeable_state and mergeable_state != "dirty":
                self._pr_conflict_dispatched.discard(branch)

        review_comments, issue_comments, reviews = await asyncio.gather(
            asyncio.to_thread(gh.list_pr_review_comments, pr_number),
            asyncio.to_thread(gh.list_pr_issue_comments, pr_number),
            asyncio.to_thread(gh.list_pr_reviews, pr_number),
        )

        seen = self._pr_comment_seen.get(branch, frozenset())
        feedback_parts: list[str] = []
        new_seen_ids: set[int] = set()

        for comment in review_comments:
            cid = int(comment.get("id") or 0)
            if not cid or cid in seen:
                continue
            new_seen_ids.add(cid)
            body = str(comment.get("body") or "").strip()
            if body.startswith(SYMPHONY_BOT_MARKER):
                continue
            author = str((comment.get("user") or {}).get("login") or "unknown")
            if body:
                feedback_parts.append(f"**{author}** left a review comment:\n\n{body}")

        for comment in issue_comments:
            cid = int(comment.get("id") or 0)
            if not cid or cid in seen:
                continue
            new_seen_ids.add(cid)
            body = str(comment.get("body") or "").strip()
            if body.startswith(SYMPHONY_BOT_MARKER):
                continue
            author = str((comment.get("user") or {}).get("login") or "unknown")
            if body:
                feedback_parts.append(f"**{author}** commented:\n\n{body}")

        for review in reviews:
            rid = int(review.get("id") or 0)
            if not rid or rid in seen:
                continue
            new_seen_ids.add(rid)
            state = str(review.get("state") or "").lower()
            if state != "changes_requested":
                continue
            body = str(review.get("body") or "").strip()
            if body.startswith(SYMPHONY_BOT_MARKER):
                continue
            reviewer = str((review.get("user") or {}).get("login") or "unknown")
            text = f"**{reviewer}** requested changes"
            if body:
                text += f":\n\n{body}"
            feedback_parts.append(text)

        self._pr_comment_seen[branch] = seen | frozenset(new_seen_ids)

        if feedback_parts:
            combined = "\n\n---\n\n".join(feedback_parts)
            await self._handle_pr_feedback(branch, pr_number, combined)
            return

        # Check for new CI failures only when there is no human feedback this tick,
        # so the agent addresses human comments before retrying CI.
        await self._handle_ci_failures(branch, pr_number, issue)

    async def _handle_ci_failures(self, branch: str, pr_number: int, issue: Issue) -> None:
        """Trigger a PR-feedback agent run when new CI check failures appear."""
        gh = self.github_client
        if gh is None:
            return
        failed = await asyncio.to_thread(gh.get_pr_failed_check_runs, pr_number)
        if not failed:
            if branch in self._pr_ci_seen and self.on_pr_update is not None:
                self.on_pr_update(branch, pr_number, "open")
            return
        ci_seen = self._pr_ci_seen.get(branch, frozenset())
        new_failures = [r for r in failed if r["id"] not in ci_seen]
        if not new_failures:
            return
        self._pr_ci_seen[branch] = ci_seen | frozenset(r["id"] for r in new_failures)
        lines = []
        for r in new_failures:
            line = f"- **{r['name']}** failed"
            if r["details_url"]:
                line += f" — {r['details_url']}"
            if r["summary"]:
                line += f"\n  {r['summary']}"
            lines.append(line)
        feedback_text = "The following CI checks failed:\n\n" + "\n".join(lines)
        LOGGER.info(
            "CI failure(s) on PR #%d for %s: %s",
            pr_number, issue.identifier, ", ".join(r["name"] for r in new_failures),
        )
        if self.on_pr_update is not None:
            self.on_pr_update(branch, pr_number, "ci_fail")
        await self._handle_pr_feedback(branch, pr_number, feedback_text)

    async def handle_github_pr_event(self, event: GitHubEvent) -> None:
        """Route a GitHub PR event to the appropriate handler."""
        if isinstance(event, PRClosedEvent):
            await self._handle_pr_closed(event)
        elif isinstance(event, PRCommentEvent):
            await self._handle_pr_feedback(
                event.pr_head_branch, event.pr_number,
                f"**{event.comment_author}** left a review comment:\n\n{event.comment_body}",
            )
        elif isinstance(event, PRReviewEvent) and event.review_state == "changes_requested":
            body = event.review_body or "(no description)"
            await self._handle_pr_feedback(
                event.pr_head_branch, event.pr_number,
                f"**{event.reviewer}** requested changes:\n\n{body}",
            )
        elif isinstance(event, PRReviewEvent) and event.review_state == "approved":
            LOGGER.info("PR #%d approved — waiting for merge to close the issue.", event.pr_number)
            if self.on_pr_update is not None:
                self.on_pr_update(event.pr_head_branch, event.pr_number, "approved")

    async def _handle_pr_closed(self, event: PRClosedEvent) -> None:
        issue = self._branch_to_issue.get(event.pr_head_branch)
        if issue is None:
            LOGGER.debug("PR #%d closed on unknown branch %r — ignoring.", event.pr_number, event.pr_head_branch)
            return

        target_state = self.config.tracker.done_state if event.merged else self.config.tracker.cancelled_state
        LOGGER.info(
            "PR #%d %s for %s → transitioning to %s",
            event.pr_number,
            "merged" if event.merged else "closed",
            issue.identifier,
            target_state,
        )
        if hasattr(self.tracker, "update_issue_state_by_name"):
            await _call_sync(self.tracker.update_issue_state_by_name, issue.id, target_state)

        self._branch_to_issue.pop(event.pr_head_branch, None)
        self._pr_turns.pop(event.pr_head_branch, None)
        if issue is not None:
            release_issue(issue.id, self.state)

    async def _handle_pr_conflict(self, branch: str, pr_number: int, issue: Issue) -> None:
        if branch in self._pr_conflict_dispatched:
            return
        self._pr_conflict_dispatched.add(branch)
        LOGGER.info(
            "PR #%d has merge conflicts for %s — dispatching conflict resolution agent.",
            pr_number, issue.identifier,
        )
        if self.on_pr_update is not None:
            self.on_pr_update(branch, pr_number, "conflict")
        feedback_text = _render_pr_conflict_prompt()
        await self._handle_pr_feedback(branch, pr_number, feedback_text)

    async def _handle_pr_feedback(self, branch: str, pr_number: int, feedback_text: str) -> None:
        issue = self._branch_to_issue.get(branch)
        if issue is None:
            LOGGER.debug("PR #%d comment on unknown branch %r — ignoring.", pr_number, branch)
            return

        max_turns = self.config.github.max_pr_turns
        turns = self._pr_turns.get(branch, 0)
        if turns >= max_turns:
            LOGGER.warning(
                "PR #%d (%s) has reached max_pr_turns=%d — skipping agent run.",
                pr_number, issue.identifier, max_turns,
            )
            if self.github_client is not None:
                self.github_client.post_pr_comment(
                    pr_number,
                    f"{SYMPHONY_BOT_MARKER}\nMaximum feedback iterations ({max_turns}) reached for this PR. "
                    "Please review and merge or close manually.",
                )
            return

        self._pr_turns[branch] = turns + 1
        LOGGER.info(
            "PR #%d feedback for %s (turn %d/%d) — dispatching agent.",
            pr_number, issue.identifier, turns + 1, max_turns,
        )

        diff = ""
        if self.github_client is not None:
            diff = await asyncio.to_thread(self.github_client.get_pr_diff, pr_number)

        prompt = _render_pr_feedback_prompt(issue, pr_number, branch, feedback_text, diff)
        await self._run_pr_feedback(issue, branch, pr_number, prompt)

    async def _run_pr_feedback(self, issue: Issue, branch: str, pr_number: int, prompt: str) -> None:
        workspace = None
        session = None
        try:
            if hasattr(self.workspace_manager, "prepare_for_pr_feedback"):
                workspace = await _maybe_await(
                    self.workspace_manager.prepare_for_pr_feedback(issue, branch)
                )
            else:
                workspace = await _maybe_await(self.workspace_manager.prepare_for_issue(issue))

            await _maybe_await(self.workspace_manager.before_run(workspace))

            if _is_api_runner(self.runner):
                result = await self.runner.run_task(
                    Path(workspace.path), prompt, issue, self._agent_event_handler
                )
            else:
                session = await self.runner.start_session(Path(workspace.path))
                result = await self.runner.run_turn(session, prompt, issue, self._agent_event_handler)

            await _maybe_await(self.workspace_manager.after_run(workspace))

            if not result.success:
                LOGGER.warning("PR feedback agent run failed for %s: %s", issue.identifier, result.exit_reason)
                if self.github_client is not None:
                    self.github_client.post_pr_comment(
                        pr_number,
                        f"{SYMPHONY_BOT_MARKER}\nI encountered an error while addressing your feedback: `{result.exit_reason}`",
                    )
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("PR feedback run raised for %s: %s", issue.identifier, exc)
            if workspace is not None:
                await _best_effort_after_run(self.workspace_manager, workspace)
        finally:
            if session is not None and hasattr(self.runner, "stop_session"):
                await _maybe_await(self.runner.stop_session(session))
            if workspace is not None:
                await _maybe_await(
                    self.workspace_manager.cleanup(issue.identifier)
                    if not hasattr(self.workspace_manager, "cleanup_for_run")
                    else self.workspace_manager.cleanup_for_run(workspace)
                )

    async def _handle_feedback_for_issue(self, issue: Issue) -> None:
        if not hasattr(self.tracker, "fetch_issue_comments_with_ids"):
            return

        pairs: list[tuple[str, str]] = list(
            await _call_sync(self.tracker.fetch_issue_comments_with_ids, issue.id)
        )
        # Scan all comments for GitHub PR URLs in case _enrich_with_comments missed them.
        all_texts = [text for _, text in pairs]
        self._detect_pr_from_comments(dataclasses.replace(issue, comments=tuple(all_texts)))

        current_ids = frozenset(cid for cid, _ in pairs)

        # On first encounter after (re)start, mark all existing comments as seen
        # without classifying them so pre-existing close/approve signals are not
        # re-triggered by a restart.
        if issue.id not in self._feedback_seen:
            self._feedback_seen[issue.id] = current_ids
            return

        seen = self._feedback_seen[issue.id]
        new_ids = current_ids - seen
        if not new_ids:
            return

        new_comments = [text for cid, text in pairs if cid in new_ids]
        new_comments = new_comments[-_MAX_CLASSIFY_COMMENTS:]

        try:
            signal = await asyncio.to_thread(classify_feedback, new_comments)
        except ClassifyError as exc:
            LOGGER.warning("Feedback classification failed for %s, will retry: %s", issue.identifier, exc)
            return  # Don't update _feedback_seen; will retry next poll

        if signal is None:
            self._feedback_seen[issue.id] = current_ids
            return

        tracker_cfg = self.config.tracker

        # For CHANGE_REQUEST: if the issue already has an open PR, push fixes to
        # the existing branch rather than re-dispatching and opening a second PR.
        if signal == FeedbackSignal.CHANGE_REQUEST:
            branch = issue.branch_name
            pr_number: int | None = self._branch_pr_numbers.get(branch) if branch else None
            if pr_number is None and branch and self.github_client is not None:
                pr_number = await asyncio.to_thread(
                    self.github_client.find_open_pr_for_issue, issue.identifier
                )
            if branch and pr_number:
                LOGGER.info(
                    "Feedback signal change_request on %s → pushing fixes to PR #%d on branch %s",
                    issue.identifier, pr_number, branch,
                )
                self._branch_to_issue[branch] = issue
                self._feedback_seen[issue.id] = current_ids
                await self._handle_pr_feedback(branch, pr_number, "\n".join(new_comments))
                return

        if signal == FeedbackSignal.APPROVE:
            target_state = tracker_cfg.done_state
        elif signal == FeedbackSignal.CHANGE_REQUEST:
            active = tracker_cfg.active_states
            target_state = active[0] if active else "Todo"
        else:  # CLOSE
            target_state = tracker_cfg.cancelled_state

        LOGGER.info(
            "Feedback signal %s on %s → transitioning to %s",
            signal.value,
            issue.identifier,
            target_state,
        )

        if signal == FeedbackSignal.CLOSE and self.github_client is not None:
            branch = issue.branch_name
            pr_number: int | None = self._branch_pr_numbers.get(branch) if branch else None
            if pr_number is None:
                branches = [b for b, iss in self._branch_to_issue.items() if iss.id == issue.id]
                for b in branches:
                    pr_number = self._branch_pr_numbers.get(b)
                    if pr_number:
                        break
            if pr_number is None:
                pr_number = await asyncio.to_thread(
                    self.github_client.find_open_pr_for_issue, issue.identifier
                )
            if pr_number:
                closed = await asyncio.to_thread(self.github_client.close_pr, pr_number)
                if closed:
                    LOGGER.info("Closed PR #%d for %s", pr_number, issue.identifier)
                else:
                    LOGGER.warning("Failed to close PR #%d for %s", pr_number, issue.identifier)

        if hasattr(self.tracker, "update_issue_state_by_name"):
            success = await _call_sync(self.tracker.update_issue_state_by_name, issue.id, target_state)
            if success:
                self._feedback_seen[issue.id] = current_ids
            else:
                LOGGER.warning(
                    "Failed to transition %s to %s, will retry next poll",
                    issue.identifier,
                    target_state,
                )
        else:
            self._feedback_seen[issue.id] = current_ids

    def _notify_state_change(self) -> None:
        if self.on_state_change is not None:
            self.on_state_change(self.state)


@dataclass(frozen=True)
class _WorkerResult:
    success: bool
    error: str | None = None


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


def _render_pr_feedback_prompt(
    issue: Issue,
    pr_number: int,
    branch: str,
    feedback_text: str,
    diff: str,
) -> str:
    diff_section = ""
    if diff:
        truncated = diff[:_PR_DIFF_TRUNCATE]
        if len(diff) > _PR_DIFF_TRUNCATE:
            truncated += "\n... [diff truncated]"
        diff_section = f"\n\nCurrent PR diff:\n```diff\n{truncated}\n```"

    return f"""\
You are addressing reviewer feedback on pull request #{pr_number} for issue {issue.identifier}: {issue.title}

Original issue:
{issue.description or "(no description)"}
{diff_section}

Reviewer feedback:
{feedback_text}

Instructions:
1. Make the necessary code changes to address the feedback.
2. Commit your changes with a clear message.
3. Push to the PR branch: git push origin HEAD:{branch}
4. Post a comment on the PR summarizing what you changed. The body MUST start with
   `<!-- symphony -->` on its own first line so the automation skips your comment on
   the next polling cycle (without this marker your comment will trigger another run):
     gh pr comment {pr_number} --body $'<!-- symphony -->\\nI addressed the feedback by ...'

Focus only on what the reviewer asked for. Do not refactor unrelated code.
"""


def _render_pr_conflict_prompt() -> str:
    return """\
This PR has merge conflicts with the base branch that must be resolved before it can be merged.

Instructions:
1. Fetch the latest base branch and merge it into the current branch:
     git fetch origin main
     git merge origin/main
2. Identify and resolve all conflict markers (<<<<<<<, =======, >>>>>>>).
3. Stage the resolved files and commit:
     git add <resolved files>
     git commit -m "fix: resolve merge conflicts with main"
4. Push the branch:
     git push origin HEAD
5. Post a comment on the PR confirming the conflicts are resolved. The body MUST start with
   `<!-- symphony -->` on its own first line:
     gh pr comment <pr_number> --body $'<!-- symphony -->\\nMerge conflicts resolved.'

Resolve conflicts carefully — preserve the intent of both sides where possible.
"""
