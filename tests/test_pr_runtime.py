"""Tests for GitHub PR feedback handling in SymphonyRuntime."""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock, patch

from symphony.agents.base import AgentEvent, AgentEventType, AgentSession, TaskResult, TokenUsage, TurnResult
from symphony.config import WorkflowConfig
from symphony.github.webhooks import PRClosedEvent, PRCommentEvent, PRReviewEvent
from symphony.runtime import SymphonyRuntime
from symphony.tracker.models import Issue


def make_config(workspace_root: Path, max_pr_turns: int = 3) -> WorkflowConfig:
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
        branch_name="sym-42-add-feature",
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

    async def run_task(self, path: Path, prompt: str, issue: Issue, callback: object) -> TaskResult:
        self.prompts.append(prompt)
        return TaskResult(success=self.success, exit_reason=None if self.success else "error")


class FakeGitHubClient:
    def __init__(self) -> None:
        self.comments: list[tuple[int, str]] = []
        self.labels: dict[int, set[str]] = {}

    def post_pr_comment(self, pr_number: int, body: str) -> bool:
        self.comments.append((pr_number, body))
        return True

    def get_pr_diff(self, pr_number: int) -> str:
        return f"diff for PR #{pr_number}"

    # PR-lock label support (IN-628): a tiny in-memory model of the real client.
    def list_pr_labels(self, pr_number: int) -> list[str]:
        return sorted(self.labels.get(pr_number, set()))

    def add_pr_labels(self, pr_number: int, labels: list[str]) -> bool:
        self.labels.setdefault(pr_number, set()).update(labels)
        return True

    def remove_pr_label(self, pr_number: int, label: str) -> bool:
        self.labels.get(pr_number, set()).discard(label)
        return True


def make_runtime(tmp: Path, max_pr_turns: int = 3, runner_success: bool = True) -> tuple[SymphonyRuntime, FakeTracker, FakeRunner, FakeGitHubClient]:
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
# PR comment → agent run
# ---------------------------------------------------------------------------

class TestPRCommentTriggersAgent(unittest.IsolatedAsyncioTestCase):
    async def test_known_branch_triggers_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _, runner, _ = make_runtime(Path(tmp))
            issue = make_issue()
            branch = "feat/sym-42-add-feature-run1"
            runtime._branch_to_issue[branch] = issue

            event = PRCommentEvent(
                pr_number=5, pr_head_branch=branch, pr_head_sha="abc",
                comment_body="Please add error handling", comment_author="alice",
                comment_id=1, repo_owner="org", repo_name="repo",
            )
            await runtime.handle_github_pr_event(event)

            self.assertEqual(len(runner.prompts), 1)
            self.assertIn("alice", runner.prompts[0])
            self.assertIn("Please add error handling", runner.prompts[0])
            self.assertIn(str(5), runner.prompts[0])

    async def test_lock_label_set_during_run_and_released_after(self) -> None:
        # IN-628: a fix dispatch claims the PR with the lock label and releases it
        # when the agent run finishes, so the label is gone afterward.
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _, runner, github = make_runtime(Path(tmp))
            branch = "feat/sym-42-run1"
            runtime._branch_to_issue[branch] = make_issue()
            event = PRCommentEvent(
                pr_number=9, pr_head_branch=branch, pr_head_sha="abc",
                comment_body="fix it", comment_author="alice",
                comment_id=1, repo_owner="org", repo_name="repo",
            )
            await runtime.handle_github_pr_event(event)
            self.assertEqual(len(runner.prompts), 1)
            self.assertEqual(github.list_pr_labels(9), [])  # released

    async def test_lock_held_by_other_daemon_skips_dispatch(self) -> None:
        # IN-628: another daemon already holds the lock label → skip, don't dispatch,
        # and don't burn a turn (the holder owns the budget).
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _, runner, github = make_runtime(Path(tmp))
            branch = "feat/sym-42-run1"
            runtime._branch_to_issue[branch] = make_issue()
            github.add_pr_labels(9, [runtime.config.github.pr_lock_label])
            event = PRCommentEvent(
                pr_number=9, pr_head_branch=branch, pr_head_sha="abc",
                comment_body="fix it", comment_author="alice",
                comment_id=1, repo_owner="org", repo_name="repo",
            )
            await runtime.handle_github_pr_event(event)
            self.assertEqual(len(runner.prompts), 0)
            self.assertEqual(runtime._pr_turns.get(branch, 0), 0)

    async def test_escalated_branch_skips_dispatch(self) -> None:
        # IN-628: once a branch is escalated for oscillation, fix dispatch is paused.
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _, runner, _ = make_runtime(Path(tmp))
            branch = "feat/sym-42-run1"
            runtime._branch_to_issue[branch] = make_issue()
            runtime._pr_escalated.add(branch)
            event = PRCommentEvent(
                pr_number=9, pr_head_branch=branch, pr_head_sha="abc",
                comment_body="fix it", comment_author="alice",
                comment_id=1, repo_owner="org", repo_name="repo",
            )
            await runtime.handle_github_pr_event(event)
            self.assertEqual(len(runner.prompts), 0)

    async def test_unknown_branch_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _, runner, _ = make_runtime(Path(tmp))
            event = PRCommentEvent(
                pr_number=5, pr_head_branch="unknown-branch", pr_head_sha="abc",
                comment_body="fix this", comment_author="alice",
                comment_id=1, repo_owner="org", repo_name="repo",
            )
            await runtime.handle_github_pr_event(event)
            self.assertEqual(len(runner.prompts), 0)

    async def test_pr_diff_included_in_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _, runner, _ = make_runtime(Path(tmp))
            issue = make_issue()
            branch = "feat/sym-42-run1"
            runtime._branch_to_issue[branch] = issue

            event = PRCommentEvent(
                pr_number=7, pr_head_branch=branch, pr_head_sha="abc",
                comment_body="refactor this", comment_author="bob",
                comment_id=2, repo_owner="org", repo_name="repo",
            )
            await runtime.handle_github_pr_event(event)
            self.assertIn("diff for PR #7", runner.prompts[0])


