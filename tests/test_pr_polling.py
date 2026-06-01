"""Tests for GitHub PR comment polling in SymphonyRuntime."""
from __future__ import annotations

import asyncio
import dataclasses
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from symphony.config import WorkflowConfig
from symphony.github.webhooks import PRClosedEvent
from symphony.runtime import SYMPHONY_BOT_MARKER, SymphonyRuntime
from symphony.tracker.models import Issue


def make_config(workspace_root: Path, max_pr_turns: int = 5) -> WorkflowConfig:
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


def make_issue(issue_id: str = "issue-1", identifier: str = "SYM-42") -> Issue:
    return Issue(
        id=issue_id,
        identifier=identifier,
        title="Add feature",
        description="Do the thing",
        priority=1,
        state="In Review",
        branch_name="feat/sym-42-add-feature",
        url=f"https://linear.app/x/issue/{identifier}",
    )


@dataclass(frozen=True)
class FakeWorkspace:
    path: Path
    workspace_key: str = "sym-42"
    run_id: str = "run1"
    branch_name: str | None = "feat/sym-42-add-feature-run1"
    run_log_path: Path | None = None
    created_now: bool = True


class FakeTracker:
    def __init__(self) -> None:
        self.state_updates: list[tuple[str, str]] = []
        # Issues the tracker is told to return when the runtime asks for a
        # specific state-name set — used by ``record_startup_open_prs`` to
        # rebuild PR tracking from the review-state cohort at startup.
        self.issues_by_state: dict[str, list[Issue]] = {}
        self.fetch_by_states_should_raise: bool = False

    async def fetch_candidate_issues(self) -> list[Issue]:
        return []

    async def fetch_issue_states_by_ids(self, ids: list[str]) -> list[Issue]:
        return []

    async def update_issue_state_by_name(self, issue_id: str, state_name: str) -> bool:
        self.state_updates.append((issue_id, state_name))
        return True

    async def fetch_issues_by_states(self, state_names: list[str]) -> list[Issue]:
        if self.fetch_by_states_should_raise:
            raise RuntimeError("tracker fetch failed")
        collected: list[Issue] = []
        for state in state_names:
            collected.extend(self.issues_by_state.get(state, []))
        return collected


class FakeWorkspaceManager:
    def __init__(self, tmp: Path) -> None:
        self._tmp = tmp

    async def prepare_for_pr_feedback(self, issue: Issue, branch: str, **_: object) -> FakeWorkspace:
        p = self._tmp / issue.identifier / "pr-run"
        p.mkdir(parents=True, exist_ok=True)
        return FakeWorkspace(path=p, branch_name=branch)

    async def prepare_for_issue(self, issue: Issue, **_: object) -> FakeWorkspace:
        p = self._tmp / issue.identifier / "run1"
        p.mkdir(parents=True, exist_ok=True)
        return FakeWorkspace(path=p)

    async def before_run(self, ws: FakeWorkspace) -> None:
        pass

    async def after_run(self, ws: FakeWorkspace) -> None:
        pass

    async def cleanup_for_run(self, ws: FakeWorkspace, **_: object) -> bool:
        return True

    async def cleanup(self, identifier: str, **_: object) -> bool:
        return True


class FakeRunner:
    def __init__(self, success: bool = True) -> None:
        self.success = success
        self.prompts: list[str] = []

    async def run_task(self, path: Path, prompt: str, issue: Issue, callback: object) -> object:
        self.prompts.append(prompt)

        class _Result:
            success = True
            exit_reason = None

        return _Result()


