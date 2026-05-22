"""Tests for GitHub PR comment polling in SymphonyRuntime."""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from symphony.config import WorkflowConfig
from symphony.github.webhooks import PRClosedEvent
from symphony.runtime import SymphonyRuntime
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

    async def fetch_candidate_issues(self) -> list[Issue]:
        return []

    async def fetch_issue_states_by_ids(self, ids: list[str]) -> list[Issue]:
        return []

    async def update_issue_state_by_name(self, issue_id: str, state_name: str) -> bool:
        self.state_updates.append((issue_id, state_name))
        return True


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
            # Bot posts its own comment
            github.add_issue_comment(7, 999, github._bot_login, "I addressed your feedback")

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
