from __future__ import annotations

import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from symphony.agents.base import AgentEvent, AgentEventType, AgentSession, TaskResult, TokenUsage, TurnResult
from symphony.config import WorkflowConfig
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


class BlockerGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_blocked_candidate_emits_blocker_skip_and_is_not_dispatched(self):
        from symphony.tracker.models import Blocker

        with tempfile.TemporaryDirectory() as temp_dir:
            blocked = Issue(
                id="issue-blocked",
                identifier="IN-700",
                title="Blocked work",
                description=None,
                priority=1,
                state="Todo",
                branch_name=None,
                url=None,
                blocked_by=(Blocker(id="b1", identifier="IN-699", state="In Progress"),),
            )
            clean = issue(issue_id="issue-clean", identifier="IN-701")
            runtime = SymphonyRuntime(
                config=make_config(Path(temp_dir) / "workspaces"),
                prompt_template="x",
                tracker=FakeTracker([blocked, clean]),
                workspace_manager=FakeWorkspaceManager(Path(temp_dir) / "workspaces"),
                runner=FakeSessionRunner(),
                clock_ms=ManualClock(10_000),
            )

            with self.assertLogs("symphony.runtime", level="INFO") as logs:
                result = await runtime.run_tick()

            self.assertEqual(("IN-701",), result.dispatched)
            self.assertTrue(
                any("blocker_skip" in line and "IN-700" in line and "IN-699" in line for line in logs.output),
                logs.output,
            )

    async def test_unblocked_candidate_becomes_dispatchable_on_next_tick(self):
        # Regression: a non-Todo candidate that first appears blocked is
        # marked seen via _prev_candidate_ids. If the blocker later resolves
        # while the candidate row stays in the same active state, it's no
        # longer "new" in the candidate diff — without the blocked→unblocked
        # tracking the issue is stranded forever.
        from symphony.tracker.models import Blocker

        with tempfile.TemporaryDirectory() as temp_dir:
            blocker = Blocker(id="b1", identifier="IN-UP", state="In Progress")
            blocked = Issue(
                id="issue-X",
                identifier="IN-X",
                title="held by upstream",
                description=None,
                priority=1,
                state="In Progress",  # non-Todo active state
                branch_name=None,
                url=None,
                blocked_by=(blocker,),
            )
            tracker = FakeTracker([blocked])
            runtime = SymphonyRuntime(
                config=make_config(Path(temp_dir) / "workspaces"),
                prompt_template="x",
                tracker=tracker,
                workspace_manager=FakeWorkspaceManager(Path(temp_dir) / "workspaces"),
                runner=FakeSessionRunner(),
                clock_ms=ManualClock(10_000),
            )

            # Tick 1: blocked → not dispatched, marked seen.
            result1 = await runtime.run_tick()
            self.assertEqual((), result1.dispatched)
            self.assertIn(blocked.id, runtime._prev_blocked_ids)

            # Tick 2: blocker resolves (Done). The candidate row is the same
            # but the blocker state changed. Must be re-evaluated for dispatch.
            unblocked = Issue(
                id=blocked.id,
                identifier=blocked.identifier,
                title=blocked.title,
                description=blocked.description,
                priority=blocked.priority,
                state=blocked.state,
                branch_name=None,
                url=None,
                blocked_by=(Blocker(id="b1", identifier="IN-UP", state="Done"),),
            )
            tracker.candidates = [unblocked]

            result2 = await runtime.run_tick()
            self.assertEqual(("IN-X",), result2.dispatched)
            self.assertNotIn(unblocked.id, runtime._prev_blocked_ids)

    async def test_blocker_skip_only_logged_on_first_appearance(self):
        from symphony.tracker.models import Blocker

        with tempfile.TemporaryDirectory() as temp_dir:
            blocked = Issue(
                id="issue-blocked",
                identifier="IN-702",
                title="Blocked",
                description=None,
                priority=1,
                state="Todo",
                branch_name=None,
                url=None,
                blocked_by=(Blocker(id="b1", identifier="IN-699", state="Todo"),),
            )
            tracker = FakeTracker([blocked])
            runtime = SymphonyRuntime(
                config=make_config(Path(temp_dir) / "workspaces"),
                prompt_template="x",
                tracker=tracker,
                workspace_manager=FakeWorkspaceManager(Path(temp_dir) / "workspaces"),
                runner=FakeSessionRunner(),
                clock_ms=ManualClock(10_000),
            )

            with self.assertLogs("symphony.runtime", level="INFO") as logs:
                await runtime.run_tick()  # first tick — new candidate, should log
                await runtime.run_tick()  # second tick — same candidate, no new log

            blocker_lines = [line for line in logs.output if "blocker_skip" in line]
            self.assertEqual(1, len(blocker_lines), blocker_lines)


class FailureStateRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def _config(self, workspace_root: Path, *, failure_state: str | None, keep_on_failure: bool = False) -> WorkflowConfig:
        ws: dict = {"root": str(workspace_root)}
        if keep_on_failure:
            ws["keep_on_failure"] = True
        tracker_cfg = {
            "kind": "linear",
            "active_states": ["Todo"],
            "terminal_states": ["Done", "Cancelled"],
        }
        if failure_state:
            tracker_cfg["failure_state"] = failure_state
        return WorkflowConfig.from_mapping(
            {
                "tracker": tracker_cfg,
                "workspace": ws,
                "agent": {"max_concurrent_agents": 1},
                "polling": {"interval_ms": 5_000},
            }
        )

    async def test_worker_failure_moves_issue_to_failure_state_and_cleans_workspace(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = _team_issue()
            tracker = ClaimingFakeTracker([target])
            workspace_manager = FakeWorkspaceManager(Path(temp_dir) / "workspaces")
            runner = FakeSessionRunner(success=False, exit_reason="agent_crashed")
            runtime = SymphonyRuntime(
                config=self._config(Path(temp_dir) / "workspaces", failure_state="Cancelled"),
                prompt_template="x",
                tracker=tracker,
                workspace_manager=workspace_manager,
                runner=runner,
                clock_ms=ManualClock(10_000),
            )

            with self.assertLogs("symphony.runtime", level="WARNING") as logs:
                result = await runtime.run_tick()

            self.assertEqual(("IN-501",), result.failed)
            self.assertEqual([(target.id, "team-1", "Cancelled")], tracker.move_calls)
            # No retry scheduled — issue is fully released.
            self.assertEqual({}, runtime.state.retry_attempts)
            self.assertNotIn(target.id, runtime.state.claimed)
            self.assertNotIn(target.id, runtime.state.running)
            self.assertTrue(any("run_failed" in line and "Cancelled" in line for line in logs.output))
            # Workspace cleanup happened (the fake records cleanup calls).
            self.assertIn(("cleanup", "IN-501"), workspace_manager.calls)

    async def test_keep_on_failure_skips_workspace_cleanup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = _team_issue()
            tracker = ClaimingFakeTracker([target])
            workspace_manager = FakeWorkspaceManager(Path(temp_dir) / "workspaces")
            runner = FakeSessionRunner(success=False, exit_reason="agent_crashed")
            runtime = SymphonyRuntime(
                config=self._config(
                    Path(temp_dir) / "workspaces",
                    failure_state="Cancelled",
                    keep_on_failure=True,
                ),
                prompt_template="x",
                tracker=tracker,
                workspace_manager=workspace_manager,
                runner=runner,
                clock_ms=ManualClock(10_000),
            )

            result = await runtime.run_tick()

            self.assertEqual(("IN-501",), result.failed)
            self.assertNotIn(("cleanup", "IN-501"), workspace_manager.calls)

    async def test_unresolvable_approval_now_moves_to_failure_state(self):
        # IN-289 layer on top of IN-288: when both approval_state and
        # failure_state are missing, approval failures park (IN-288 behavior).
        # When failure_state is configured, they should additionally move.
        with tempfile.TemporaryDirectory() as temp_dir:
            target = _team_issue()
            tracker = ClaimingFakeTracker([target])
            workspace_manager = FakeWorkspaceManager(Path(temp_dir) / "workspaces")
            runner = FakeSessionRunner(success=False, exit_reason="approval_required")
            runtime = SymphonyRuntime(
                config=self._config(Path(temp_dir) / "workspaces", failure_state="Cancelled"),
                prompt_template="x",
                tracker=tracker,
                workspace_manager=workspace_manager,
                runner=runner,
                clock_ms=ManualClock(10_000),
            )

            result = await runtime.run_tick()

            self.assertEqual(("IN-501",), result.failed)
            self.assertEqual("approval_unreachable", result.errors["IN-501"])
            self.assertEqual([(target.id, "team-1", "Cancelled")], tracker.move_calls)
            self.assertNotIn(target.id, runtime.state.claimed)

    async def test_approval_state_takes_priority_over_failure_state(self):
        # Regression: when BOTH approval_state and failure_state are configured,
        # an approval_required exit must route to approval_state (PR #38 review
        # fix #1). Previously the runtime called _terminate_run unconditionally
        # for approval failures, which moved the issue to failure_state and
        # bypassed the approval resolution path entirely.
        with tempfile.TemporaryDirectory() as temp_dir:
            target = _team_issue()
            tracker = ClaimingFakeTracker([target])
            runner = FakeSessionRunner(success=False, exit_reason="approval_required")
            config = WorkflowConfig.from_mapping(
                {
                    "tracker": {
                        "kind": "linear",
                        "active_states": ["Todo"],
                        "terminal_states": ["Done", "Cancelled"],
                        "approval_state": "Needs Approval",
                        "failure_state": "Cancelled",
                    },
                    "workspace": {"root": str(Path(temp_dir) / "workspaces")},
                    "agent": {"max_concurrent_agents": 1},
                    "polling": {"interval_ms": 5_000},
                }
            )
            runtime = SymphonyRuntime(
                config=config,
                prompt_template="x",
                tracker=tracker,
                workspace_manager=FakeWorkspaceManager(Path(temp_dir) / "workspaces"),
                runner=runner,
                clock_ms=ManualClock(10_000),
            )

            result = await runtime.run_tick()

            self.assertEqual("approval_pending", result.errors["IN-501"])
            # Move went to approval_state, NOT failure_state.
            self.assertEqual(
                [(target.id, "team-1", "Needs Approval")], tracker.move_calls
            )

    async def test_workspace_failure_after_claim_does_not_double_move_to_failure_state(self):
        # Regression: when both queued_state and failure_state are configured
        # and workspace prep fails after a successful claim, the rollback
        # handler moves the issue back to queued_state. The outer failure
        # handler MUST NOT also move it to failure_state — that would
        # double-move the ticket and turn a recoverable workspace error into
        # a cancelled Linear issue (PR #38 review fix #2).
        with tempfile.TemporaryDirectory() as temp_dir:
            target = _team_issue()
            tracker = ClaimingFakeTracker([target])
            workspace_manager = FailingWorkspaceManager(Path(temp_dir) / "workspaces")
            config = WorkflowConfig.from_mapping(
                {
                    "tracker": {
                        "kind": "linear",
                        "active_states": ["Todo", "In Progress"],
                        "terminal_states": ["Done", "Cancelled"],
                        "in_progress_state": "In Progress",
                        "queued_state": "Todo",
                        "failure_state": "Cancelled",
                    },
                    "workspace": {"root": str(Path(temp_dir) / "workspaces")},
                    "agent": {"max_concurrent_agents": 1},
                    "polling": {"interval_ms": 5_000},
                }
            )
            runtime = SymphonyRuntime(
                config=config,
                prompt_template="x",
                tracker=tracker,
                workspace_manager=workspace_manager,
                runner=FakeSessionRunner(),
                clock_ms=ManualClock(10_000),
            )

            with self.assertLogs("symphony.runtime", level="WARNING") as logs:
                await runtime.run_tick()

            # Exactly two moves: claim → In Progress, then rollback → Todo.
            # Failure_state must NOT appear in the call sequence.
            self.assertEqual(
                [
                    (target.id, "team-1", "In Progress"),
                    (target.id, "team-1", "Todo"),
                ],
                tracker.move_calls,
            )
            self.assertTrue(any("claim_rollback:" in line for line in logs.output))
            # And run_failed should not be logged — the rollback owns the path.
            self.assertFalse(any("run_failed:" in line for line in logs.output))

    async def test_failure_state_move_failure_schedules_retry(self):
        # Regression: if the failure_state move raises (transient Linear
        # outage), the original code logged and released, leaving the issue
        # in its active state with `_prev_candidate_ids` already containing
        # it — stranded with no retry.
        with tempfile.TemporaryDirectory() as temp_dir:
            target = _team_issue()
            tracker = ClaimingFakeTracker([target])
            runner = FakeSessionRunner(success=False, exit_reason="agent_crashed")

            # Patch move_issue_to_state to raise unconditionally. With no
            # claim guard configured this is the only move attempt.
            tracker.move_issue_to_state = (
                lambda issue_id, team_id, state_name: (_ for _ in ()).throw(
                    RuntimeError("linear_outage")
                )
            )

            runtime = SymphonyRuntime(
                config=self._config(Path(temp_dir) / "workspaces", failure_state="Cancelled"),
                prompt_template="x",
                tracker=tracker,
                workspace_manager=FakeWorkspaceManager(Path(temp_dir) / "workspaces"),
                runner=runner,
                clock_ms=ManualClock(10_000),
            )

            result = await runtime.run_tick()

            self.assertEqual(("IN-501",), result.failed)
            # Retry scheduled so next tick re-attempts the failure_state move.
            self.assertIn(target.id, runtime.state.retry_attempts)
            self.assertIn(
                "failure_state_move_failed",
                runtime.state.retry_attempts[target.id].error or "",
            )

    async def test_rollback_clears_prev_candidate_marker_for_redispatch(self):
        # Regression: after a successful rollback to queued_state, the issue
        # was released but still in `_prev_candidate_ids`. The candidate-set
        # diff on the next tick treated it as not-new and skipped dispatch
        # until external state churn occurred. Fix: clear the prev-candidate
        # marker so the rolled-back issue is dispatched as new on the next
        # tick.
        with tempfile.TemporaryDirectory() as temp_dir:
            target = _team_issue()
            tracker = ClaimingFakeTracker([target])
            workspace_manager = FailingWorkspaceManager(Path(temp_dir) / "workspaces")
            config = WorkflowConfig.from_mapping(
                {
                    "tracker": {
                        "kind": "linear",
                        "active_states": ["Todo", "In Progress"],
                        "terminal_states": ["Done", "Cancelled"],
                        "in_progress_state": "In Progress",
                        "queued_state": "Todo",
                    },
                    "workspace": {"root": str(Path(temp_dir) / "workspaces")},
                    "agent": {"max_concurrent_agents": 1},
                    "polling": {"interval_ms": 5_000},
                }
            )
            runtime = SymphonyRuntime(
                config=config,
                prompt_template="x",
                tracker=tracker,
                workspace_manager=workspace_manager,
                runner=FakeSessionRunner(),
                clock_ms=ManualClock(10_000),
            )

            await runtime.run_tick()

            # The rolled-back ticket must not be in `_prev_candidate_ids` so
            # the next tick treats it as new and dispatches it.
            self.assertNotIn(target.id, runtime._prev_candidate_ids)

    async def test_legacy_mode_still_retries_when_failure_state_unset(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = _team_issue()
            tracker = ClaimingFakeTracker([target])
            runner = FakeSessionRunner(success=False, exit_reason="agent_crashed")
            runtime = SymphonyRuntime(
                config=self._config(Path(temp_dir) / "workspaces", failure_state=None),
                prompt_template="x",
                tracker=tracker,
                workspace_manager=FakeWorkspaceManager(Path(temp_dir) / "workspaces"),
                runner=runner,
                clock_ms=ManualClock(10_000),
            )

            result = await runtime.run_tick()

            self.assertEqual(("IN-501",), result.failed)
            # No failure-state move attempted, retry still scheduled.
            self.assertEqual([], tracker.move_calls)
            self.assertIn(target.id, runtime.state.retry_attempts)


class ApprovalGateRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_approval_required_without_resolution_parks_issue_without_retry(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = issue()
            runner = FakeSessionRunner(success=False, exit_reason="approval_required")
            runtime = SymphonyRuntime(
                config=make_config(Path(temp_dir) / "workspaces"),  # no approval_state
                prompt_template="x",
                tracker=FakeTracker([target]),
                workspace_manager=FakeWorkspaceManager(Path(temp_dir) / "workspaces"),
                runner=runner,
                clock_ms=ManualClock(10_000),
            )

            with self.assertLogs("symphony.runtime", level="WARNING") as logs:
                result = await runtime.run_tick()

            self.assertEqual(("IN-200",), result.failed)
            self.assertEqual("approval_unreachable", result.errors["IN-200"])
            # No retry scheduled — issue is parked in `claimed` until operator action.
            self.assertEqual({}, runtime.state.retry_attempts)
            self.assertIn(target.id, runtime.state.claimed)
            self.assertNotIn(target.id, runtime.state.running)
            self.assertTrue(any("approval_unreachable" in line for line in logs.output))

    async def test_approval_required_with_resolution_path_moves_to_approval_state(self):
        # When tracker.approval_state IS configured, the runtime must actually
        # use it: move the Linear issue into approval_state and release the
        # dispatch without scheduling a retry. The previous behavior fell
        # through to retry-scheduling, leaving approval_state unused.
        with tempfile.TemporaryDirectory() as temp_dir:
            target = _team_issue()
            tracker = ClaimingFakeTracker([target])
            runner = FakeSessionRunner(success=False, exit_reason="approval_required")
            config = WorkflowConfig.from_mapping(
                {
                    "tracker": {
                        "kind": "linear",
                        "active_states": ["Todo"],
                        "terminal_states": ["Done"],
                        "approval_state": "Needs Approval",
                    },
                    "workspace": {"root": str(Path(temp_dir) / "workspaces")},
                    "agent": {"max_concurrent_agents": 1},
                    "polling": {"interval_ms": 5_000},
                }
            )
            runtime = SymphonyRuntime(
                config=config,
                prompt_template="x",
                tracker=tracker,
                workspace_manager=FakeWorkspaceManager(Path(temp_dir) / "workspaces"),
                runner=runner,
                clock_ms=ManualClock(10_000),
            )

            with self.assertLogs("symphony.runtime", level="WARNING") as logs:
                result = await runtime.run_tick()

            self.assertEqual(("IN-501",), result.failed)
            self.assertEqual("approval_pending", result.errors["IN-501"])
            self.assertEqual([(target.id, "team-1", "Needs Approval")], tracker.move_calls)
            # No retry scheduled; the dispatch is released and the resolver
            # owns the next move.
            self.assertEqual({}, runtime.state.retry_attempts)
            self.assertNotIn(target.id, runtime.state.claimed)
            self.assertTrue(any("approval_pending" in line for line in logs.output))

    async def test_turn_input_required_is_treated_as_approval_required(self):
        # CodexRunner emits `turn_input_required` for generic protocol-level
        # approval frames. Without recognizing it, those exits fell through to
        # normal retry even when approval_state was unset.
        with tempfile.TemporaryDirectory() as temp_dir:
            target = issue()
            runner = FakeSessionRunner(success=False, exit_reason="turn_input_required")
            runtime = SymphonyRuntime(
                config=make_config(Path(temp_dir) / "workspaces"),  # no approval_state
                prompt_template="x",
                tracker=FakeTracker([target]),
                workspace_manager=FakeWorkspaceManager(Path(temp_dir) / "workspaces"),
                runner=runner,
                clock_ms=ManualClock(10_000),
            )

            result = await runtime.run_tick()

            self.assertEqual(("IN-200",), result.failed)
            self.assertEqual("approval_unreachable", result.errors["IN-200"])
            self.assertEqual({}, runtime.state.retry_attempts)

    async def test_parked_claim_is_not_released_while_issue_stays_active(self):
        # Regression for PR #37 recheck: a parked approval claim must NOT be
        # released-and-re-dispatched while the Linear issue stays continuously
        # active. Without the operator-intervention gate, the zombie release
        # fires every tick and the daemon loops the same approval failure on
        # every poll interval.
        from symphony.orchestrator import complete_worker_terminal_failure, dispatch_issue

        with tempfile.TemporaryDirectory() as temp_dir:
            target = issue()
            tracker = FakeTracker([target])
            runtime = SymphonyRuntime(
                config=make_config(Path(temp_dir) / "workspaces"),
                prompt_template="x",
                tracker=tracker,
                workspace_manager=FakeWorkspaceManager(Path(temp_dir) / "workspaces"),
                runner=FakeSessionRunner(),
                clock_ms=ManualClock(10_000),
            )
            dispatch_issue(target, runtime.state, now_ms=runtime.clock_ms())
            complete_worker_terminal_failure(target.id, runtime.state)
            self.assertIn(target.id, runtime.state.claimed)
            # Issue stayed in candidates across the prior tick — no operator
            # intervention.
            runtime._prev_candidate_ids = {target.id}

            result = await runtime.run_tick()

            self.assertEqual((), result.dispatched)
            self.assertIn(target.id, runtime.state.claimed)

    async def test_zombie_claim_released_after_operator_intervention(self):
        # After `complete_worker_terminal_failure` parks an issue in
        # `state.claimed`, an operator that moves the ticket OUT of and
        # back INTO an active state must be able to unblock the same daemon.
        # Operator intervention is detected by the issue being absent from
        # `_prev_candidate_ids` (i.e., it left the candidate set last tick).
        from symphony.orchestrator import complete_worker_terminal_failure, dispatch_issue

        with tempfile.TemporaryDirectory() as temp_dir:
            target = issue()
            tracker = FakeTracker([target])
            runtime = SymphonyRuntime(
                config=make_config(Path(temp_dir) / "workspaces"),
                prompt_template="x",
                tracker=tracker,
                workspace_manager=FakeWorkspaceManager(Path(temp_dir) / "workspaces"),
                runner=FakeSessionRunner(),
                clock_ms=ManualClock(10_000),
            )
            dispatch_issue(target, runtime.state, now_ms=runtime.clock_ms())
            complete_worker_terminal_failure(target.id, runtime.state)
            self.assertIn(target.id, runtime.state.claimed)
            # Issue was NOT in candidates last tick (operator moved it out and
            # back) — the intervention signal.
            runtime._prev_candidate_ids = set()

            with self.assertLogs("symphony.runtime", level="INFO") as logs:
                result = await runtime.run_tick()

            self.assertTrue(any("claim_released" in line for line in logs.output))
            self.assertEqual(("IN-200",), result.dispatched)

    async def test_approval_state_move_failure_schedules_retry(self):
        # Regression: if move_issue_to_state raises (Linear outage, etc.) the
        # handler used to log and release, leaving the issue in its original
        # active state with `_prev_candidate_ids` already containing it — the
        # daemon would never retry the move and never re-dispatch. The fix
        # schedules a normal retry so the next tick retries the move.
        with tempfile.TemporaryDirectory() as temp_dir:
            target = _team_issue()
            tracker = ClaimingFakeTracker([target])
            runner = FakeSessionRunner(success=False, exit_reason="approval_required")

            # Monkey-patch move to raise on the SECOND call (the first call is
            # the IN-290 claim move which we want to succeed).
            original_move = tracker.move_issue_to_state
            call_count = {"n": 0}

            def flaky_move(issue_id, team_id, state_name):
                call_count["n"] += 1
                if call_count["n"] >= 2:
                    raise RuntimeError("linear_outage")
                return original_move(issue_id, team_id, state_name)

            tracker.move_issue_to_state = flaky_move

            config = WorkflowConfig.from_mapping(
                {
                    "tracker": {
                        "kind": "linear",
                        "active_states": ["Todo"],
                        "terminal_states": ["Done"],
                        "in_progress_state": "In Progress",
                        "approval_state": "Needs Approval",
                    },
                    "workspace": {"root": str(Path(temp_dir) / "workspaces")},
                    "agent": {"max_concurrent_agents": 1},
                    "polling": {"interval_ms": 5_000},
                }
            )
            runtime = SymphonyRuntime(
                config=config,
                prompt_template="x",
                tracker=tracker,
                workspace_manager=FakeWorkspaceManager(Path(temp_dir) / "workspaces"),
                runner=runner,
                clock_ms=ManualClock(10_000),
            )

            result = await runtime.run_tick()

            self.assertEqual(("IN-501",), result.failed)
            self.assertEqual("approval_state_move_failed", result.errors["IN-501"])
            # Retry scheduled so the next tick re-attempts the move.
            self.assertIn(target.id, runtime.state.retry_attempts)


class ClaimingFakeTracker(FakeTracker):
    """FakeTracker that supports the IN-290 claim flow."""

    def __init__(
        self,
        candidates: list[Issue],
        *,
        states: list[Issue] | None = None,
        post_claim_state: str | None = None,
        post_claim_override_issue_id: str | None = None,
        post_claim_state_for_override: str | None = None,
        move_raises: Exception | None = None,
    ) -> None:
        super().__init__(candidates, states=states)
        self.post_claim_state = post_claim_state
        self.post_claim_override_issue_id = post_claim_override_issue_id
        self.post_claim_state_for_override = post_claim_state_for_override
        self.move_raises = move_raises
        self.move_calls: list[tuple[str, str | None, str]] = []

    def move_issue_to_state(self, issue_id: str, team_id: str | None, state_name: str) -> Issue:
        if self.move_raises is not None:
            raise self.move_raises
        self.move_calls.append((issue_id, team_id, state_name))
        # Apply the move to our local "states" view so subsequent fetches see it,
        # unless the test wants to simulate a competing instance changing state.
        source = self.candidates if self.states is None else self.states
        target = next((item for item in source if item.id == issue_id), None)
        if target is None:
            raise RuntimeError(f"unknown issue {issue_id}")
        applied_state = state_name
        if (
            self.post_claim_override_issue_id == issue_id
            and self.post_claim_state_for_override is not None
        ):
            applied_state = self.post_claim_state_for_override
        elif self.post_claim_state is not None:
            applied_state = self.post_claim_state
        updated = Issue(
            id=target.id,
            identifier=target.identifier,
            title=target.title,
            description=target.description,
            priority=target.priority,
            state=applied_state,
            branch_name=target.branch_name,
            url=target.url,
            labels=target.labels,
            blocked_by=target.blocked_by,
            team_id=target.team_id,
        )
        if self.states is None:
            self.candidates = [updated if item.id == issue_id else item for item in self.candidates]
        else:
            self.states = [updated if item.id == issue_id else item for item in self.states]
        return updated


def _claim_config(workspace_root: Path, *, in_progress_state: str = "In Progress", queued_state: str | None = None):
    return WorkflowConfig.from_mapping(
        {
            "tracker": {
                "kind": "linear",
                "active_states": ["Todo", "In Progress"],
                "terminal_states": ["Done", "Canceled"],
                "in_progress_state": in_progress_state,
                **({"queued_state": queued_state} if queued_state else {}),
            },
            "workspace": {"root": str(workspace_root)},
            "agent": {"max_concurrent_agents": 2},
            "polling": {"interval_ms": 5_000},
        }
    )


def _team_issue(*, state: str = "Todo", issue_id: str = "issue-claim", identifier: str = "IN-501") -> Issue:
    return Issue(
        id=issue_id,
        identifier=identifier,
        title=f"{identifier} title",
        description=None,
        priority=1,
        state=state,
        branch_name=None,
        url=None,
        team_id="team-1",
    )


class FailingWorkspaceManager(FakeWorkspaceManager):
    async def prepare_for_issue(self, target: Issue) -> FakeWorkspace:
        self.calls.append(("prepare", target.identifier))
        raise RuntimeError("workspace_blew_up")


class ClaimFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_claim_succeeds_and_workspace_prep_runs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = _team_issue()
            tracker = ClaimingFakeTracker([target])
            workspace_manager = FakeWorkspaceManager(Path(temp_dir) / "workspaces")
            runner = FakeSessionRunner()
            runtime = SymphonyRuntime(
                config=_claim_config(Path(temp_dir) / "workspaces"),
                prompt_template="work on {{ issue.identifier }}",
                tracker=tracker,
                workspace_manager=workspace_manager,
                runner=runner,
                clock_ms=ManualClock(10_000),
            )

            with self.assertLogs("symphony.runtime", level="INFO") as logs:
                result = await runtime.run_tick()

            self.assertEqual(("IN-501",), result.dispatched)
            self.assertEqual([(target.id, "team-1", "In Progress")], tracker.move_calls)
            self.assertTrue(any("claim_succeeded" in line for line in logs.output))
            # Workspace prep ran after the claim — verifies the order.
            self.assertIn(("prepare", "IN-501"), workspace_manager.calls)

    async def test_transient_claim_error_schedules_retry(self):
        # When `move_issue_to_state` raises (transient Linear 5xx, network
        # blip, …) the issue must remain eligible — otherwise it ends up in
        # `_prev_candidate_ids` as "already seen" but never on a retry queue
        # and gets stranded until it leaves and re-enters the candidate set.
        with tempfile.TemporaryDirectory() as temp_dir:
            target = _team_issue()
            tracker = ClaimingFakeTracker([target], move_raises=RuntimeError("linear_500"))
            workspace_manager = FakeWorkspaceManager(Path(temp_dir) / "workspaces")
            runtime = SymphonyRuntime(
                config=_claim_config(Path(temp_dir) / "workspaces"),
                prompt_template="x",
                tracker=tracker,
                workspace_manager=workspace_manager,
                runner=FakeSessionRunner(),
                clock_ms=ManualClock(10_000),
            )

            with self.assertLogs("symphony.runtime", level="WARNING") as logs:
                result = await runtime.run_tick()

            self.assertEqual(("IN-501",), result.failed)
            self.assertIn(target.id, runtime.state.retry_attempts)
            retry = runtime.state.retry_attempts[target.id]
            self.assertEqual(1, retry.attempt)
            self.assertIn("claim_error", retry.error or "")
            # Claim retains the slot for the rescheduled attempt.
            self.assertIn(target.id, runtime.state.claimed)
            # Workspace prep MUST NOT run when the claim fails.
            self.assertEqual([], workspace_manager.calls)
            self.assertTrue(any("claim_error" in line for line in logs.output))

    async def test_post_claim_verification_failure_aborts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = _team_issue()
            # Simulate a concurrent instance moving the ticket away after we
            # called updateIssue — the re-fetch shows it's no longer in "In
            # Progress" (PRD §8.5 step 3).
            tracker = ClaimingFakeTracker(
                [target],
                post_claim_override_issue_id=target.id,
                post_claim_state_for_override="Done",
            )
            workspace_manager = FakeWorkspaceManager(Path(temp_dir) / "workspaces")
            runtime = SymphonyRuntime(
                config=_claim_config(Path(temp_dir) / "workspaces"),
                prompt_template="x",
                tracker=tracker,
                workspace_manager=workspace_manager,
                runner=FakeSessionRunner(),
                clock_ms=ManualClock(10_000),
            )

            with self.assertLogs("symphony.runtime", level="WARNING") as logs:
                result = await runtime.run_tick()

            self.assertEqual(("IN-501",), result.failed)
            self.assertEqual([], workspace_manager.calls)
            self.assertTrue(any("claim_failed" in line for line in logs.output))
            self.assertTrue(any("post_claim_state_mismatch" in line for line in logs.output))

    async def test_workspace_failure_after_claim_rolls_back_when_queued_state_set(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = _team_issue()
            tracker = ClaimingFakeTracker([target])
            workspace_manager = FailingWorkspaceManager(Path(temp_dir) / "workspaces")
            runtime = SymphonyRuntime(
                config=_claim_config(Path(temp_dir) / "workspaces", queued_state="Todo"),
                prompt_template="x",
                tracker=tracker,
                workspace_manager=workspace_manager,
                runner=FakeSessionRunner(),
                clock_ms=ManualClock(10_000),
            )

            with self.assertLogs("symphony.runtime", level="WARNING") as logs:
                result = await runtime.run_tick()

            self.assertEqual(("IN-501",), result.failed)
            # Two move calls: forward to In Progress, then back to Todo.
            self.assertEqual(
                [(target.id, "team-1", "In Progress"), (target.id, "team-1", "Todo")],
                tracker.move_calls,
            )
            self.assertTrue(any("claim_rollback:" in line for line in logs.output))

    async def test_workspace_failure_after_claim_logs_abandoned_when_no_queued_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = _team_issue()
            tracker = ClaimingFakeTracker([target])
            workspace_manager = FailingWorkspaceManager(Path(temp_dir) / "workspaces")
            runtime = SymphonyRuntime(
                config=_claim_config(Path(temp_dir) / "workspaces"),  # queued_state unset
                prompt_template="x",
                tracker=tracker,
                workspace_manager=workspace_manager,
                runner=FakeSessionRunner(),
                clock_ms=ManualClock(10_000),
            )

            with self.assertLogs("symphony.runtime", level="WARNING") as logs:
                result = await runtime.run_tick()

            self.assertEqual(("IN-501",), result.failed)
            # No rollback call — only the original forward move.
            self.assertEqual([(target.id, "team-1", "In Progress")], tracker.move_calls)
            self.assertTrue(any("claim_abandoned:" in line for line in logs.output))
            # Regression: the outer retry handler must NOT enqueue a retry for
            # abandoned claims. With allow_claimed_retry=True the next tick
            # would otherwise re-dispatch the same ticket, potentially
            # duplicating an agent if a concurrent instance is running it.
            self.assertEqual({}, runtime.state.retry_attempts)
            self.assertNotIn(target.id, runtime.state.claimed)

    async def test_issues_already_in_in_progress_state_are_skipped(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            # The candidate is currently in "In Progress" — either claimed by
            # another instance or being worked on by a human.
            target = _team_issue(state="In Progress")
            tracker = ClaimingFakeTracker([target])
            runtime = SymphonyRuntime(
                config=_claim_config(Path(temp_dir) / "workspaces"),
                prompt_template="x",
                tracker=tracker,
                workspace_manager=FakeWorkspaceManager(Path(temp_dir) / "workspaces"),
                runner=FakeSessionRunner(),
                clock_ms=ManualClock(10_000),
            )

            result = await runtime.run_tick()

            self.assertEqual((), result.dispatched)
            self.assertEqual([], tracker.move_calls)

    async def test_legacy_mode_skips_claim_when_in_progress_state_unset(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = _team_issue()
            tracker = ClaimingFakeTracker([target])
            runtime = SymphonyRuntime(
                config=make_config(Path(temp_dir) / "workspaces"),  # no in_progress_state
                prompt_template="x",
                tracker=tracker,
                workspace_manager=FakeWorkspaceManager(Path(temp_dir) / "workspaces"),
                runner=FakeSessionRunner(),
                clock_ms=ManualClock(10_000),
            )

            result = await runtime.run_tick()

            self.assertEqual(("IN-501",), result.dispatched)
            self.assertEqual([], tracker.move_calls)


if __name__ == "__main__":
    unittest.main()