class FakeGitHubClient:
    """Controllable fake for GitHub API calls used in polling."""

    def __init__(self, owner: str = "org", repo: str = "repo") -> None:
        self.owner = owner
        self.repo = repo
        self.comments: list[tuple[int, str]] = []
        # State for the fake PR
        self._open_pr: dict[str, int] = {}          # branch → pr_number
        self._pr_data: dict[int, dict] = {}          # pr_number → {merged, state}
        self._review_comments: dict[int, list[dict]] = {}  # pr_number → comments
        self._issue_comments: dict[int, list[dict]] = {}
        self._reviews: dict[int, list[dict]] = {}
        self._bot_login: str = "bot-user"

    def add_open_pr(self, branch: str, pr_number: int, *, merged: bool = False, open: bool = True) -> None:
        if open:
            self._open_pr[branch] = pr_number
        pr_state = "open" if open else "closed"
        self._pr_data[pr_number] = {"number": pr_number, "state": pr_state, "merged": merged}
        if pr_number not in self._review_comments:
            self._review_comments[pr_number] = []
        if pr_number not in self._issue_comments:
            self._issue_comments[pr_number] = []
        if pr_number not in self._reviews:
            self._reviews[pr_number] = []

    def add_review_comment(self, pr_number: int, comment_id: int, author: str, body: str) -> None:
        self._review_comments.setdefault(pr_number, []).append(
            {"id": comment_id, "body": body, "user": {"login": author}}
        )

    def add_issue_comment(self, pr_number: int, comment_id: int, author: str, body: str) -> None:
        self._issue_comments.setdefault(pr_number, []).append(
            {"id": comment_id, "body": body, "user": {"login": author}}
        )

    def add_review(self, pr_number: int, review_id: int, reviewer: str, state: str, body: str = "") -> None:
        self._reviews.setdefault(pr_number, []).append(
            {"id": review_id, "state": state, "body": body, "user": {"login": reviewer}}
        )

    def close_pr(self, branch: str, pr_number: int, *, merged: bool) -> None:
        self._open_pr.pop(branch, None)
        self._pr_data[pr_number] = {"number": pr_number, "state": "closed", "merged": merged}

    # --- API methods called by runtime ---

    def post_pr_comment(self, pr_number: int, body: str) -> bool:
        self.comments.append((pr_number, body))
        return True

    def get_pr_diff(self, pr_number: int) -> str:
        return f"diff for PR #{pr_number}"

    def get_authenticated_login(self) -> str | None:
        return self._bot_login

    def find_open_pr_for_branch(self, branch: str) -> int | None:
        return self._open_pr.get(branch)

    def get_pr(self, pr_number: int) -> dict | None:
        return self._pr_data.get(pr_number)

    def list_pr_review_comments(self, pr_number: int) -> list[dict]:
        return list(self._review_comments.get(pr_number, []))

    def list_pr_issue_comments(self, pr_number: int) -> list[dict]:
        return list(self._issue_comments.get(pr_number, []))

    def list_pr_reviews(self, pr_number: int) -> list[dict]:
        return list(self._reviews.get(pr_number, []))


def make_runtime(
    tmp: Path, max_pr_turns: int = 5, runner_success: bool = True
) -> tuple[SymphonyRuntime, FakeTracker, FakeRunner, FakeGitHubClient]:
    config = make_config(tmp, max_pr_turns=max_pr_turns)
    tracker = FakeTracker()
    runner = FakeRunner(success=runner_success)
    github = FakeGitHubClient()
    runtime = SymphonyRuntime(
        config=config,
        tracker=tracker,
        workspace_manager=FakeWorkspaceManager(tmp),
        runner=runner,
        github_client=github,
    )
    return runtime, tracker, runner, github


# ---------------------------------------------------------------------------
# No-op when no github_client or no tracked branches
# ---------------------------------------------------------------------------

class TestPollGitHubPRFeedbackNoop(unittest.IsolatedAsyncioTestCase):
    async def test_no_client_does_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(Path(tmp))
            tracker = FakeTracker()
            runner = FakeRunner()
            runtime = SymphonyRuntime(
                config=config,
                tracker=tracker,
                workspace_manager=FakeWorkspaceManager(Path(tmp)),
                runner=runner,
                github_client=None,
            )
            runtime._branch_to_issue["some-branch"] = make_issue()
            # Should return without error
            await runtime.poll_github_pr_feedback()
            self.assertEqual(len(runner.prompts), 0)

    async def test_no_tracked_branches_does_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _, runner, _ = make_runtime(Path(tmp))
            await runtime.poll_github_pr_feedback()
            self.assertEqual(len(runner.prompts), 0)


# ---------------------------------------------------------------------------
# New comment triggers agent run
# ---------------------------------------------------------------------------

