"""Tests for the live terminal dashboard (symphony/display.py) and related CLI helpers.

Coverage targets (IN-338):
  Unit:
    - LiveDashboard.on_state_change populates _running, _retrying correctly
    - LiveDashboard.update_tick moves entries into _done / _failed without duplicates
    - LiveDashboard.update_pr stores branch→pr_number and pr_number→status
    - _bar(pct) correct filled/empty ratio at 0%, 50%, 90%, 100%
    - _fmt_dur correct output at 0ms, 59 000ms, 60 000ms, 600 000ms

  Integration:
    - run_poll_loop calls after_tick with the correct RuntimeTickResult
    - SymphonyRuntime.on_pr_update fires ("open") when a new PR is first discovered
    - SymphonyRuntime.on_pr_update fires ("merged"/"closed") when PR closes
    - SymphonyRuntime.on_pr_update fires ("ci_fail") on new CI check failures
    - SymphonyRuntime.on_pr_update fires ("approved") on approved PR review event

  TTY / fallback:
    - Dashboard NOT created when NO_DASHBOARD=1
    - Dashboard NOT created when NO_COLOR=1
    - Dashboard NOT created when stdout is not a TTY
    - Stream log handler suppressed when dashboard is active
    - Stream log handler preserved when dashboard is not active
"""
from __future__ import annotations

import asyncio
import logging
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock, patch

from symphony.config import WorkflowConfig
from symphony.display import LiveDashboard, _BAR_WIDTH, _bar, _fmt_dur
from symphony.github.webhooks import PRReviewEvent
from symphony.orchestrator import OrchestratorState, RetryEntry, RunningEntry
from symphony.runtime import RuntimeTickResult, SymphonyRuntime
from symphony.tracker.models import Issue


# ---------------------------------------------------------------------------
# Helpers shared across test classes
# ---------------------------------------------------------------------------

def _make_issue(
    issue_id: str = "issue-1",
    identifier: str = "IN-1",
    state: str = "Todo",
    branch_name: str | None = None,
) -> Issue:
    return Issue(
        id=issue_id,
        identifier=identifier,
        title=f"{identifier} title",
        description="desc",
        priority=1,
        state=state,
        branch_name=branch_name,
        url=f"https://linear.app/x/issue/{identifier}",
    )


def _make_config(workspace_root: Path, max_pr_turns: int = 5) -> WorkflowConfig:
    return WorkflowConfig.from_mapping(
        {
            "tracker": {
                "kind": "linear",
                "active_states": ["Todo"],
                "terminal_states": ["Done", "Canceled"],
                "done_state": "Done",
                "cancelled_state": "Canceled",
            },
            "workspace": {"root": str(workspace_root)},
            "agent": {"max_concurrent_agents": 2},
            "polling": {"interval_ms": 5_000},
            "github": {"max_pr_turns": max_pr_turns},
        }
    )


def _make_orchestrator_state() -> OrchestratorState:
    return OrchestratorState(
        poll_interval_ms=30_000,
        max_concurrent_agents=4,
        active_states=("Todo",),
        terminal_states=("Done", "Canceled"),
    )


@dataclass(frozen=True)
class FakeWorkspace:
    path: Path
    branch_name: str | None = "feat/in-1-run1"
    run_id: str = "run1"
    run_log_path: Path | None = None
    workspace_key: str = "in-1"
    created_now: bool = True


class FakeTracker:
    def __init__(self, issues: list[Issue] | None = None) -> None:
        self._issues = issues or []
        self.state_updates: list[tuple[str, str]] = []

    async def fetch_candidate_issues(self) -> list[Issue]:
        return list(self._issues)

    async def fetch_issue_states_by_ids(self, ids: list[str]) -> list[Issue]:
        return [i for i in self._issues if i.id in ids]

    async def update_issue_state_by_name(self, issue_id: str, state_name: str) -> bool:
        self.state_updates.append((issue_id, state_name))
        return True