# ---------------------------------------------------------------------------
# PR review (changes_requested) → agent run
# ---------------------------------------------------------------------------

class TestPRReviewChangesRequested(unittest.IsolatedAsyncioTestCase):
    async def test_changes_requested_triggers_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _, runner, _ = make_runtime(Path(tmp))
            issue = make_issue()
            branch = "feat/sym-42-run1"
            runtime._branch_to_issue[branch] = issue

            event = PRReviewEvent(
                pr_number=5, pr_head_branch=branch,
                review_state="changes_requested",
                review_body="Please restructure this function",
                reviewer="carol", repo_owner="org", repo_name="repo",
            )
            await runtime.handle_github_pr_event(event)

            self.assertEqual(len(runner.prompts), 1)
            self.assertIn("carol", runner.prompts[0])
            self.assertIn("Please restructure this function", runner.prompts[0])

    async def test_approved_review_does_not_trigger_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _, runner, _ = make_runtime(Path(tmp))
            issue = make_issue()
            branch = "feat/sym-42-run1"
            runtime._branch_to_issue[branch] = issue

            event = PRReviewEvent(
                pr_number=5, pr_head_branch=branch,
                review_state="approved",
                review_body="LGTM", reviewer="carol",
                repo_owner="org", repo_name="repo",
            )
            await runtime.handle_github_pr_event(event)
            self.assertEqual(len(runner.prompts), 0)


# ---------------------------------------------------------------------------
# Max PR turns enforcement
# ---------------------------------------------------------------------------

class TestMaxPRTurns(unittest.IsolatedAsyncioTestCase):
    async def test_exceeding_max_turns_skips_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _, runner, github = make_runtime(Path(tmp), max_pr_turns=2)
            issue = make_issue()
            branch = "feat/sym-42-run1"
            runtime._branch_to_issue[branch] = issue
            runtime._pr_turns[branch] = 2  # already at max

            event = PRCommentEvent(
                pr_number=5, pr_head_branch=branch, pr_head_sha="abc",
                comment_body="one more change please", comment_author="alice",
                comment_id=3, repo_owner="org", repo_name="repo",
            )
            await runtime.handle_github_pr_event(event)

            self.assertEqual(len(runner.prompts), 0)
            self.assertEqual(len(github.comments), 1)
            self.assertIn("Maximum", github.comments[0][1])

    async def test_turn_counter_increments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _, runner, _ = make_runtime(Path(tmp), max_pr_turns=5)
            issue = make_issue()
            branch = "feat/sym-42-run1"
            runtime._branch_to_issue[branch] = issue

            for _ in range(3):
                event = PRCommentEvent(
                    pr_number=5, pr_head_branch=branch, pr_head_sha="abc",
                    comment_body="fix this", comment_author="alice",
                    comment_id=1, repo_owner="org", repo_name="repo",
                )
                await runtime.handle_github_pr_event(event)

            self.assertEqual(runtime._pr_turns[branch], 3)
            self.assertEqual(len(runner.prompts), 3)


# ---------------------------------------------------------------------------
# PR closed → Linear state transition
# ---------------------------------------------------------------------------

class TestPRClosed(unittest.IsolatedAsyncioTestCase):
    async def test_merged_pr_transitions_to_done(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime, tracker, _, _ = make_runtime(Path(tmp))
            issue = make_issue()
            branch = "feat/sym-42-run1"
            runtime._branch_to_issue[branch] = issue

            event = PRClosedEvent(
                pr_number=5, pr_head_branch=branch, merged=True,
                repo_owner="org", repo_name="repo",
            )
            await runtime.handle_github_pr_event(event)

            self.assertEqual(tracker.state_updates, [(issue.id, "Done")])
            self.assertNotIn(branch, runtime._branch_to_issue)
            self.assertNotIn(branch, runtime._pr_turns)

    async def test_closed_without_merge_transitions_to_cancelled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime, tracker, _, _ = make_runtime(Path(tmp))
            issue = make_issue()
            branch = "feat/sym-42-run1"
            runtime._branch_to_issue[branch] = issue

            event = PRClosedEvent(
                pr_number=5, pr_head_branch=branch, merged=False,
                repo_owner="org", repo_name="repo",
            )
            await runtime.handle_github_pr_event(event)

            self.assertEqual(tracker.state_updates, [(issue.id, "Canceled")])

    async def test_unknown_branch_close_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime, tracker, _, _ = make_runtime(Path(tmp))
            event = PRClosedEvent(
                pr_number=5, pr_head_branch="unknown-branch", merged=True,
                repo_owner="org", repo_name="repo",
            )
            await runtime.handle_github_pr_event(event)
            self.assertEqual(tracker.state_updates, [])