class TestPollNewComment(unittest.IsolatedAsyncioTestCase):
    async def test_new_review_comment_triggers_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _, runner, github = make_runtime(Path(tmp))
            issue = make_issue()
            branch = "feat/sym-42-run1"
            runtime._branch_to_issue[branch] = issue
            github.add_open_pr(branch, 7)
            github.add_review_comment(7, 101, "alice", "Please fix the naming")

            await runtime.poll_github_pr_feedback()

            self.assertEqual(len(runner.prompts), 1)
            self.assertIn("alice", runner.prompts[0])
            self.assertIn("Please fix the naming", runner.prompts[0])

    async def test_new_issue_comment_triggers_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _, runner, github = make_runtime(Path(tmp))
            issue = make_issue()
            branch = "feat/sym-42-run1"
            runtime._branch_to_issue[branch] = issue
            github.add_open_pr(branch, 7)
            github.add_issue_comment(7, 201, "bob", "Can you add tests?")

            await runtime.poll_github_pr_feedback()

            self.assertEqual(len(runner.prompts), 1)
            self.assertIn("bob", runner.prompts[0])
            self.assertIn("Can you add tests?", runner.prompts[0])

    async def test_changes_requested_review_triggers_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _, runner, github = make_runtime(Path(tmp))
            issue = make_issue()
            branch = "feat/sym-42-run1"
            runtime._branch_to_issue[branch] = issue
            github.add_open_pr(branch, 7)
            github.add_review(7, 301, "carol", "changes_requested", "Please refactor this")

            await runtime.poll_github_pr_feedback()

            self.assertEqual(len(runner.prompts), 1)
            self.assertIn("carol", runner.prompts[0])
            self.assertIn("requested changes", runner.prompts[0])

    async def test_approved_review_does_not_trigger_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _, runner, github = make_runtime(Path(tmp))
            issue = make_issue()
            branch = "feat/sym-42-run1"
            runtime._branch_to_issue[branch] = issue
            github.add_open_pr(branch, 7)
            github.add_review(7, 301, "carol", "approved", "LGTM")

            await runtime.poll_github_pr_feedback()

            self.assertEqual(len(runner.prompts), 0)

    async def test_no_pr_found_does_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _, runner, github = make_runtime(Path(tmp))
            issue = make_issue()
            branch = "feat/sym-42-run1"
            runtime._branch_to_issue[branch] = issue
            # No PR added — find_open_pr_for_branch returns None

            await runtime.poll_github_pr_feedback()

            self.assertEqual(len(runner.prompts), 0)


# ---------------------------------------------------------------------------
# Deduplication — seen comments not re-processed
# ---------------------------------------------------------------------------

class TestPollDeduplication(unittest.IsolatedAsyncioTestCase):
    async def test_comment_seen_only_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _, runner, github = make_runtime(Path(tmp))
            issue = make_issue()
            branch = "feat/sym-42-run1"
            runtime._branch_to_issue[branch] = issue
            github.add_open_pr(branch, 7)
            github.add_issue_comment(7, 201, "alice", "Fix this please")

            await runtime.poll_github_pr_feedback()
            await runtime.poll_github_pr_feedback()  # second poll — same comment

            self.assertEqual(len(runner.prompts), 1)

    async def test_new_comment_after_first_poll_triggers_second_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _, runner, github = make_runtime(Path(tmp))
            issue = make_issue()
            branch = "feat/sym-42-run1"
            runtime._branch_to_issue[branch] = issue
            github.add_open_pr(branch, 7)
            github.add_issue_comment(7, 201, "alice", "First comment")

            await runtime.poll_github_pr_feedback()
            self.assertEqual(len(runner.prompts), 1)

            github.add_issue_comment(7, 202, "bob", "Second comment")
            await runtime.poll_github_pr_feedback()
            self.assertEqual(len(runner.prompts), 2)


# ---------------------------------------------------------------------------
# Bot comment filtering
# ---------------------------------------------------------------------------

