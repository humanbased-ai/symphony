"""Tests for startup reconciliation of review-state issues.

Covers SymphonyRuntime.reconcile_review_state_issues, which rebuilds the
PR-merged -> done transition after a restart wipes the in-memory branch maps
(PRD §8.1). See the "Restart reconciliation of review-state issues" checklist
item in prd.md.
"""
from __future__ import annotations

import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from symphony.config import WorkflowConfig
from symphony.runtime import SymphonyRuntime
from symphony.tracker.models import Issue


def make_config(workspace_root: Path) -> WorkflowConfig:
    return WorkflowConfig.from_mapping(
        {
            "tracker": {
                "kind": "linear",
                "active_states": ["Todo", "In Progress"],
                "terminal_states": ["Done", "Canceled", "Duplicate"],
                "review_state": "In Review",
                "done_state": "Done",
                "cancelled_state": "Canceled",
            },
            "workspace": {"root": str(workspace_root)},
            "agent": {"max_concurrent_agents": 2},
            "polling": {"interval_ms": 5_000},
        }
    )


def make_issue(
    issue_id: str = "issue-1",
    identifier: str = "IN-489",
    *,
    branch_name: str | None = "kayl/in-489-frontier-registry-v2-664cc3b5",
    state: str = "In Review",
) -> Issue:
    return Issue(
        id=issue_id,
        identifier=identifier,
        title=f"{identifier} · do the thing",
        description="",
        priority=1,
        state=state,
        branch_name=branch_name,
        url=f"https://linear.app/inductive-network/issue/{identifier}/slug",
    )


@dataclass(frozen=True)
class FakeWorkspace:
    path: Path
    workspace_key: str = "in-489"
    run_id: str = "run1"
    branch_name: str | None = None
    run_log_path: Path | None = None
    created_now: bool = True


class FakeWorkspaceManager:
    def __init__(self, tmp: Path) -> None:
        self._tmp = tmp


class FakeRunner:
    async def run_task(self, *_: object, **__: object) -> object:  # pragma: no cover - unused here
        class _R:
            success = True
            exit_reason = None

        return _R()


class FakeTracker:
    """Tracker fake exposing only what reconciliation touches."""

    def __init__(self, review_issues: list[Issue]) -> None:
        self._review_issues = review_issues
        self.state_updates: list[tuple[str, str]] = []
        self.fetched_states: list[list[str]] = []

    async def fetch_candidate_issues(self) -> list[Issue]:
        return []

    async def fetch_issues_by_states(self, state_names: list[str]) -> list[Issue]:
        self.fetched_states.append(list(state_names))
        return list(self._review_issues)

    async def update_issue_state_by_name(self, issue_id: str, state_name: str) -> bool:
        self.state_updates.append((issue_id, state_name))
        return True


class FakeGitHubClient:
    def __init__(self, owner: str = "codatta", repo: str = "codatta-onchain-protocol") -> None:
        self.owner = owner
        self.repo = repo
        # identifier -> pr_number
        self._issue_pr: dict[str, int] = {}
        # pr_number -> pr_data dict
        self._pr_data: dict[int, dict] = {}

    def add_pr(
        self,
        identifier: str,
        pr_number: int,
        *,
        branch: str,
        state: str,
        merged: bool,
        body: str | None = None,
        title: str = "",
    ) -> None:
        self._issue_pr[identifier] = pr_number
        self._pr_data[pr_number] = {
            "number": pr_number,
            "state": state,
            "merged": merged,
            "head": {"ref": branch},
            "body": body if body is not None else f"Resolves https://linear.app/inductive-network/issue/{identifier}/slug",
            "title": title or f"{identifier} · do the thing",
        }

    # --- API surface used by reconcile_review_state_issues ---
    def find_pr_for_issue(self, issue_identifier: str) -> int | None:
        return self._issue_pr.get(issue_identifier)

    def get_pr(self, pr_number: int) -> dict | None:
        return self._pr_data.get(pr_number)