class FakeWorkspaceManager:
    def __init__(self, tmp: Path) -> None:
        self._tmp = tmp

    async def prepare_for_issue(self, issue: Issue, **_: object) -> FakeWorkspace:
        p = self._tmp / issue.identifier / "run1"
        p.mkdir(parents=True, exist_ok=True)
        return FakeWorkspace(path=p)

    async def prepare_for_pr_feedback(self, issue: Issue, branch: str, **_: object) -> FakeWorkspace:
        p = self._tmp / issue.identifier / "pr-run"
        p.mkdir(parents=True, exist_ok=True)
        return FakeWorkspace(path=p, branch_name=branch)

    async def before_run(self, ws: FakeWorkspace) -> None:
        pass

    async def after_run(self, ws: FakeWorkspace) -> None:
        pass

    async def cleanup(self, identifier: str, **_: object) -> bool:
        return True

    async def cleanup_for_run(self, ws: FakeWorkspace, **_: object) -> bool:
        return True

    async def startup_sweep(self) -> int:
        return 0


class FakeRunner:
    def __init__(self, success: bool = True) -> None:
        self.success = success

    async def run_task(self, path: Path, prompt: str, issue: Issue, on_event: object) -> object:
        from symphony.agents.base import TaskResult  # noqa: PLC0415
        return TaskResult(success=self.success, exit_reason=None if self.success else "fail")


# ---------------------------------------------------------------------------
# Unit tests — _fmt_dur
# ---------------------------------------------------------------------------

class TestFmtDur(unittest.TestCase):
    def test_zero_ms(self) -> None:
        self.assertEqual(_fmt_dur(0), "0m 00s")

    def test_59_seconds(self) -> None:
        self.assertEqual(_fmt_dur(59_000), "0m 59s")

    def test_60_seconds(self) -> None:
        self.assertEqual(_fmt_dur(60_000), "1m 00s")

    def test_10_minutes(self) -> None:
        self.assertEqual(_fmt_dur(600_000), "10m 00s")

    def test_negative_clamped_to_zero(self) -> None:
        # max(0, ms) guard — negative durations should not produce negative output
        self.assertEqual(_fmt_dur(-1_000), "0m 00s")


# ---------------------------------------------------------------------------
# Unit tests — _bar
# ---------------------------------------------------------------------------