class TestBotCommentFiltering(unittest.IsolatedAsyncioTestCase):
    async def test_bot_own_comment_not_re_processed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _, runner, github = make_runtime(Path(tmp))
            issue = make_issue()
            branch = "feat/sym-42-run1"
            runtime._branch_to_issue[branch] = issue
            github.add_open_pr(branch, 7)
            # Bot posts its own comment (identified by the Symphony marker, not by login)
            github.add_issue_comment(7, 999, "any-login", f"{SYMPHONY_BOT_MARKER}\nI addressed your feedback")

            await runtime.poll_github_pr_feedback()

            self.assertEqual(len(runner.prompts), 0)


# ---------------------------------------------------------------------------
# Multiple comments in one poll → single agent run
# ---------------------------------------------------------------------------

class TestMultipleCommentsInOnePoll(unittest.IsolatedAsyncioTestCase):
    async def test_multiple_comments_trigger_single_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _, runner, github = make_runtime(Path(tmp))
            issue = make_issue()
            branch = "feat/sym-42-run1"
            runtime._branch_to_issue[branch] = issue
            github.add_open_pr(branch, 7)
            github.add_review_comment(7, 101, "alice", "Fix naming")
            github.add_issue_comment(7, 201, "bob", "Add tests")

            await runtime.poll_github_pr_feedback()

            self.assertEqual(len(runner.prompts), 1)
            self.assertIn("alice", runner.prompts[0])
            self.assertIn("bob", runner.prompts[0])


# ---------------------------------------------------------------------------
# PR closed detection via polling
# ---------------------------------------------------------------------------