def make_runtime(
    tmp: Path, review_issues: list[Issue], *, with_github: bool = True
) -> tuple[SymphonyRuntime, FakeTracker, FakeGitHubClient | None]:
    tracker = FakeTracker(review_issues)
    github = FakeGitHubClient() if with_github else None
    runtime = SymphonyRuntime(
        config=make_config(tmp),
        tracker=tracker,
        workspace_manager=FakeWorkspaceManager(tmp),
        runner=FakeRunner(),
        github_client=github,
    )
    return runtime, tracker, github


class TestReconcileReviewState(unittest.IsolatedAsyncioTestCase):
    async def test_merged_pr_transitions_to_done(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            issue = make_issue()
            runtime, tracker, github = make_runtime(Path(tmp), [issue])
            github.add_pr(
                issue.identifier, 56,
                branch="kayl/in-489-frontier-registry-v2-664cc3b5",
                state="closed", merged=True,
            )

            await runtime.reconcile_review_state_issues()

            self.assertEqual(tracker.fetched_states, [["In Review"]])
            self.assertEqual(tracker.state_updates, [(issue.id, "Done")])
            # Merged PRs should not linger in the watch maps.
            self.assertEqual(runtime._branch_to_issue, {})
            self.assertEqual(runtime._branch_pr_numbers, {})

    async def test_open_pr_is_re_registered_for_polling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            issue = make_issue()
            runtime, tracker, github = make_runtime(Path(tmp), [issue])
            branch = "kayl/in-489-frontier-registry-v2-664cc3b5"
            github.add_pr(issue.identifier, 56, branch=branch, state="open", merged=False)

            await runtime.reconcile_review_state_issues()

            # No premature state change for an open PR.
            self.assertEqual(tracker.state_updates, [])
            # Watch resumes for the eventual merge.
            self.assertIs(runtime._branch_to_issue.get(branch), issue)
            self.assertEqual(runtime._branch_pr_numbers.get(branch), 56)

    async def test_closed_unmerged_pr_left_in_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            issue = make_issue()
            runtime, tracker, github = make_runtime(Path(tmp), [issue])
            github.add_pr(
                issue.identifier, 56,
                branch="kayl/in-489-frontier-registry-v2-664cc3b5",
                state="closed", merged=False,
            )

            await runtime.reconcile_review_state_issues()

            # Never auto-cancel from reconciliation.
            self.assertEqual(tracker.state_updates, [])
            self.assertEqual(runtime._branch_to_issue, {})

    async def test_no_pr_found_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            issue = make_issue()
            runtime, tracker, _ = make_runtime(Path(tmp), [issue])
            # No PR registered for the issue.
            await runtime.reconcile_review_state_issues()
            self.assertEqual(tracker.state_updates, [])

    async def test_unrelated_pr_is_skipped_by_reference_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            issue = make_issue(identifier="IN-489")
            runtime, tracker, github = make_runtime(Path(tmp), [issue])
            # Search returns a PR that does NOT reference this issue (substring
            # collision): different branch, body, title, and no matching URL.
            github.add_pr(
                issue.identifier, 999,
                branch="kayl/in-4890-some-other-issue",
                state="closed", merged=True,
                body="Resolves https://linear.app/inductive-network/issue/IN-4890/other",
                title="IN-4890 · unrelated",
            )

            await runtime.reconcile_review_state_issues()

            # Guard prevents marking the wrong issue Done.
            self.assertEqual(tracker.state_updates, [])

    async def test_no_github_client_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            issue = make_issue()
            runtime, tracker, _ = make_runtime(Path(tmp), [issue], with_github=False)
            await runtime.reconcile_review_state_issues()
            self.assertEqual(tracker.state_updates, [])
            self.assertEqual(tracker.fetched_states, [])


if __name__ == "__main__":
    unittest.main()
