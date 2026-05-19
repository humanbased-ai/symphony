import unittest
from datetime import datetime, timezone

from symphony.config import WorkflowConfig
from symphony.orchestrator import (
    CONTINUATION_RETRY_DELAY_MS,
    OrchestratorError,
    OrchestratorState,
    RetryEntry,
    complete_worker_failure,
    complete_worker_success,
    dispatch_issue,
    reconcile_refreshed_issues,
    retry_delay_ms,
    select_dispatchable,
    should_dispatch,
    stalled_issue_ids,
    unresolved_blockers,
)
from symphony.tracker.models import Blocker, Issue


def issue(
    issue_id: str,
    identifier: str,
    *,
    state: str = "Todo",
    priority: int | None = 1,
    created_at: datetime | None = None,
    blocked_by: tuple[Blocker, ...] = (),
) -> Issue:
    return Issue(
        id=issue_id,
        identifier=identifier,
        title=f"{identifier} title",
        description=None,
        priority=priority,
        state=state,
        branch_name=None,
        url=None,
        blocked_by=blocked_by,
        created_at=created_at,
    )


class OrchestratorTests(unittest.TestCase):
    def state(self) -> OrchestratorState:
        config = WorkflowConfig.from_mapping(
            {
                "tracker": {
                    "kind": "linear",
                    "active_states": ["Todo", "In Progress"],
                    "terminal_states": ["Done", "Canceled"],
                },
                "agent": {
                    "max_concurrent_agents": 2,
                    "max_concurrent_agents_by_state": {"Todo": 1},
                },
                "polling": {"interval_ms": 15_000},
            }
        )
        return OrchestratorState.from_config(config)

    def test_state_uses_workflow_config_limits(self):
        state = self.state()

        self.assertEqual(15_000, state.poll_interval_ms)
        self.assertEqual(2, state.max_concurrent_agents)
        self.assertEqual(1, state.available_slots_for_state("todo"))

    def test_dispatch_claims_issue_and_clears_retry(self):
        state = self.state()
        target = issue("issue-1", "IN-169")
        state.retry_attempts["issue-1"] = RetryEntry(
            issue_id="issue-1",
            identifier="IN-169",
            attempt=1,
            due_at_ms=500,
            error="previous failure",
        )

        entry = dispatch_issue(target, state, now_ms=100)

        self.assertIs(entry, state.running["issue-1"])
        self.assertIn("issue-1", state.claimed)
        self.assertNotIn("issue-1", state.retry_attempts)
        self.assertEqual(100, entry.started_at_ms)

    def test_retry_dispatch_can_use_existing_claim(self):
        state = self.state()
        target = issue("issue-1", "IN-169")
        state.claimed.add("issue-1")
        state.retry_attempts["issue-1"] = RetryEntry(
            issue_id="issue-1",
            identifier="IN-169",
            attempt=2,
            due_at_ms=500,
            error="previous failure",
        )

        entry = dispatch_issue(target, state, now_ms=1_000, attempt=2)

        self.assertEqual(2, entry.retry_attempt)
        self.assertIn("issue-1", state.claimed)
        self.assertNotIn("issue-1", state.retry_attempts)

    def test_candidate_selection_sorts_and_respects_state_limit(self):
        state = self.state()
        old = datetime(2026, 1, 1, tzinfo=timezone.utc)
        newer = datetime(2026, 1, 2, tzinfo=timezone.utc)
        issues = [
            issue("issue-3", "IN-003", state="In Progress", priority=None, created_at=newer),
            issue("issue-2", "IN-002", priority=2, created_at=old),
            issue("issue-1", "IN-001", priority=1, created_at=newer),
            issue("issue-4", "IN-004", priority=1, created_at=old),
        ]

        selected = select_dispatchable(issues, state)

        self.assertEqual(["IN-004", "IN-003"], [item.identifier for item in selected])
        self.assertEqual({}, state.running)
        self.assertEqual(set(), state.claimed)

    def test_todo_issue_with_open_blocker_is_not_dispatchable(self):
        state = self.state()
        blocked = issue(
            "issue-1",
            "IN-169",
            blocked_by=(Blocker(id="blocker-1", identifier="IN-100", state="In Progress"),),
        )

        self.assertFalse(should_dispatch(blocked, state))

    def test_completed_blocker_does_not_block_dispatch(self):
        state = self.state()
        unblocked = issue(
            "issue-1",
            "IN-169",
            blocked_by=(Blocker(id="blocker-1", identifier="IN-100", state="Done"),),
        )

        self.assertTrue(should_dispatch(unblocked, state))

    def test_in_progress_issue_with_open_blocker_is_also_filtered(self):
        # IN-287 expanded the blocker gate from Todo-only to all active states.
        state = self.state()
        blocked = issue(
            "issue-1",
            "IN-287",
            state="In Progress",
            blocked_by=(Blocker(id="blocker-1", identifier="IN-100", state="Todo"),),
        )

        self.assertFalse(should_dispatch(blocked, state))

    def test_worker_success_schedules_short_continuation_retry(self):
        state = self.state()
        dispatch_issue(issue("issue-1", "IN-169"), state, now_ms=100)

        retry = complete_worker_success("issue-1", state, now_ms=5_000)

        self.assertNotIn("issue-1", state.running)
        self.assertIn("issue-1", state.claimed)
        self.assertEqual(1, retry.attempt)
        self.assertEqual(5_000 + CONTINUATION_RETRY_DELAY_MS, retry.due_at_ms)
        self.assertIsNone(retry.error)

    def test_worker_failure_schedules_exponential_retry_from_attempt(self):
        state = self.state()
        dispatch_issue(issue("issue-1", "IN-169"), state, now_ms=100, attempt=2)

        retry = complete_worker_failure(
            "issue-1",
            state,
            now_ms=5_000,
            max_retry_backoff_ms=300_000,
            error="agent turn error",
        )

        self.assertEqual(3, retry.attempt)
        self.assertEqual(5_000 + 40_000, retry.due_at_ms)
        self.assertEqual("agent turn error", retry.error)

    def test_retry_delay_is_capped_and_requires_positive_attempt(self):
        self.assertEqual(300_000, retry_delay_ms(99, 300_000))

        with self.assertRaisesRegex(OrchestratorError, "retry_attempt_must_be_positive"):
            retry_delay_ms(0, 300_000)

    def test_stall_detection_uses_last_event_when_present(self):
        state = self.state()
        entry = dispatch_issue(issue("issue-1", "IN-169"), state, now_ms=100)
        entry.last_event_at_ms = 900

        self.assertEqual([], stalled_issue_ids(state, now_ms=1_200, stall_timeout_ms=500))
        self.assertEqual(["issue-1"], stalled_issue_ids(state, now_ms=1_401, stall_timeout_ms=500))

    def test_reconciliation_releases_terminal_and_non_active_runs(self):
        state = self.state()
        dispatch_issue(issue("issue-1", "IN-169", state="In Progress"), state, now_ms=100)
        dispatch_issue(issue("issue-2", "IN-170", state="In Progress"), state, now_ms=100)

        actions = reconcile_refreshed_issues(
            [
                issue("issue-1", "IN-169", state="Done"),
                issue("issue-2", "IN-170", state="Backlog"),
            ],
            state,
        )

        self.assertEqual(
            [("issue-1", True, "terminal_state"), ("issue-2", False, "non_active_state")],
            [(action.issue_id, action.cleanup_workspace, action.reason) for action in actions],
        )
        self.assertEqual({}, state.running)
        self.assertEqual(set(), state.claimed)