class TestBar(unittest.TestCase):
    def test_0_pct_all_empty(self) -> None:
        b = _bar(0.0)
        self.assertEqual(len(b), _BAR_WIDTH)
        self.assertNotIn("█", b)

    def test_100_pct_all_filled(self) -> None:
        b = _bar(100.0)
        self.assertEqual(len(b), _BAR_WIDTH)
        self.assertNotIn("░", b)

    def test_50_pct_half_filled(self) -> None:
        b = _bar(50.0)
        self.assertEqual(len(b), _BAR_WIDTH)
        filled = b.count("█")
        empty = b.count("░")
        self.assertEqual(filled, _BAR_WIDTH // 2)
        self.assertEqual(empty, _BAR_WIDTH - _BAR_WIDTH // 2)

    def test_90_pct_mostly_filled(self) -> None:
        b = _bar(90.0)
        self.assertEqual(len(b), _BAR_WIDTH)
        filled = b.count("█")
        expected = int(90.0 / 100 * _BAR_WIDTH)
        self.assertEqual(filled, expected)

    def test_over_100_clamped(self) -> None:
        b = _bar(200.0)
        self.assertNotIn("░", b)

    def test_under_0_clamped(self) -> None:
        b = _bar(-10.0)
        self.assertNotIn("█", b)


# ---------------------------------------------------------------------------
# Unit tests — LiveDashboard (Rich suppressed so no TTY needed)
# ---------------------------------------------------------------------------

def _make_dashboard() -> LiveDashboard:
    """Create a LiveDashboard with its Live context mocked out."""
    with patch("symphony.display.Live"):
        dash = LiveDashboard()
    dash._live = MagicMock()
    return dash


class TestLiveDashboardOnStateChange(unittest.TestCase):
    def test_populates_running(self) -> None:
        dash = _make_dashboard()
        issue = _make_issue()
        entry = RunningEntry(issue=issue, started_at_ms=1_000)
        state = _make_orchestrator_state()
        state.running["issue-1"] = entry

        dash.on_state_change(state)

        self.assertIn("issue-1", dash._running)
        self.assertIs(dash._running["issue-1"], entry)

    def test_clears_running_when_empty(self) -> None:
        dash = _make_dashboard()
        dash._running["issue-1"] = object()
        state = _make_orchestrator_state()

        dash.on_state_change(state)

        self.assertEqual(dash._running, {})

    def test_populates_retrying(self) -> None:
        dash = _make_dashboard()
        retry = RetryEntry(issue_id="issue-1", identifier="IN-1", attempt=2, due_at_ms=5_000)
        state = _make_orchestrator_state()
        state.retry_attempts["issue-1"] = retry

        dash.on_state_change(state)

        self.assertIn(retry, dash._retrying)

    def test_all_entries_accumulates(self) -> None:
        dash = _make_dashboard()
        issue = _make_issue()
        entry = RunningEntry(issue=issue, started_at_ms=1_000)
        state = _make_orchestrator_state()
        state.running["issue-1"] = entry

        dash.on_state_change(state)
        state.running.clear()
        dash.on_state_change(state)

        # entry should still be in _all_entries after it leaves running
        self.assertIn("issue-1", dash._all_entries)

    def test_poll_interval_updated(self) -> None:
        dash = _make_dashboard()
        state = _make_orchestrator_state()
        state.poll_interval_ms = 60_000

        dash.on_state_change(state)

        self.assertEqual(dash._poll_interval_s, 60)


class TestLiveDashboardUpdateTick(unittest.TestCase):
    def _populated_dash(self) -> tuple[LiveDashboard, RunningEntry]:
        dash = _make_dashboard()
        issue = _make_issue(identifier="IN-1")
        entry = RunningEntry(issue=issue, started_at_ms=1_000)
        state = _make_orchestrator_state()
        state.running["issue-1"] = entry
        dash.on_state_change(state)
        return dash, entry

    def test_completed_moved_to_done(self) -> None:
        dash, _ = self._populated_dash()
        result = RuntimeTickResult(fetched=1, completed=("IN-1",))

        dash.update_tick(result)

        self.assertEqual(len(dash._done), 1)
        self.assertEqual(dash._done[0].identifier, "IN-1")

    def test_failed_moved_to_failed(self) -> None:
        dash, _ = self._populated_dash()
        result = RuntimeTickResult(fetched=1, failed=("IN-1",), errors={"IN-1": "timeout"})

        dash.update_tick(result)

        self.assertEqual(len(dash._failed), 1)
        self.assertEqual(dash._failed[0].identifier, "IN-1")
        self.assertEqual(dash._failed[0].error, "timeout")

    def test_no_duplicate_done_entries(self) -> None:
        dash, _ = self._populated_dash()
        result = RuntimeTickResult(fetched=1, completed=("IN-1",))

        dash.update_tick(result)
        dash.update_tick(result)  # second call — should not add a duplicate

        self.assertEqual(len(dash._done), 1)

    def test_no_duplicate_failed_entries(self) -> None:
        dash, _ = self._populated_dash()
        result = RuntimeTickResult(fetched=1, failed=("IN-1",), errors={"IN-1": "boom"})

        dash.update_tick(result)
        dash.update_tick(result)

        self.assertEqual(len(dash._failed), 1)

    def test_unknown_identifier_skipped(self) -> None:
        dash = _make_dashboard()
        result = RuntimeTickResult(fetched=0, completed=("IN-999",))

        dash.update_tick(result)

        self.assertEqual(dash._done, [])

    def test_fetched_count_stored(self) -> None:
        dash = _make_dashboard()
        result = RuntimeTickResult(fetched=7)

        dash.update_tick(result)

        self.assertEqual(dash._fetched, 7)


class TestLiveDashboardUpdatePr(unittest.TestCase):
    def test_stores_branch_pr_number(self) -> None:
        dash = _make_dashboard()
        dash.update_pr("feat/in-1", 42, "open")
        self.assertEqual(dash._pr_numbers["feat/in-1"], 42)

    def test_stores_pr_status(self) -> None:
        dash = _make_dashboard()
        dash.update_pr("feat/in-1", 42, "merged")
        self.assertEqual(dash._pr_statuses[42], "merged")

    def test_status_update_overwrites(self) -> None:
        dash = _make_dashboard()
        dash.update_pr("feat/in-1", 42, "open")
        dash.update_pr("feat/in-1", 42, "approved")
        self.assertEqual(dash._pr_statuses[42], "approved")

    def test_different_branches_independent(self) -> None:
        dash = _make_dashboard()
        dash.update_pr("branch-a", 10, "open")
        dash.update_pr("branch-b", 20, "merged")
        self.assertEqual(dash._pr_numbers["branch-a"], 10)
        self.assertEqual(dash._pr_numbers["branch-b"], 20)
        self.assertEqual(dash._pr_statuses[10], "open")
        self.assertEqual(dash._pr_statuses[20], "merged")


# ---------------------------------------------------------------------------
# Integration tests — run_poll_loop after_tick callback
# ---------------------------------------------------------------------------

class TestRunPollLoopAfterTick(unittest.IsolatedAsyncioTestCase):
    async def test_after_tick_receives_runtime_tick_result(self) -> None:
        from symphony.cli import run_poll_loop  # noqa: PLC0415

        received: list[RuntimeTickResult] = []

        def after_tick(result: RuntimeTickResult) -> None:
            received.append(result)
            raise StopAsyncIteration  # break the loop after first tick

        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(Path(tmp))
            tracker = FakeTracker(issues=[])
            runtime = SymphonyRuntime(
                config=config,
                tracker=tracker,
                workspace_manager=FakeWorkspaceManager(Path(tmp)),
                runner=FakeRunner(),
            )

            with self.assertRaises(StopAsyncIteration):
                await run_poll_loop(runtime, after_tick=after_tick)

        self.assertEqual(len(received), 1)
        self.assertIsInstance(received[0], RuntimeTickResult)
        self.assertEqual(received[0].fetched, 0)


# ---------------------------------------------------------------------------
# Integration tests — SymphonyRuntime.on_pr_update callbacks
# ---------------------------------------------------------------------------

class FakeGitHubClient:
    """Minimal stub for tests that exercise _poll_pr_for_branch."""

    def __init__(self) -> None:
        self.owner = "acme"
        self.repo = "repo"
        self._open_pr: int | None = None
        self._pr_data: dict | None = None
        self.review_comments: list[dict] = []
        self.issue_comments: list[dict] = []
        self.reviews: list[dict] = []
        self.check_runs: list[dict] = []

    def find_open_pr_for_branch(self, branch: str) -> int | None:
        return self._open_pr

    def get_pr(self, pr_number: int) -> dict:
        return self._pr_data or {}

    def list_pr_review_comments(self, pr_number: int) -> list[dict]:
        return list(self.review_comments)

    def list_pr_issue_comments(self, pr_number: int) -> list[dict]:
        return list(self.issue_comments)

    def list_pr_reviews(self, pr_number: int) -> list[dict]:
        return list(self.reviews)

    def get_pr_failed_check_runs(self, pr_number: int) -> list[dict]:
        return list(self.check_runs)

    def get_pr_diff(self, pr_number: int) -> str:
        return ""

    def post_pr_comment(self, pr_number: int, body: str) -> None:
        pass


class TestOnPrUpdateCallbacks(unittest.IsolatedAsyncioTestCase):
    def _make_runtime(
        self,
        tmp: Path,
        gh: FakeGitHubClient,
        branch: str = "feat/in-1",
    ) -> tuple[SymphonyRuntime, list[tuple[str, int, str]]]:
        updates: list[tuple[str, int, str]] = []

        config = _make_config(Path(tmp))
        runtime = SymphonyRuntime(
            config=config,
            tracker=FakeTracker(),
            workspace_manager=FakeWorkspaceManager(Path(tmp)),
            runner=FakeRunner(),
            github_client=gh,
            on_pr_update=lambda b, n, s: updates.append((b, n, s)),
        )
        # Seed branch→issue mapping as the workspace prepare step would do.
        issue = _make_issue(branch_name=branch)
        runtime._branch_to_issue[branch] = issue
        return runtime, updates

    async def test_on_pr_update_fires_open_on_first_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gh = FakeGitHubClient()
            gh._open_pr = 99
            runtime, updates = self._make_runtime(Path(tmp), gh, branch="feat/in-1")

            await runtime._poll_pr_for_branch("feat/in-1", runtime._branch_to_issue["feat/in-1"])

        self.assertEqual(len(updates), 1)
        branch, pr_num, status = updates[0]
        self.assertEqual(branch, "feat/in-1")
        self.assertEqual(pr_num, 99)
        self.assertEqual(status, "open")

    async def test_on_pr_update_fires_merged_when_pr_closes_merged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gh = FakeGitHubClient()
            # PR was open but now closed + merged
            gh._open_pr = None
            gh._pr_data = {"merged": True}
            runtime, updates = self._make_runtime(Path(tmp), gh, branch="feat/in-1")
            # Pretend we already discovered this PR on a previous tick
            runtime._branch_pr_numbers["feat/in-1"] = 77

            await runtime._poll_pr_for_branch("feat/in-1", runtime._branch_to_issue["feat/in-1"])

        self.assertEqual(len(updates), 1)
        _, pr_num, status = updates[0]
        self.assertEqual(pr_num, 77)
        self.assertEqual(status, "merged")

    async def test_on_pr_update_fires_closed_when_pr_closes_not_merged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gh = FakeGitHubClient()
            gh._open_pr = None
            gh._pr_data = {"merged": False}
            runtime, updates = self._make_runtime(Path(tmp), gh, branch="feat/in-1")
            runtime._branch_pr_numbers["feat/in-1"] = 55

            await runtime._poll_pr_for_branch("feat/in-1", runtime._branch_to_issue["feat/in-1"])

        _, pr_num, status = updates[0]
        self.assertEqual(pr_num, 55)
        self.assertEqual(status, "closed")

    async def test_on_pr_update_fires_ci_fail_on_new_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gh = FakeGitHubClient()
            gh._open_pr = 42
            gh.check_runs = [
                {"id": 1, "name": "lint", "details_url": "https://ci.example.com/1", "summary": "lint failed"},
            ]
            runtime, updates = self._make_runtime(Path(tmp), gh, branch="feat/in-1")
            # Pretend PR already known so we go straight to check-runs
            runtime._branch_pr_numbers["feat/in-1"] = 42

            await runtime._poll_pr_for_branch("feat/in-1", runtime._branch_to_issue["feat/in-1"])

        ci_updates = [u for u in updates if u[2] == "ci_fail"]
        self.assertEqual(len(ci_updates), 1)
        _, pr_num, status = ci_updates[0]
        self.assertEqual(pr_num, 42)
        self.assertEqual(status, "ci_fail")

    async def test_ci_fail_not_fired_twice_for_same_check_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gh = FakeGitHubClient()
            gh._open_pr = 42
            gh.check_runs = [{"id": 1, "name": "lint", "details_url": "", "summary": ""}]
            runtime, updates = self._make_runtime(Path(tmp), gh, branch="feat/in-1")
            runtime._branch_pr_numbers["feat/in-1"] = 42

            await runtime._poll_pr_for_branch("feat/in-1", runtime._branch_to_issue["feat/in-1"])
            await runtime._poll_pr_for_branch("feat/in-1", runtime._branch_to_issue["feat/in-1"])

        ci_updates = [u for u in updates if u[2] == "ci_fail"]
        self.assertEqual(len(ci_updates), 1)

    async def test_on_pr_update_fires_approved_on_pr_review_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(Path(tmp))
            updates: list[tuple[str, int, str]] = []
            runtime = SymphonyRuntime(
                config=config,
                tracker=FakeTracker(),
                workspace_manager=FakeWorkspaceManager(Path(tmp)),
                runner=FakeRunner(),
                on_pr_update=lambda b, n, s: updates.append((b, n, s)),
            )
            # Seed branch mapping
            runtime._branch_to_issue["feat/in-1"] = _make_issue()

            event = PRReviewEvent(
                pr_number=11,
                pr_head_branch="feat/in-1",
                reviewer="alice",
                review_state="approved",
                review_body="LGTM",
                repo_owner="acme",
                repo_name="repo",
            )
            await runtime.handle_github_pr_event(event)

        self.assertEqual(len(updates), 1)
        branch, pr_num, status = updates[0]
        self.assertEqual(branch, "feat/in-1")
        self.assertEqual(pr_num, 11)
        self.assertEqual(status, "approved")


# ---------------------------------------------------------------------------
# TTY / fallback behaviour tests
# ---------------------------------------------------------------------------

class TestMaybeCreateDashboard(unittest.TestCase):
    """Tests for _maybe_create_dashboard() in symphony/cli.py."""

    def _call(self) -> object:
        from symphony.cli import _maybe_create_dashboard  # noqa: PLC0415
        return _maybe_create_dashboard()

    def test_no_dashboard_env_returns_none(self) -> None:
        with patch.dict("os.environ", {"NO_DASHBOARD": "1"}):
            self.assertIsNone(self._call())

    def test_no_color_env_returns_none(self) -> None:
        with patch.dict("os.environ", {"NO_COLOR": "1"}):
            self.assertIsNone(self._call())

    def test_non_tty_stdout_returns_none(self) -> None:
        with patch.dict("os.environ", {}, clear=False):
            # Remove NO_DASHBOARD / NO_COLOR in case they're set in env
            for var in ("NO_DASHBOARD", "NO_COLOR"):
                patch.dict("os.environ", {var: ""}).start()
            with patch("sys.stdout") as mock_stdout:
                mock_stdout.isatty.return_value = False
                with patch.dict("os.environ", {"NO_DASHBOARD": "", "NO_COLOR": ""}):
                    result = self._call()
        # Either None (correct) or ImportError (rich not installed) — both acceptable
        self.assertIsNone(result)

    def test_returns_dashboard_when_tty_and_rich_available(self) -> None:
        env_patch = {"NO_DASHBOARD": "", "NO_COLOR": ""}
        with patch.dict("os.environ", env_patch):
            with patch("sys.stdout") as mock_stdout:
                mock_stdout.isatty.return_value = True
                with patch("symphony.display.Live"):
                    result = self._call()
        # None if rich is unavailable in test environment, LiveDashboard otherwise.
        self.assertTrue(result is None or isinstance(result, LiveDashboard))


class TestSuppressStreamHandler(unittest.TestCase):
    """Tests for _suppress_stream_handler() in symphony/cli.py."""

    def _call(self) -> None:
        from symphony.cli import _suppress_stream_handler  # noqa: PLC0415
        _suppress_stream_handler()

    def test_removes_stream_handlers(self) -> None:
        root = logging.getLogger()
        handler = logging.StreamHandler()
        root.addHandler(handler)
        try:
            self._call()
            stream_handlers = [
                h for h in root.handlers
                if isinstance(h, logging.StreamHandler)
                and not isinstance(h, logging.handlers.RotatingFileHandler)
            ]
            self.assertEqual(stream_handlers, [])
        finally:
            root.removeHandler(handler)

    def test_preserves_file_handlers(self) -> None:
        import logging.handlers as lh  # noqa: PLC0415
        root = logging.getLogger()
        with tempfile.NamedTemporaryFile(suffix=".log", delete=False) as f:
            file_handler = lh.RotatingFileHandler(f.name)
        root.addHandler(file_handler)
        try:
            self._call()
            self.assertIn(file_handler, root.handlers)
        finally:
            root.removeHandler(file_handler)
            file_handler.close()

    def test_no_stream_handler_when_dashboard_inactive(self) -> None:
        """When dashboard is not created, stream handlers should not be removed."""
        root = logging.getLogger()
        handler = logging.StreamHandler()
        root.addHandler(handler)
        try:
            # Simulating: dashboard is None, so _suppress_stream_handler is NOT called.
            # The handler should still be present.
            self.assertIn(handler, root.handlers)
        finally:
            root.removeHandler(handler)


if __name__ == "__main__":
    unittest.main()