class TestPRClosedDetection(unittest.IsolatedAsyncioTestCase):
    async def test_merged_pr_transitions_issue_to_done(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime, tracker, _, github = make_runtime(Path(tmp))
            issue = make_issue()
            branch = "feat/sym-42-run1"
            runtime._branch_to_issue[branch] = issue
            github.add_open_pr(branch, 7)
            # First poll: discovers PR, caches pr_number
            runtime._branch_pr_numbers[branch] = 7

            # Close the PR
            github.close_pr(branch, 7, merged=True)

            await runtime.poll_github_pr_feedback()

            self.assertEqual(tracker.state_updates, [(issue.id, "Done")])
            self.assertNotIn(branch, runtime._branch_to_issue)

    async def test_abandoned_pr_transitions_issue_to_canceled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime, tracker, _, github = make_runtime(Path(tmp))
            issue = make_issue()
            branch = "feat/sym-42-run1"
            runtime._branch_to_issue[branch] = issue
            runtime._branch_pr_numbers[branch] = 7
            github.add_open_pr(branch, 7)
            github.close_pr(branch, 7, merged=False)

            await runtime.poll_github_pr_feedback()

            self.assertEqual(tracker.state_updates, [(issue.id, "Canceled")])

    async def test_pr_number_cached_on_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _, runner, github = make_runtime(Path(tmp))
            issue = make_issue()
            branch = "feat/sym-42-run1"
            runtime._branch_to_issue[branch] = issue
            github.add_open_pr(branch, 99)

            self.assertNotIn(branch, runtime._branch_pr_numbers)
            await runtime.poll_github_pr_feedback()
            self.assertEqual(runtime._branch_pr_numbers[branch], 99)


# ---------------------------------------------------------------------------
# Acceptance bounce-back: judge fail → re-dispatch implementer
# ---------------------------------------------------------------------------


class TestAcceptanceBounceBack(unittest.IsolatedAsyncioTestCase):
    """When the acceptance judge says ``fail``, the runtime should funnel the
    verdict through ``_handle_pr_feedback`` so the implementer gets another
    turn (bounded by ``max_pr_turns``). ``uncertain`` and ``pass`` must not
    trigger bounce-back — a retry can't resolve judge doubt, and pass is
    obviously success."""

    def _run_with_verdict(self, tmp: Path, overall: str, *, bounce_back_on_fail: bool = True):
        """Spin up a runtime where ``maybe_run_acceptance`` is patched to
        return a synthetic verdict of the chosen overall. ``bounce_back_on_fail``
        is opt-in in production (default False); tests targeting the bounce
        loop pass True explicitly, the off-by-default behavior is exercised
        by ``test_default_off_does_not_bounce_on_fail`` below."""
        from unittest.mock import patch
        from datetime import datetime, timezone

        from symphony.acceptance import AcceptanceCheck, AcceptanceVerdict, ConvergenceResult

        runtime, _, runner, _ = make_runtime(tmp)
        # Enable the acceptance block on this config (default is disabled).
        runtime.config = dataclasses.replace(
            runtime.config,
            acceptance=dataclasses.replace(
                runtime.config.acceptance,
                enabled=True,
                bounce_back_on_fail=bounce_back_on_fail,
            ),
        )
        issue = make_issue()
        branch = "feat/sym-42-run1"
        runtime._branch_to_issue[branch] = issue

        verdict = AcceptanceVerdict(
            overall=overall,
            checks=(
                AcceptanceCheck(
                    requirement="widget must turn purple",
                    status="unmet" if overall != "pass" else "met",
                    evidence="Widget.tsx still emits blue",
                    confidence=0.9,
                ),
            ),
            confidence=0.9,
            summary_for_human="Widget is still blue, not purple.",
        )
        convergence = ConvergenceResult(True, "silent", "converged")
        snapshot = runtime._convergence_snapshots.get(branch)
        from symphony.acceptance_runtime import ConvergenceSnapshot
        fake_snapshot = ConvergenceSnapshot(
            last_feedback_at=datetime(2026, 5, 30, tzinfo=timezone.utc),
            pr_turns_observed=0,
        )

        async def fake_maybe_run_acceptance(*args, **kwargs):
            return (fake_snapshot, "head-abc", verdict, convergence)

        return runtime, runner, branch, fake_maybe_run_acceptance

    async def test_fail_verdict_bounces_back_to_implementer(self) -> None:
        """The contract: fail → ``_handle_pr_feedback`` runs → FakeRunner
        receives a prompt containing the unmet requirement."""
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            runtime, runner, branch, fake = self._run_with_verdict(Path(tmp), "fail")
            with patch("symphony.runtime.maybe_run_acceptance", side_effect=fake):
                await runtime._maybe_run_acceptance(branch, 7, runtime._branch_to_issue[branch], False)

            self.assertEqual(len(runner.prompts), 1, "fail verdict must dispatch the implementer once")
            prompt = runner.prompts[0]
            self.assertIn("widget must turn purple", prompt)
            self.assertIn("acceptance judge", prompt.lower())

    async def test_uncertain_verdict_escalates_without_bouncing(self) -> None:
        """``uncertain`` is the judge admitting it cannot tell — retrying
        the implementer would not resolve that. Must NOT bounce-back."""
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            runtime, runner, branch, fake = self._run_with_verdict(Path(tmp), "uncertain")
            with patch("symphony.runtime.maybe_run_acceptance", side_effect=fake):
                await runtime._maybe_run_acceptance(branch, 7, runtime._branch_to_issue[branch], False)

            self.assertEqual(runner.prompts, [])

    async def test_pass_verdict_does_not_bounce(self) -> None:
        """Pass is success — no bounce-back, obviously."""
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            runtime, runner, branch, fake = self._run_with_verdict(Path(tmp), "pass")
            with patch("symphony.runtime.maybe_run_acceptance", side_effect=fake):
                await runtime._maybe_run_acceptance(branch, 7, runtime._branch_to_issue[branch], False)

            self.assertEqual(runner.prompts, [])

    async def test_bounce_back_respects_max_pr_turns(self) -> None:
        """Once ``max_pr_turns`` is exhausted, bounce-back must stop. The
        runtime's existing ``_handle_pr_feedback`` enforces this; the test
        verifies the integration end-to-end: pre-set _pr_turns to the cap,
        verify no further dispatch and the standard max-turns comment."""
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            runtime, runner, branch, fake = self._run_with_verdict(Path(tmp), "fail")
            # Exhaust the budget so the next bounce attempt must be vetoed.
            runtime._pr_turns[branch] = runtime.config.github.max_pr_turns
            with patch("symphony.runtime.maybe_run_acceptance", side_effect=fake):
                await runtime._maybe_run_acceptance(branch, 7, runtime._branch_to_issue[branch], False)

            self.assertEqual(runner.prompts, [], "max_pr_turns must cap bounce-back")

    async def test_default_off_does_not_bounce_on_fail(self) -> None:
        """``bounce_back_on_fail`` defaults to False — fail verdicts must
        ONLY post the evaluation comment and wait for a human, NOT trigger
        a re-dispatch. This is the production-safe behavior for new
        rollouts; users who trust the verdict quality flip the flag on."""
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            runtime, runner, branch, fake = self._run_with_verdict(
                Path(tmp), "fail", bounce_back_on_fail=False,
            )
            with patch("symphony.runtime.maybe_run_acceptance", side_effect=fake):
                await runtime._maybe_run_acceptance(branch, 7, runtime._branch_to_issue[branch], False)

            self.assertEqual(runner.prompts, [], "default-off must skip the bounce")


# ---------------------------------------------------------------------------
# Startup recovery: rebuild branch→issue from review-state cohort
# ---------------------------------------------------------------------------


class TestRecordStartupOpenPRs(unittest.IsolatedAsyncioTestCase):
    """The PR-poll loop only follows branches stored in ``_branch_to_issue``.
    That map is process-local, so a daemon restart drops every PR that was
    opened in a previous session. ``record_startup_open_prs`` rebuilds the
    map from tracker-side review-state issues paired with their open PRs."""

    async def test_review_issue_with_open_pr_is_recovered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime, tracker, _, github = make_runtime(Path(tmp))
            issue = make_issue(issue_id="issue-recover", identifier="SYM-99")
            tracker.issues_by_state["In Review"] = [issue]
            github.add_open_pr(issue.branch_name, 42)

            await runtime.record_startup_open_prs()

            self.assertEqual(runtime._branch_to_issue.get(issue.branch_name), issue)
            self.assertEqual(runtime._branch_pr_numbers.get(issue.branch_name), 42)

    async def test_review_issue_with_no_open_pr_is_skipped(self) -> None:
        """Don't re-attach issues whose PR was already closed in the
        previous session — they have nothing the poller can act on."""
        with tempfile.TemporaryDirectory() as tmp:
            runtime, tracker, _, _ = make_runtime(Path(tmp))
            issue = make_issue(issue_id="issue-no-pr", identifier="SYM-100")
            tracker.issues_by_state["In Review"] = [issue]
            # No open PR added to github fake.

            await runtime.record_startup_open_prs()

            self.assertNotIn(issue.branch_name, runtime._branch_to_issue)
            self.assertNotIn(issue.branch_name, runtime._branch_pr_numbers)

    async def test_issue_without_branch_name_is_skipped(self) -> None:
        """Issues that never spawned a workspace branch (e.g. moved to
        review manually) cannot be paired with a PR — skip silently."""
        with tempfile.TemporaryDirectory() as tmp:
            runtime, tracker, _, _ = make_runtime(Path(tmp))
            issue = dataclasses.replace(
                make_issue(issue_id="issue-no-branch", identifier="SYM-101"),
                branch_name=None,
            )
            tracker.issues_by_state["In Review"] = [issue]

            await runtime.record_startup_open_prs()

            self.assertEqual(runtime._branch_to_issue, {})

    async def test_tracker_failure_does_not_crash_startup(self) -> None:
        """Startup recovery is best-effort — a tracker outage must not
        block the daemon from booting and serving the normal poll loop."""
        with tempfile.TemporaryDirectory() as tmp:
            runtime, tracker, _, _ = make_runtime(Path(tmp))
            tracker.fetch_by_states_should_raise = True

            # Must not raise.
            await runtime.record_startup_open_prs()
            self.assertEqual(runtime._branch_to_issue, {})

    async def test_no_github_client_is_safe(self) -> None:
        """No github client → no PRs to recover. Method must be a no-op
        rather than raise an AttributeError."""
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(Path(tmp))
            tracker = FakeTracker()
            runtime = SymphonyRuntime(
                config=config,
                tracker=tracker,
                workspace_manager=FakeWorkspaceManager(Path(tmp)),
                runner=FakeRunner(),
                github_client=None,
            )
            tracker.issues_by_state["In Review"] = [make_issue()]

            await runtime.record_startup_open_prs()
            self.assertEqual(runtime._branch_to_issue, {})