class BlockerGateTests(unittest.TestCase):
    def _state(self) -> OrchestratorState:
        config = WorkflowConfig.from_mapping(
            {
                "tracker": {
                    "kind": "linear",
                    "active_states": ["Todo", "In Progress"],
                    "terminal_states": ["Done", "Canceled"],
                },
                "agent": {"max_concurrent_agents": 2},
                "polling": {"interval_ms": 5_000},
            }
        )
        return OrchestratorState.from_config(config)

    def test_unresolved_blockers_returns_only_non_terminal(self):
        state = self._state()
        target = issue(
            "issue-1",
            "IN-100",
            blocked_by=(
                Blocker(id="b1", identifier="IN-1", state="Done"),
                Blocker(id="b2", identifier="IN-2", state="Todo"),
                Blocker(id="b3", identifier="IN-3", state="Canceled"),
            ),
        )

        blockers = unresolved_blockers(target, state)

        self.assertEqual(["IN-2"], [b.identifier for b in blockers])

    def test_blocked_issue_is_excluded_from_select_dispatchable(self):
        state = self._state()
        clean = issue("issue-1", "IN-001")
        blocked = issue(
            "issue-2",
            "IN-002",
            blocked_by=(Blocker(id="b1", identifier="IN-9", state="In Progress"),),
        )

        selected = select_dispatchable([clean, blocked], state)

        self.assertEqual(["issue-1"], [item.id for item in selected])


class ClaimGuardTests(unittest.TestCase):
    def _state(self, *, in_progress_state: str | None = "In Progress") -> OrchestratorState:
        config = WorkflowConfig.from_mapping(
            {
                "tracker": {
                    "kind": "linear",
                    "active_states": ["Todo", "In Progress"],
                    "terminal_states": ["Done", "Canceled"],
                    **(
                        {"in_progress_state": in_progress_state}
                        if in_progress_state is not None
                        else {}
                    ),
                },
                "agent": {"max_concurrent_agents": 2},
                "polling": {"interval_ms": 5_000},
            }
        )
        return OrchestratorState.from_config(config)

    def test_in_progress_issues_are_filtered_when_claim_guard_enabled(self):
        state = self._state()
        already_claimed = issue("issue-1", "IN-501", state="In Progress")
        queued = issue("issue-2", "IN-502", state="Todo")

        selected = select_dispatchable([already_claimed, queued], state)

        self.assertEqual(["issue-2"], [item.id for item in selected])
        self.assertFalse(should_dispatch(already_claimed, state))

    def test_in_progress_issues_pass_when_claim_guard_disabled(self):
        state = self._state(in_progress_state=None)
        already_active = issue("issue-1", "IN-501", state="In Progress")

        self.assertTrue(should_dispatch(already_active, state))

    def test_continuation_retry_for_claimed_issue_bypasses_in_progress_filter(self):
        # Regression: after a successful run Symphony's agent leaves the
        # Linear issue in `in_progress_state` and `complete_worker_success`
        # schedules a continuation `RetryEntry`. The IN-290 filter must not
        # reject the continuation retry when allow_claimed_retry=True;
        # otherwise the daemon never fires its own continuation path while
        # the issue stays in In Progress.
        state = self._state()
        target = issue("issue-1", "IN-290", state="In Progress")
        state.claimed.add("issue-1")
        state.retry_attempts["issue-1"] = RetryEntry(
            issue_id="issue-1",
            identifier="IN-290",
            attempt=1,
            due_at_ms=0,
            error=None,
        )

        self.assertTrue(should_dispatch(target, state, allow_claimed_retry=True))
        # And the same issue is still rejected without the retry permission.
        self.assertFalse(should_dispatch(target, state))


if __name__ == "__main__":
    unittest.main()
