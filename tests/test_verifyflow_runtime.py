"""Tests for the VerifyFlow advisory step (IN-569).

Covers the config block and the decision core: run once per head SHA, only
after a fresh Crosscheck APPROVE, advisory whatever the outcome.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from symphony.config import VerifyflowConfig
from symphony.verifyflow_runtime import maybe_run_verifyflow


# --- config -----------------------------------------------------------------


def test_verifyflow_config_disabled_by_default() -> None:
    cfg = VerifyflowConfig.from_mapping({})
    assert cfg.enabled is False
    assert cfg.command == "vf"
    assert cfg.level == "functional"
    assert cfg.timeout_seconds == 900


def test_verifyflow_config_from_mapping() -> None:
    cfg = VerifyflowConfig.from_mapping(
        {"verifyflow": {"enabled": True, "command": "/opt/vf", "level": "ui", "timeout_seconds": 60}}
    )
    assert cfg.enabled is True
    assert cfg.command == "/opt/vf"
    assert cfg.level == "ui"
    assert cfg.timeout_seconds == 60


# --- decision core ----------------------------------------------------------


HEAD_SHA = "abc123def4567890"
PR_URL = "https://github.com/owner/repo/pull/42"

CROSSCHECK_APPROVE = {
    "body": "[crosscheck] Code Review\n\nLooks good.\n\nVERDICT: APPROVE",
    "created_at": "2026-06-03T08:00:00Z",
}
CROSSCHECK_NEEDS_WORK = {
    "body": "[crosscheck] Code Review\n\nIssues found.\n\nVERDICT: NEEDS WORK",
    "created_at": "2026-06-03T08:00:00Z",
}


class FakeGitHubClient:
    def __init__(
        self,
        pr_data: dict[str, Any] | None,
        comments: list[dict[str, Any]],
        failed_check_runs: list[dict[str, Any]] | None = None,
    ):
        self._pr_data = pr_data
        self._comments = comments
        self._failed_check_runs = failed_check_runs or []

    def get_pr(self, pr_number: int) -> dict[str, Any] | None:
        return self._pr_data

    def list_pr_issue_comments(self, pr_number: int) -> list[dict[str, Any]]:
        return self._comments

    def get_pr_failed_check_runs(self, pr_number: int) -> list[dict[str, Any]]:
        return self._failed_check_runs


def make_spawn(exit_code: int | None, summary: dict[str, Any] | None):
    calls: list[list[str]] = []

    async def spawn(cmd: list[str], timeout_seconds: int) -> tuple[int | None, str]:
        calls.append(cmd)
        stdout = json.dumps(summary) + "\n" if summary is not None else ""
        return exit_code, stdout

    return spawn, calls


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def _config(**over: Any) -> VerifyflowConfig:
    return VerifyflowConfig(enabled=True, **over)


def test_runs_vf_step_after_crosscheck_approve() -> None:
    gh = FakeGitHubClient(
        {"head": {"sha": HEAD_SHA}, "html_url": PR_URL}, [CROSSCHECK_APPROVE]
    )
    summary = {"verdict": "accept", "criteria": {"pass": 2}, "prCommentPosted": True}
    spawn, calls = make_spawn(0, summary)

    outcome = run(
        maybe_run_verifyflow(
            github_client=gh, config=_config(), branch="b", pr_number=42,
            already_run_sha=None, spawn=spawn,
        )
    )

    assert outcome is not None
    assert outcome.head_sha == HEAD_SHA
    assert outcome.exit_code == 0
    assert outcome.summary == summary
    assert calls == [
        ["vf", "step", "--pr", PR_URL, "--level", "functional", "--crosscheck-verdict", "APPROVE"]
    ]


def test_skips_without_crosscheck_approve() -> None:
    for comments in ([], [CROSSCHECK_NEEDS_WORK]):
        gh = FakeGitHubClient({"head": {"sha": HEAD_SHA}, "html_url": PR_URL}, comments)
        spawn, calls = make_spawn(0, {})
        outcome = run(
            maybe_run_verifyflow(
                github_client=gh, config=_config(), branch="b", pr_number=42,
                already_run_sha=None, spawn=spawn,
            )
        )
        assert outcome is None
        assert calls == []


def test_same_head_sha_is_not_verified_twice() -> None:
    gh = FakeGitHubClient(
        {"head": {"sha": HEAD_SHA}, "html_url": PR_URL}, [CROSSCHECK_APPROVE]
    )
    spawn, calls = make_spawn(0, {})
    outcome = run(
        maybe_run_verifyflow(
            github_client=gh, config=_config(), branch="b", pr_number=42,
            already_run_sha=HEAD_SHA, spawn=spawn,
        )
    )
    assert outcome is None
    assert calls == []


def test_missing_pr_data_skips() -> None:
    gh = FakeGitHubClient(None, [CROSSCHECK_APPROVE])
    spawn, calls = make_spawn(0, {})
    outcome = run(
        maybe_run_verifyflow(
            github_client=gh, config=_config(), branch="b", pr_number=42,
            already_run_sha=None, spawn=spawn,
        )
    )
    assert outcome is None
    assert calls == []


def test_failed_run_still_returns_outcome_for_dedup() -> None:
    """Advisory semantics: a crashed vf step is recorded (no hot-loop retry)."""
    gh = FakeGitHubClient(
        {"head": {"sha": HEAD_SHA}, "html_url": PR_URL}, [CROSSCHECK_APPROVE]
    )
    spawn, _calls = make_spawn(1, None)
    outcome = run(
        maybe_run_verifyflow(
            github_client=gh, config=_config(), branch="b", pr_number=42,
            already_run_sha=None, spawn=spawn,
        )
    )
    assert outcome is not None
    assert outcome.head_sha == HEAD_SHA
    assert outcome.exit_code == 1
    assert outcome.summary is None


def test_ci_green_trigger_runs_without_crosscheck() -> None:
    """trigger: ci_green (IN-570) — repos without Crosscheck still get verified."""
    gh = FakeGitHubClient({"head": {"sha": HEAD_SHA}, "html_url": PR_URL}, [], failed_check_runs=[])
    spawn, calls = make_spawn(0, {"verdict": "accept"})
    outcome = run(
        maybe_run_verifyflow(
            github_client=gh, config=_config(trigger="ci_green"), branch="b", pr_number=42,
            already_run_sha=None, spawn=spawn,
        )
    )
    assert outcome is not None
    assert outcome.head_sha == HEAD_SHA
    # No crosscheck comment → no --crosscheck-verdict arg.
    assert calls == [["vf", "step", "--pr", PR_URL, "--level", "functional"]]


def test_ci_green_trigger_skips_on_failed_checks() -> None:
    gh = FakeGitHubClient(
        {"head": {"sha": HEAD_SHA}, "html_url": PR_URL}, [],
        failed_check_runs=[{"id": 1, "name": "tests", "details_url": "", "summary": "boom"}],
    )
    spawn, calls = make_spawn(0, {})
    outcome = run(
        maybe_run_verifyflow(
            github_client=gh, config=_config(trigger="ci_green"), branch="b", pr_number=42,
            already_run_sha=None, spawn=spawn,
        )
    )
    assert outcome is None
    assert calls == []


def test_ci_green_trigger_still_records_crosscheck_verdict_when_present() -> None:
    gh = FakeGitHubClient(
        {"head": {"sha": HEAD_SHA}, "html_url": PR_URL}, [CROSSCHECK_NEEDS_WORK],
        failed_check_runs=[],
    )
    spawn, calls = make_spawn(0, {"verdict": "accept"})
    outcome = run(
        maybe_run_verifyflow(
            github_client=gh, config=_config(trigger="ci_green"), branch="b", pr_number=42,
            already_run_sha=None, spawn=spawn,
        )
    )
    # ci_green does not gate on the review verdict — it only records it.
    assert outcome is not None
    assert calls[0][-2:] == ["--crosscheck-verdict", "NEEDS WORK"]


def test_unknown_trigger_rejected_by_config() -> None:
    from symphony.config import ConfigError

    with pytest.raises(ConfigError, match="unsupported_verifyflow_trigger"):
        VerifyflowConfig.from_mapping({"verifyflow": {"trigger": "always"}})


def test_custom_command_and_level_are_used() -> None:
    gh = FakeGitHubClient(
        {"head": {"sha": HEAD_SHA}, "html_url": PR_URL}, [CROSSCHECK_APPROVE]
    )
    spawn, calls = make_spawn(0, {"verdict": "accept"})
    run(
        maybe_run_verifyflow(
            github_client=gh, config=_config(command="/opt/vf", level="ui"),
            branch="b", pr_number=42, already_run_sha=None, spawn=spawn,
        )
    )
    assert calls[0][:2] == ["/opt/vf", "step"]
    assert "--level" in calls[0] and calls[0][calls[0].index("--level") + 1] == "ui"
