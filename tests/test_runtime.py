from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from symphony.agents.base import AgentEvent, AgentEventType, AgentSession, TaskResult, TokenUsage, TurnResult
from symphony.config import WorkflowConfig
from symphony.feedback import ClassifyError, FeedbackSignal
from symphony.http_server import build_state_snapshot
from symphony.orchestrator import RetryEntry
from symphony.runtime import SymphonyRuntime
from symphony.tracker.models import Issue


def make_config(workspace_root: Path) -> WorkflowConfig:
    return WorkflowConfig.from_mapping(
        {
            "tracker": {
                "kind": "linear",
                "active_states": ["Todo", "In Progress"],
                "terminal_states": ["Done", "Canceled"],
            },
            "workspace": {"root": str(workspace_root)},
            "agent": {"max_concurrent_agents": 2, "max_retry_backoff_ms": 300_000},
            "polling": {"interval_ms": 5_000},
        }
    )


def issue(
    issue_id: str = "issue-1",
    identifier: str = "IN-200",
    *,
    state: str = "Todo",
    priority: int | None = 1,
) -> Issue:
    return Issue(
        id=issue_id,
        identifier=identifier,
        title=f"{identifier} title",
        description="Runtime glue",
        priority=priority,
        state=state,
        branch_name=None,
        url=f"https://linear.app/example/issue/{identifier}",
    )


class ManualClock:
    def __init__(self, now_ms: int = 1_000) -> None:
        self.now_ms = now_ms

    def __call__(self) -> int:
        return self.now_ms


class FakeTracker:
    def __init__(self, candidates: list[Issue], *, states: list[Issue] | None = None) -> None:
        self.candidates = candidates
        self.states = states
        self.fetch_calls = 0
        self.refresh_calls: list[list[str]] = []

    async def fetch_candidate_issues(self) -> list[Issue]:
        self.fetch_calls += 1
        return list(self.candidates)

    async def fetch_issue_states_by_ids(self, issue_ids: list[str]) -> list[Issue]:
        self.refresh_calls.append(issue_ids)
        source = self.candidates if self.states is None else self.states
        by_id = {item.id: item for item in source}
        return [by_id[issue_id] for issue_id in issue_ids if issue_id in by_id]


@dataclass(frozen=True)
class FakeWorkspace:
    path: Path
    workspace_key: str
    created_now: bool = True


class FakeWorkspaceManager:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[tuple[str, str]] = []

    async def prepare_for_issue(self, target: Issue) -> FakeWorkspace:
        self.calls.append(("prepare", target.identifier))
        path = self.root / target.identifier
        path.mkdir(parents=True, exist_ok=True)
        return FakeWorkspace(path=path, workspace_key=target.identifier)

    async def before_run(self, workspace: FakeWorkspace) -> None:
        self.calls.append(("before_run", workspace.workspace_key))

    async def after_run(self, workspace: FakeWorkspace) -> None:
        self.calls.append(("after_run", workspace.workspace_key))

    async def cleanup(self, identifier: str) -> bool:
        self.calls.append(("cleanup", identifier))
        return True


class FakeSessionRunner:
    def __init__(self, *, success: bool = True, exit_reason: str = "turn_completed") -> None:
        self.success = success
        self.exit_reason = exit_reason
        self.prompts: list[str] = []
        self.sessions_stopped: list[str] = []
        self.snapshots_during_turn: list[dict] = []
        self.runtime: SymphonyRuntime | None = None

    async def start_session(self, workspace: Path) -> AgentSession:
        return AgentSession(id="session-1", workspace=workspace)

    async def run_turn(self, session: AgentSession, prompt: str, target: Issue, on_event) -> TurnResult:
        self.prompts.append(prompt)
        await on_event(
            AgentEvent(
                type=AgentEventType.SESSION_STARTED,
                message="started",
                issue_id=target.id,
                issue_identifier=target.identifier,
                session_id=session.id,
            )
        )
        if self.runtime is not None:
            self.snapshots_during_turn.append(build_state_snapshot(self.runtime.snapshot()))
        await on_event(
            AgentEvent(
                type=AgentEventType.TURN_COMPLETED if self.success else AgentEventType.TURN_FAILED,
                message=self.exit_reason,
                issue_id=target.id,
                issue_identifier=target.identifier,
                session_id=session.id,
            )
        )
        return TurnResult(
            success=self.success,
            exit_reason=self.exit_reason,
            usage=TokenUsage.from_input_output(10, 5),
        )

    async def stop_session(self, session: AgentSession) -> None:
        self.sessions_stopped.append(session.id)


