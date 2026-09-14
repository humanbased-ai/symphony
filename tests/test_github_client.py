"""Unit tests for ``jazzband.github.client.GitHubClient`` methods.

The acceptance-gate end-to-end on PR #149 surfaced a real bug: when a check
(e.g. ``validate-pr-description``) was re-run in a new check_suite — caused
by the PR description being edited — GitHub's ``?filter=latest`` returned
the latest run **per suite**, not per check name. The old failure and the
new success both appeared, the failed-run filter caught the stale failure,
and Jazzband saw ``ci_green=False`` forever. Tests here lock in the fix.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from jazzband.github.client import GitHubClient, GitHubClientError


def _client() -> GitHubClient:
    return GitHubClient(token="tkn", owner="acme", repo="widget")


def _check_run(
    *,
    name: str,
    conclusion: str,
    run_id: int,
    started_at: str,
    details_url: str = "https://example/run",
    summary: str = "",
) -> dict:
    return {
        "id": run_id,
        "name": name,
        "conclusion": conclusion,
        "started_at": started_at,
        "details_url": details_url,
        "output": {"summary": summary},
    }


def _make_request_stub(runs: list[dict]):
    """Return a ``_request`` replacement that serves ``get_pr`` (so a head
    sha is available) and then the check-runs response. ``GitHubClient`` is
    a frozen dataclass, so the patch is at the class level — autospec
    threads ``self`` in correctly."""
    def stub(self, method, path, body=None):  # noqa: ARG001
        if "/pulls/" in path:
            return {"head": {"sha": "deadbeef"}}
        if "/check-runs" in path:
            return {"check_runs": runs}
        raise AssertionError(f"unexpected request: {method} {path}")
    return stub


class GetPRFailedCheckRunsTests(unittest.TestCase):
    """``get_pr_failed_check_runs`` must dedup by check name (keeping the
    latest run per name) so a successful re-run actually supersedes an
    earlier failure on the same commit. Without dedup, the silent acceptance
    branch never sees ``ci_green=True`` after a re-run."""

    def test_latest_successful_rerun_supersedes_earlier_failure(self):
        """The PR #149 scenario: same check name, two suites, old run
        failed, new run succeeded. ``get_pr_failed_check_runs`` must
        return an empty list — the failure was superseded."""
        runs = [
            _check_run(
                name="validate-pr-description",
                conclusion="failure",
                run_id=100,
                started_at="2026-06-01T04:45:01Z",
            ),
            _check_run(
                name="validate-pr-description",
                conclusion="success",
                run_id=200,
                started_at="2026-06-01T04:47:51Z",
            ),
            _check_run(
                name="make-all",
                conclusion="success",
                run_id=300,
                started_at="2026-06-01T04:45:01Z",
            ),
        ]
        with patch.object(GitHubClient, "_request", autospec=True, side_effect=_make_request_stub(runs)):
            self.assertEqual(_client().get_pr_failed_check_runs(1), [])

    def test_unique_failure_is_returned(self):
        """No re-runs — the one failure must come through unchanged so
        ``_handle_ci_failures`` can dispatch the implementer."""
        runs = [
            _check_run(
                name="make-all",
                conclusion="failure",
                run_id=42,
                started_at="2026-06-01T04:00:00Z",
                details_url="https://example/make-all",
                summary="Compilation failed",
            ),
            _check_run(
                name="validate-pr-description",
                conclusion="success",
                run_id=43,
                started_at="2026-06-01T04:00:01Z",
            ),
        ]
        with patch.object(GitHubClient, "_request", autospec=True, side_effect=_make_request_stub(runs)):
            failed = _client().get_pr_failed_check_runs(1)
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["id"], 42)
        self.assertEqual(failed[0]["name"], "make-all")
        self.assertEqual(failed[0]["summary"], "Compilation failed")

    def test_failure_rerun_replacing_earlier_success_is_returned(self):
        """Symmetric case: a later re-run that failed must supersede the
        earlier success. Otherwise a freshly broken check would be hidden
        behind the old green run and Jazzband would not dispatch a fix."""
        runs = [
            _check_run(
                name="lint",
                conclusion="success",
                run_id=10,
                started_at="2026-06-01T04:00:00Z",
            ),
            _check_run(
                name="lint",
                conclusion="failure",
                run_id=11,
                started_at="2026-06-01T05:00:00Z",
            ),
        ]
        with patch.object(GitHubClient, "_request", autospec=True, side_effect=_make_request_stub(runs)):
            failed = _client().get_pr_failed_check_runs(1)
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["id"], 11)

    def test_multiple_distinct_failures_all_returned(self):
        """Different check names, all currently failing — all should come
        through so ``_handle_ci_failures`` can name them in the dispatch
        comment."""
        runs = [
            _check_run(name="lint", conclusion="failure", run_id=1, started_at="2026-06-01T04:00:00Z"),
            _check_run(name="build", conclusion="failure", run_id=2, started_at="2026-06-01T04:00:00Z"),
            _check_run(name="test", conclusion="success", run_id=3, started_at="2026-06-01T04:00:00Z"),
        ]
        with patch.object(GitHubClient, "_request", autospec=True, side_effect=_make_request_stub(runs)):
            failed = _client().get_pr_failed_check_runs(1)
        names = {f["name"] for f in failed}
        self.assertEqual(names, {"lint", "build"})

    def test_pr_without_head_sha_returns_empty(self):
        """Defensive: a malformed PR response (no head sha) should yield an
        empty list rather than raise, so a transient API hiccup does not
        crash the runtime tick."""
        def stub(self, method, path, body=None):  # noqa: ARG001
            if "/pulls/" in path:
                return {"head": {}}
            raise AssertionError(f"unexpected request: {method} {path}")
        with patch.object(GitHubClient, "_request", autospec=True, side_effect=stub):
            self.assertEqual(_client().get_pr_failed_check_runs(1), [])

    def test_api_error_returns_empty(self):
        """A ``GitHubClientError`` (e.g. 5xx from GitHub) must not propagate
        — the runtime treats absence of failures as 'CI green so far' and
        will re-check on the next tick."""
        with patch.object(
            GitHubClient,
            "_request",
            autospec=True,
            side_effect=GitHubClientError("github_http_error:500"),
        ):
            self.assertEqual(_client().get_pr_failed_check_runs(1), [])


if __name__ == "__main__":
    unittest.main()