class RaisingSessionRunner(FakeSessionRunner):
    async def run_turn(self, session: AgentSession, prompt: str, target: Issue, on_event) -> TurnResult:
        self.prompts.append(prompt)
        raise RuntimeError("agent exploded")


class FakeAPIRunner:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def run_task(self, workspace: Path, prompt: str, target: Issue, on_event) -> TaskResult:
        self.prompts.append(prompt)
        await on_event(
            AgentEvent(
                type=AgentEventType.TASK_COMPLETED,
                message="task completed",
                issue_id=target.id,
                issue_identifier=target.identifier,
            )
        )
        return TaskResult(success=True, exit_reason="task_completed", output_paths=(workspace / "artifact.txt",))


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_tick_dispatches_issue_runs_workspace_and_schedules_continuation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = issue()
            runner = FakeSessionRunner()
            runtime = SymphonyRuntime(
                config=make_config(Path(temp_dir) / "workspaces"),
                prompt_template="Work on {{ issue.identifier }} attempt={{ attempt }}",
                tracker=FakeTracker([target]),
                workspace_manager=FakeWorkspaceManager(Path(temp_dir) / "workspaces"),
                runner=runner,
                clock_ms=ManualClock(10_000),
            )
            runner.runtime = runtime

            result = await runtime.run_tick()

        self.assertEqual(1, result.fetched)
        self.assertEqual(("IN-200",), result.dispatched)
        self.assertEqual(("IN-200",), result.completed)
        self.assertEqual((), result.failed)
        self.assertEqual(["Work on IN-200 attempt=None"], runner.prompts)
        self.assertEqual(["session-1"], runner.sessions_stopped)
        self.assertNotIn(target.id, runtime.state.running)
        self.assertIn(target.id, runtime.state.retry_attempts)
        retry = runtime.state.retry_attempts[target.id]
        self.assertEqual(1, retry.attempt)
        self.assertIsNone(retry.error)
        self.assertEqual("IN-200", retry.identifier)
        self.assertEqual(1, runner.snapshots_during_turn[0]["counts"]["running"])
        self.assertEqual("session-1", runner.snapshots_during_turn[0]["running"][0]["session_id"])

    async def test_failure_schedules_retry_and_still_stops_session(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = issue()
            runner = FakeSessionRunner(success=False, exit_reason="turn_failed")
            runtime = SymphonyRuntime(
                config=make_config(Path(temp_dir) / "workspaces"),
                prompt_template="Work on {{ issue.identifier }}",
                tracker=FakeTracker([target]),
                workspace_manager=FakeWorkspaceManager(Path(temp_dir) / "workspaces"),
                runner=runner,
                clock_ms=ManualClock(20_000),
            )

            result = await runtime.run_tick()

        self.assertEqual(("IN-200",), result.failed)
        self.assertEqual({"IN-200": "turn_failed"}, result.errors)
        self.assertEqual(["session-1"], runner.sessions_stopped)
        retry = runtime.state.retry_attempts[target.id]
        self.assertEqual(1, retry.attempt)
        self.assertEqual("turn_failed", retry.error)
        self.assertEqual(30_000, retry.due_at_ms)

    async def test_exception_schedules_retry_and_runs_after_run_best_effort(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = issue()
            workspace_manager = FakeWorkspaceManager(Path(temp_dir) / "workspaces")
            runner = RaisingSessionRunner()
            runtime = SymphonyRuntime(
                config=make_config(Path(temp_dir) / "workspaces"),
                prompt_template="Work on {{ issue.identifier }}",
                tracker=FakeTracker([target]),
                workspace_manager=workspace_manager,
                runner=runner,
                clock_ms=ManualClock(40_000),
            )

            result = await runtime.run_tick()

        self.assertEqual(("IN-200",), result.failed)
        self.assertEqual("agent exploded", result.errors["IN-200"])
        self.assertIn(("after_run", "IN-200"), workspace_manager.calls)
        self.assertEqual(["session-1"], runner.sessions_stopped)
        self.assertEqual("agent exploded", runtime.state.retry_attempts[target.id].error)

    async def test_due_retry_is_redispatched_with_attempt_context(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = issue()
            clock = ManualClock(50_000)
            runtime = SymphonyRuntime(
                config=make_config(Path(temp_dir) / "workspaces"),
                prompt_template="Retry attempt {{ attempt }} for {{ issue.identifier }}",
                tracker=FakeTracker([target]),
                workspace_manager=FakeWorkspaceManager(Path(temp_dir) / "workspaces"),
                runner=FakeSessionRunner(),
                clock_ms=clock,
            )
            runtime.state.retry_attempts[target.id] = RetryEntry(
                issue_id=target.id,
                identifier=target.identifier,
                attempt=3,
                due_at_ms=clock.now_ms,
                error="previous failure",
            )

            result = await runtime.run_tick()

        self.assertEqual(("IN-200",), result.dispatched)
        self.assertEqual(["Retry attempt 3 for IN-200"], runtime.runner.prompts)
        self.assertEqual(1, runtime.state.retry_attempts[target.id].attempt)
        self.assertIsNone(runtime.state.retry_attempts[target.id].error)

    async def test_retry_missing_from_candidate_poll_is_released(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = SymphonyRuntime(
                config=make_config(Path(temp_dir) / "workspaces"),
                prompt_template="Work",
                tracker=FakeTracker([]),
                workspace_manager=FakeWorkspaceManager(Path(temp_dir) / "workspaces"),
                runner=FakeSessionRunner(),
                clock_ms=ManualClock(10_000),
            )
            runtime.state.retry_attempts["issue-1"] = RetryEntry(
                issue_id="issue-1",
                identifier="IN-200",
                attempt=1,
                due_at_ms=10_000,
                error=None,
            )
            runtime.state.claimed.add("issue-1")

            result = await runtime.run_tick()

        self.assertEqual(("IN-200",), result.released)
        self.assertNotIn("issue-1", runtime.state.retry_attempts)
        self.assertNotIn("issue-1", runtime.state.claimed)

    async def test_successful_retry_missing_from_candidates_cleans_terminal_workspace(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            terminal_issue = issue(state="Done")
            workspace_manager = FakeWorkspaceManager(Path(temp_dir) / "workspaces")
            runtime = SymphonyRuntime(
                config=make_config(Path(temp_dir) / "workspaces"),
                prompt_template="Work",
                tracker=FakeTracker([], states=[terminal_issue]),
                workspace_manager=workspace_manager,
                runner=FakeSessionRunner(),
                clock_ms=ManualClock(10_000),
            )
            runtime.state.retry_attempts[terminal_issue.id] = RetryEntry(
                issue_id=terminal_issue.id,
                identifier=terminal_issue.identifier,
                attempt=1,
                due_at_ms=10_000,
                error=None,
            )
            runtime.state.claimed.add(terminal_issue.id)

            result = await runtime.run_tick()

        self.assertEqual((terminal_issue.identifier,), result.released)
        self.assertIn(("cleanup", terminal_issue.identifier), workspace_manager.calls)
        self.assertEqual([[terminal_issue.id]], runtime.tracker.refresh_calls)
        self.assertNotIn(terminal_issue.id, runtime.state.retry_attempts)

    async def test_api_runner_path_uses_run_task_contract(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = issue(identifier="IN-201")
            runner = FakeAPIRunner()
            runtime = SymphonyRuntime(
                config=make_config(Path(temp_dir) / "workspaces"),
                prompt_template="Generate {{ issue.identifier }}",
                tracker=FakeTracker([target]),
                workspace_manager=FakeWorkspaceManager(Path(temp_dir) / "workspaces"),
                runner=runner,
                clock_ms=ManualClock(10_000),
            )

            result = await runtime.run_tick()

        self.assertEqual(("IN-201",), result.completed)
        self.assertEqual(["Generate IN-201"], runner.prompts)
        self.assertEqual(1, len(runtime.state.recent_events))
        self.assertEqual("task_completed", runtime.state.recent_events[0]["event"])


    async def test_record_startup_issues_skips_preexisting_issues_on_first_tick(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            pre_existing = issue("pre-1", "IN-300")
            runtime = SymphonyRuntime(
                config=make_config(Path(temp_dir) / "workspaces"),
                prompt_template="Work on {{ issue.identifier }}",
                tracker=FakeTracker([pre_existing]),
                workspace_manager=FakeWorkspaceManager(Path(temp_dir) / "workspaces"),
                runner=FakeSessionRunner(),
                clock_ms=ManualClock(1_000),
            )

            await runtime.record_startup_issues()
            result = await runtime.run_tick()

        self.assertEqual((), result.dispatched)
        self.assertEqual(1, result.fetched)

    async def test_issue_reentering_after_startup_is_dispatched(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            pre_existing = issue("pre-1", "IN-301")
            clock = ManualClock(1_000)
            runner = FakeSessionRunner()

            # Startup: issue is active.
            tracker = FakeTracker([pre_existing])
            runtime = SymphonyRuntime(
                config=make_config(Path(temp_dir) / "workspaces"),
                prompt_template="Work on {{ issue.identifier }}",
                tracker=tracker,
                workspace_manager=FakeWorkspaceManager(Path(temp_dir) / "workspaces"),
                runner=runner,
                clock_ms=clock,
            )
            await runtime.record_startup_issues()

            # Tick 1: pre-existing issue is skipped.
            result1 = await runtime.run_tick()
            self.assertEqual((), result1.dispatched)

            # Tick 2: issue leaves candidates (Done/Canceled).
            tracker.candidates = []
            result2 = await runtime.run_tick()
            self.assertEqual((), result2.dispatched)

            # Tick 3: issue re-enters candidates — must be dispatched now.
            tracker.candidates = [pre_existing]
            result3 = await runtime.run_tick()
            self.assertEqual(("IN-301",), result3.dispatched)


class FeedbackTracker(FakeTracker):
    """Extends FakeTracker with comment/state-transition stubs for feedback gate tests."""

    def __init__(
        self,
        candidates: list[Issue],
        *,
        review_issues: list[Issue] | None = None,
        comment_ids: dict[str, list[str]] | None = None,
        comments: dict[str, list[str]] | None = None,
    ) -> None:
        super().__init__(candidates)
        self._review_issues: list[Issue] = review_issues or []
        # comment_ids[issue_id] and comments[issue_id] are parallel lists (same length)
        self._comment_ids: dict[str, list[str]] = comment_ids or {}
        self._comments: dict[str, list[str]] = comments or {}
        self.state_transitions: list[tuple[str, str]] = []

    def fetch_issues_by_states(self, state_names: list[str]) -> list[Issue]:
        return list(self._review_issues)

    def fetch_issue_comments_with_ids(self, issue_id: str) -> list[tuple[str, str]]:
        ids = self._comment_ids.get(issue_id, [])
        texts = self._comments.get(issue_id, [])
        return list(zip(ids, texts))

    def update_issue_state_by_name(self, issue_id: str, state_name: str) -> bool:
        self.state_transitions.append((issue_id, state_name))
        return True


class FeedbackGateTests(unittest.IsolatedAsyncioTestCase):
    def _make_runtime(self, tracker, temp_dir: str) -> SymphonyRuntime:
        return SymphonyRuntime(
            config=make_config(Path(temp_dir) / "workspaces"),
            prompt_template="Work on {{ issue.identifier }}",
            tracker=tracker,
            workspace_manager=FakeWorkspaceManager(Path(temp_dir) / "workspaces"),
            runner=FakeSessionRunner(),
        )

    async def test_approve_signal_transitions_to_done(self):
        review_issue = issue("r-1", "IN-500", state="In Review")
        tracker = FeedbackTracker(
            [],
            review_issues=[review_issue],
            comment_ids={"r-1": ["c-1"]},
            comments={"r-1": ["Alice: LGTM"]},
        )
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self._make_runtime(tracker, tmp)
            with patch("symphony.runtime.classify_feedback", return_value=FeedbackSignal.APPROVE), \
                 patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
                await runtime.poll_feedback()

        self.assertEqual([("r-1", "Done")], tracker.state_transitions)

    async def test_change_request_signal_transitions_to_first_active_state(self):
        review_issue = issue("r-2", "IN-501", state="In Review")
        tracker = FeedbackTracker(
            [],
            review_issues=[review_issue],
            comment_ids={"r-2": ["c-1"]},
            comments={"r-2": ["Bob: Please fix the naming"]},
        )
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self._make_runtime(tracker, tmp)
            with patch("symphony.runtime.classify_feedback", return_value=FeedbackSignal.CHANGE_REQUEST), \
                 patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
                await runtime.poll_feedback()

        self.assertEqual([("r-2", "Todo")], tracker.state_transitions)

    async def test_close_signal_transitions_to_cancelled(self):
        review_issue = issue("r-3", "IN-502", state="In Review")
        tracker = FeedbackTracker(
            [],
            review_issues=[review_issue],
            comment_ids={"r-3": ["c-1"]},
            comments={"r-3": ["Carol: closed, not needed"]},
        )
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self._make_runtime(tracker, tmp)
            with patch("symphony.runtime.classify_feedback", return_value=FeedbackSignal.CLOSE), \
                 patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
                await runtime.poll_feedback()

        self.assertEqual([("r-3", "Canceled")], tracker.state_transitions)

    async def test_no_transition_when_no_new_comments(self):
        review_issue = issue("r-4", "IN-503", state="In Review")
        tracker = FeedbackTracker(
            [],
            review_issues=[review_issue],
            comment_ids={"r-4": ["c-1"]},
            comments={"r-4": ["Alice: LGTM"]},
        )
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self._make_runtime(tracker, tmp)
            with patch("symphony.runtime.classify_feedback", return_value=FeedbackSignal.APPROVE), \
                 patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
                # First poll: marks c-1 as seen.
                await runtime.poll_feedback()
                tracker.state_transitions.clear()
                # Second poll: same comment IDs → no new comments → no transition.
                await runtime.poll_feedback()

        self.assertEqual([], tracker.state_transitions)

    async def test_no_transition_when_no_signal_in_comments(self):
        review_issue = issue("r-5", "IN-504", state="In Review")
        tracker = FeedbackTracker(
            [],
            review_issues=[review_issue],
            comment_ids={"r-5": ["c-1"]},
            comments={"r-5": ["Dave: looks interesting"]},
        )
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self._make_runtime(tracker, tmp)
            with patch("symphony.runtime.classify_feedback", return_value=None), \
                 patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
                await runtime.poll_feedback()

        self.assertEqual([], tracker.state_transitions)

    async def test_old_signal_not_refired_on_new_non_signal_comment(self):
        """Re-open scenario: old LGTM must not re-fire when a new non-signal comment arrives."""
        review_issue = issue("r-x", "IN-510", state="In Review")
        tracker = FeedbackTracker(
            [],
            review_issues=[review_issue],
            comment_ids={"r-x": ["c-1"]},
            comments={"r-x": ["Alice: LGTM"]},
        )
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self._make_runtime(tracker, tmp)
            with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
                with patch("symphony.runtime.classify_feedback", return_value=FeedbackSignal.APPROVE):
                    # First poll: c-1 (LGTM) fires → Done transition.
                    await runtime.poll_feedback()
                tracker.state_transitions.clear()
                # Simulate re-open: a new non-signal comment arrives (c-2).
                tracker._comment_ids["r-x"] = ["c-1", "c-2"]
                tracker._comments["r-x"] = ["Alice: LGTM", "Bob: re-opened for discussion"]
                with patch("symphony.runtime.classify_feedback", return_value=None):
                    # Second poll: only c-2 is new; it carries no signal → no transition.
                    await runtime.poll_feedback()

        self.assertEqual([], tracker.state_transitions)

    async def test_signal_retried_when_state_update_fails(self):
        """If update_issue_state_by_name returns False, the signal must be retried next poll."""
        review_issue = issue("r-f", "IN-520", state="In Review")

        class FailThenSucceedTracker(FeedbackTracker):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self._call_count = 0

            def update_issue_state_by_name(self, issue_id: str, state_name: str) -> bool:
                self._call_count += 1
                if self._call_count == 1:
                    return False  # first attempt fails
                self.state_transitions.append((issue_id, state_name))
                return True

        tracker = FailThenSucceedTracker(
            [],
            review_issues=[review_issue],
            comment_ids={"r-f": ["c-1"]},
            comments={"r-f": ["Alice: LGTM"]},
        )
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self._make_runtime(tracker, tmp)
            with patch("symphony.runtime.classify_feedback", return_value=FeedbackSignal.APPROVE), \
                 patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
                await runtime.poll_feedback()  # first poll: update fails, not marked seen
                await runtime.poll_feedback()  # second poll: retries and succeeds

        self.assertEqual([("r-f", "Done")], tracker.state_transitions)

    async def test_classify_error_not_marked_seen_retried_next_poll(self):
        """ClassifyError (API failure) must leave _feedback_seen unset so the signal is retried."""
        review_issue = issue("r-e", "IN-530", state="In Review")
        tracker = FeedbackTracker(
            [],
            review_issues=[review_issue],
            comment_ids={"r-e": ["c-1"]},
            comments={"r-e": ["Alice: LGTM"]},
        )
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self._make_runtime(tracker, tmp)
            with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
                with patch("symphony.runtime.classify_feedback", side_effect=ClassifyError("timeout")):
                    await runtime.poll_feedback()  # fails: _feedback_seen NOT updated
                self.assertEqual([], tracker.state_transitions)
                self.assertNotIn("r-e", runtime._feedback_seen)

                with patch("symphony.runtime.classify_feedback", return_value=FeedbackSignal.APPROVE):
                    await runtime.poll_feedback()  # retries and succeeds

        self.assertEqual([("r-e", "Done")], tracker.state_transitions)

    async def test_no_api_key_disables_feedback_gate(self):
        """When ANTHROPIC_API_KEY is absent, poll_feedback must skip without calling classify."""
        review_issue = issue("r-k", "IN-540", state="In Review")
        tracker = FeedbackTracker(
            [],
            review_issues=[review_issue],
            comment_ids={"r-k": ["c-1"]},
            comments={"r-k": ["Alice: LGTM"]},
        )
        # Build an env dict without ANTHROPIC_API_KEY
        env_no_key = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self._make_runtime(tracker, tmp)
            with patch.dict("os.environ", env_no_key, clear=True):
                with patch("symphony.runtime.classify_feedback") as mock_classify:
                    await runtime.poll_feedback()
                    await runtime.poll_feedback()  # second call: flag already set, no duplicate warn

        self.assertEqual([], tracker.state_transitions)
        mock_classify.assert_not_called()
        self.assertTrue(runtime._warned_no_api_key)

    async def test_no_review_issues_does_nothing(self):
        tracker = FeedbackTracker([], review_issues=[])
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self._make_runtime(tracker, tmp)
            await runtime.poll_feedback()

        self.assertEqual([], tracker.state_transitions)

    async def test_poll_feedback_called_during_run_tick(self):
        """poll_feedback is invoked as part of run_tick."""
        review_issue = issue("r-6", "IN-505", state="In Review")
        tracker = FeedbackTracker(
            [],
            review_issues=[review_issue],
            comment_ids={"r-6": ["c-1"]},
            comments={"r-6": ["Eve: LGTM"]},
        )
        with tempfile.TemporaryDirectory() as tmp:
            runtime = SymphonyRuntime(
                config=make_config(Path(tmp) / "workspaces"),
                prompt_template="Work on {{ issue.identifier }}",
                tracker=tracker,
                workspace_manager=FakeWorkspaceManager(Path(tmp) / "workspaces"),
                runner=FakeSessionRunner(),
            )
            with patch("symphony.runtime.classify_feedback", return_value=FeedbackSignal.APPROVE), \
                 patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
                await runtime.run_tick()

        self.assertEqual([("r-6", "Done")], tracker.state_transitions)


if __name__ == "__main__":
    unittest.main()
