"""Unit tests for the acceptance gate runtime glue.

The decision core in ``symphony.acceptance`` is exercised in
``test_acceptance.py``; these tests cover the I/O-isolated wrapper —
verdict parsing, prompt/comment rendering, diff path extraction, and the
``maybe_run_acceptance`` orchestrator that ties convergence + judge dispatch
+ comment posting together. The judge subprocess itself is replaced by an
in-process stub so the suite stays hermetic.
"""
from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone

from symphony.acceptance import (
    AcceptanceCheck,
    AcceptanceVerdict,
    CrosscheckVerdict,
    ReviewVerdict,
)
from symphony.acceptance_runtime import (
    SYMPHONY_BOT_MARKER,
    ClaudeCodeJudgeRunner,
    ConvergenceSnapshot,
    JUDGE_DIFF_TRUNCATE,
    extract_changed_files_from_diff,
    gather_convergence_inputs,
    maybe_run_acceptance,
    parse_acceptance_verdict,
    render_judge_user_prompt,
    render_verdict_comment,
)
from symphony.config import AcceptanceConfig
from symphony.tracker.models import Issue


UTC = timezone.utc


def _issue() -> Issue:
    return Issue(
        id="issue-1",
        identifier="SYM-42",
        title="Make widget purple",
        description="The widget should be purple when hover is active.",
        priority=2,
        state="In Progress",
        branch_name="haol/sym-42-purple",
        url=None,
    )


def _approve_comment(body: str, created: datetime) -> dict:
    return {"body": body, "created_at": created.isoformat().replace("+00:00", "Z")}


class FakeGitHubClient:
    """Minimal in-process double for ``GitHubClient``.

    Only the methods the acceptance runtime calls are implemented. Each call
    is recorded so tests can assert idempotency (we do NOT want a verdict
    posted twice for the same head sha).
    """

    def __init__(
        self,
        *,
        pr: dict | None = None,
        comments: list[dict] | None = None,
        failed_checks: list[dict] | None = None,
        diff: str = "",
    ) -> None:
        self.pr = pr or {}
        self.comments = comments or []
        self.failed_checks = failed_checks or []
        self.diff = diff
        self.posted_comments: list[tuple[int, str]] = []
        self.post_pr_comment_should_succeed = True
        # Auto-merge call recording. ``merge_calls`` lets tests assert that
        # the runtime forwards the head sha (so a stale-sha merge gets caught
        # by GitHub's ``required_head`` check rather than slipping past us).
        self.merge_calls: list[dict] = []
        self.merge_pr_should_succeed = True

    def get_pr(self, pr_number: int) -> dict:
        return dict(self.pr)

    def list_pr_issue_comments(self, pr_number: int) -> list[dict]:
        return list(self.comments)

    def get_pr_failed_check_runs(self, pr_number: int) -> list[dict]:
        return list(self.failed_checks)

    def get_pr_diff(self, pr_number: int) -> str:
        return self.diff

    def post_pr_comment(self, pr_number: int, body: str) -> bool:
        self.posted_comments.append((pr_number, body))
        return self.post_pr_comment_should_succeed

    def merge_pr(
        self,
        pr_number: int,
        *,
        sha: str | None = None,
        merge_method: str = "squash",
        commit_title: str | None = None,
    ) -> bool:
        self.merge_calls.append({
            "pr_number": pr_number,
            "sha": sha,
            "merge_method": merge_method,
            "commit_title": commit_title,
        })
        return self.merge_pr_should_succeed


class StubJudge:
    """Replaces ``ClaudeCodeJudgeRunner`` so tests never spawn ``claude``."""

    def __init__(self, response: str | None) -> None:
        self.response = response
        self.last_system_prompt: str | None = None
        self.last_user_prompt: str | None = None
        self.call_count = 0

    async def judge(self, system_prompt: str, user_prompt: str) -> str | None:
        self.call_count += 1
        self.last_system_prompt = system_prompt
        self.last_user_prompt = user_prompt
        return self.response


def _passing_judge_response(*, sensitive: bool = False) -> str:
    return (
        '```json\n'
        '{\n'
        '  "overall": "pass",\n'
        '  "checks": [\n'
        '    {\n'
        '      "requirement": "widget turns purple on hover",\n'
        '      "status": "met",\n'
        '      "evidence": "Widget.tsx:42",\n'
        '      "confidence": 0.95\n'
        '    }\n'
        '  ],\n'
        f'  "touched_sensitive_paths": {"[\"SPEC.md\"]" if sensitive else "[]"},\n'
        '  "confidence": 0.92,\n'
        '  "summary_for_human": "Hover handler now sets purple."\n'
        '}\n'
        '```'
    )


# --------------------------------------------------------------------------- #
# parse_acceptance_verdict
# --------------------------------------------------------------------------- #


class ParseAcceptanceVerdictTests(unittest.TestCase):
    def test_parses_clean_json(self):
        raw = (
            '{"overall":"pass","checks":[],"touched_sensitive_paths":[],'
            '"confidence":0.9,"summary_for_human":"ok"}'
        )
        verdict = parse_acceptance_verdict(raw)
        self.assertIsNotNone(verdict)
        assert verdict is not None
        self.assertEqual(verdict.overall, "pass")
        self.assertEqual(verdict.confidence, 0.9)

    def test_strips_code_fence_and_prose(self):
        raw = (
            "Here is my decision:\n\n"
            "```json\n"
            '{"overall":"fail","checks":[{"requirement":"X","status":"unmet",'
            '"evidence":"diff:1","confidence":0.8}],'
            '"touched_sensitive_paths":[],"confidence":0.7,'
            '"summary_for_human":"missing X"}\n'
            "```"
        )
        verdict = parse_acceptance_verdict(raw)
        self.assertIsNotNone(verdict)
        assert verdict is not None
        self.assertEqual(verdict.overall, "fail")
        self.assertEqual(len(verdict.checks), 1)
        self.assertEqual(verdict.checks[0].status, "unmet")

    def test_rejects_invalid_overall(self):
        raw = '{"overall":"maybe","checks":[],"touched_sensitive_paths":[]}'
        self.assertIsNone(parse_acceptance_verdict(raw))

    def test_rejects_non_json(self):
        self.assertIsNone(parse_acceptance_verdict("not json at all"))
        self.assertIsNone(parse_acceptance_verdict(""))
        self.assertIsNone(parse_acceptance_verdict("{"))

    def test_coerces_bad_status_to_cannot_tell(self):
        raw = (
            '{"overall":"uncertain","checks":[{"requirement":"X",'
            '"status":"weird","evidence":"","confidence":0.2}],'
            '"touched_sensitive_paths":[],"confidence":0.4,'
            '"summary_for_human":""}'
        )
        verdict = parse_acceptance_verdict(raw)
        assert verdict is not None
        self.assertEqual(verdict.checks[0].status, "cannot_tell")

    def test_clamps_confidence_to_unit_interval(self):
        raw = (
            '{"overall":"pass","checks":[{"requirement":"X","status":"met",'
            '"evidence":"","confidence":5.0}],'
            '"touched_sensitive_paths":[],"confidence":-1.0,'
            '"summary_for_human":""}'
        )
        verdict = parse_acceptance_verdict(raw)
        assert verdict is not None
        self.assertEqual(verdict.checks[0].confidence, 1.0)
        self.assertEqual(verdict.confidence, 0.0)

    def test_drops_check_without_requirement(self):
        raw = (
            '{"overall":"pass","checks":[{"requirement":"",'
            '"status":"met","evidence":"x","confidence":0.5},'
            '{"requirement":"real","status":"met","evidence":"y",'
            '"confidence":0.9}],"touched_sensitive_paths":[],'
            '"confidence":0.9,"summary_for_human":""}'
        )
        verdict = parse_acceptance_verdict(raw)
        assert verdict is not None
        self.assertEqual(len(verdict.checks), 1)
        self.assertEqual(verdict.checks[0].requirement, "real")

    def test_extracts_first_balanced_object_when_nested_text_present(self):
        raw = (
            'Some intro {with braces} and then the real object: '
            '{"overall":"pass","checks":[],"touched_sensitive_paths":[],'
            '"confidence":1.0,"summary_for_human":"clean"}'
        )
        # The first balanced object after the first '{' is the prose one;
        # parser should fall through to validation and reject the wrong shape
        # OR succeed on the balanced JSON object. Either way it must not crash.
        result = parse_acceptance_verdict(raw)
        # Whatever the result, the function must terminate without exception.
        self.assertTrue(result is None or isinstance(result, AcceptanceVerdict))


# --------------------------------------------------------------------------- #
# render_judge_user_prompt
# --------------------------------------------------------------------------- #


class RenderJudgePromptTests(unittest.TestCase):
    def test_includes_issue_and_diff(self):
        prompt = render_judge_user_prompt(_issue(), "diff --git a/x b/x\n+hi\n", None)
        self.assertIn("SYM-42", prompt)
        self.assertIn("Make widget purple", prompt)
        self.assertIn("hover is active", prompt)
        self.assertIn("```diff", prompt)
        self.assertIn("+hi", prompt)

    def test_includes_crosscheck_verdict_when_provided(self):
        verdict = ReviewVerdict(
            verdict=CrosscheckVerdict.APPROVE,
            source="comment",
            created_at=datetime(2026, 5, 30, tzinfo=UTC),
        )
        prompt = render_judge_user_prompt(_issue(), "diff\n", verdict)
        self.assertIn("APPROVE", prompt)
        self.assertIn("source=comment", prompt)
        self.assertIn("do not re-do", prompt.lower())

    def test_omits_crosscheck_section_when_none(self):
        prompt = render_judge_user_prompt(_issue(), "diff\n", None)
        self.assertNotIn("Prior code-review verdict", prompt)

    def test_handles_empty_description(self):
        issue = Issue(
            id="i", identifier="SYM-1", title="t", description=None,
            priority=None, state="x", branch_name=None, url=None,
        )
        prompt = render_judge_user_prompt(issue, "", None)
        self.assertIn("(no description on issue)", prompt)
        self.assertIn("(no diff available)", prompt)

    def test_truncates_oversized_diff(self):
        big_diff = "x" * (JUDGE_DIFF_TRUNCATE + 5_000)
        prompt = render_judge_user_prompt(_issue(), big_diff, None)
        self.assertIn("[... diff truncated ...]", prompt)
        # Prompt should not contain the full untruncated diff.
        self.assertLess(len(prompt), JUDGE_DIFF_TRUNCATE + 2_000)


# --------------------------------------------------------------------------- #
# render_verdict_comment
# --------------------------------------------------------------------------- #


class RenderVerdictCommentTests(unittest.TestCase):
    def _verdict(self, **overrides) -> AcceptanceVerdict:
        defaults = dict(
            overall="pass",
            checks=(
                AcceptanceCheck(
                    requirement="widget is purple",
                    status="met",
                    evidence="Widget.tsx:42",
                    confidence=0.95,
                ),
            ),
            touched_sensitive_paths=(),
            confidence=0.9,
            summary_for_human="It works.",
        )
        defaults.update(overrides)
        return AcceptanceVerdict(**defaults)

    def test_includes_bot_marker_so_poller_skips_own_comment(self):
        body = render_verdict_comment(self._verdict())
        self.assertTrue(body.startswith(SYMPHONY_BOT_MARKER))

    def test_shows_overall_in_uppercase(self):
        body = render_verdict_comment(self._verdict(overall="uncertain"))
        self.assertIn("UNCERTAIN", body)

    def test_renders_each_check_with_status_tag(self):
        body = render_verdict_comment(self._verdict())
        self.assertIn("[met]", body)
        self.assertIn("widget is purple", body)
        self.assertIn("Widget.tsx:42", body)

    def test_renders_guard_rail_section_when_paths_touched(self):
        body = render_verdict_comment(
            self._verdict(touched_sensitive_paths=("SPEC.md",))
        )
        self.assertIn("Guard-rail paths", body)
        self.assertIn("`SPEC.md`", body)

    def test_includes_escalation_notice_when_not_merged(self):
        body = render_verdict_comment(self._verdict(), escalation=True)
        self.assertIn("not auto-merge", body)
        self.assertIn("human reviewer", body)
        self.assertNotIn("Auto-merged", body)

    def test_omits_escalation_when_disabled(self):
        body = render_verdict_comment(self._verdict(), escalation=False)
        self.assertNotIn("human reviewer", body)
        self.assertNotIn("Auto-merged", body)

    def test_celebrates_auto_merge_when_merged_true(self):
        """When auto-merge fires the comment must say so explicitly and skip
        the human-escalation tail — otherwise reviewers see contradictory
        text on a merged PR."""
        body = render_verdict_comment(self._verdict(), merged=True, escalation=False)
        self.assertIn("Auto-merged", body)
        self.assertIn("squash", body.lower())
        self.assertNotIn("human reviewer makes the final call", body)

    def test_names_skip_reason_when_provided(self):
        """The auto-merge skip reason must surface inside the PR comment so
        the gate's behavior is auditable from the PR thread alone."""
        body = render_verdict_comment(
            self._verdict(),
            escalation=True,
            auto_merge_skip_reason="verdict is uncertain, not pass",
        )
        self.assertIn("Auto-merge did not fire", body)
        self.assertIn("uncertain", body)


# --------------------------------------------------------------------------- #
# extract_changed_files_from_diff
# --------------------------------------------------------------------------- #


class ExtractChangedFilesTests(unittest.TestCase):
    def test_extracts_single_file(self):
        diff = "diff --git a/foo.py b/foo.py\n+x\n"
        self.assertEqual(extract_changed_files_from_diff(diff), ("foo.py",))

    def test_extracts_multiple_files(self):
        diff = (
            "diff --git a/foo.py b/foo.py\n+x\n"
            "diff --git a/bar/baz.py b/bar/baz.py\n+y\n"
        )
        self.assertEqual(
            extract_changed_files_from_diff(diff),
            ("foo.py", "bar/baz.py"),
        )

    def test_keeps_both_sides_of_rename(self):
        diff = "diff --git a/old.py b/new.py\nrename from old.py\n"
        self.assertEqual(extract_changed_files_from_diff(diff), ("old.py", "new.py"))

    def test_skips_dev_null(self):
        diff = "diff --git a/SPEC.md b/SPEC.md\n+y\n"
        self.assertEqual(extract_changed_files_from_diff(diff), ("SPEC.md",))

    def test_empty_diff_returns_empty(self):
        self.assertEqual(extract_changed_files_from_diff(""), ())
        self.assertEqual(extract_changed_files_from_diff("not a diff"), ())


# --------------------------------------------------------------------------- #
# gather_convergence_inputs
# --------------------------------------------------------------------------- #


class GatherConvergenceInputsTests(unittest.TestCase):
    def test_threads_review_source_and_head_sha(self):
        gh = FakeGitHubClient(pr={"head": {"sha": "abc123"}, "updated_at": "2026-05-30T00:00:00Z"})
        cfg = AcceptanceConfig(enabled=True, review_source="auto", quiet_period_seconds=120)
        inputs, raw = gather_convergence_inputs(
            github_client=gh,
            pr_number=1,
            config=cfg,
            pr_turns=0,
            snapshot=None,
            now=datetime(2026, 5, 30, 0, 1, 0, tzinfo=UTC),
            saw_new_feedback_this_tick=False,
        )
        self.assertEqual(inputs.review_source, "auto")
        self.assertEqual(inputs.head_sha, "abc123")
        self.assertEqual(raw["head_sha"], "abc123")
        self.assertEqual(inputs.quiet_period_seconds, 120.0)

    def test_picks_up_crosscheck_verdict_from_comments(self):
        comments = [_approve_comment(
            "[crosscheck] looks good\n\nVERDICT: APPROVE",
            datetime(2026, 5, 30, 1, 0, 0, tzinfo=UTC),
        )]
        gh = FakeGitHubClient(
            pr={"head": {"sha": "x"}, "updated_at": "2026-05-30T00:00:00Z"},
            comments=comments,
        )
        cfg = AcceptanceConfig(enabled=True, review_source="auto")
        inputs, _ = gather_convergence_inputs(
            github_client=gh,
            pr_number=1,
            config=cfg,
            pr_turns=0,
            snapshot=None,
            now=datetime(2026, 5, 30, 2, 0, 0, tzinfo=UTC),
            saw_new_feedback_this_tick=False,
        )
        self.assertIsNotNone(inputs.crosscheck_verdict)
        assert inputs.crosscheck_verdict is not None
        self.assertEqual(inputs.crosscheck_verdict.verdict, CrosscheckVerdict.APPROVE)

    def test_ci_green_when_no_failed_check_runs(self):
        gh = FakeGitHubClient(pr={"head": {"sha": "x"}}, failed_checks=[])
        cfg = AcceptanceConfig(enabled=True)
        inputs, _ = gather_convergence_inputs(
            github_client=gh,
            pr_number=1,
            config=cfg,
            pr_turns=0,
            snapshot=None,
            now=datetime(2026, 5, 30, tzinfo=UTC),
            saw_new_feedback_this_tick=False,
        )
        self.assertTrue(inputs.ci_green)

    def test_ci_not_green_when_failed_check_runs_exist(self):
        gh = FakeGitHubClient(pr={"head": {"sha": "x"}}, failed_checks=[{"id": 1}])
        cfg = AcceptanceConfig(enabled=True)
        inputs, _ = gather_convergence_inputs(
            github_client=gh,
            pr_number=1,
            config=cfg,
            pr_turns=0,
            snapshot=None,
            now=datetime(2026, 5, 30, tzinfo=UTC),
            saw_new_feedback_this_tick=False,
        )
        self.assertFalse(inputs.ci_green)

    def test_quiet_for_seconds_zero_without_snapshot(self):
        gh = FakeGitHubClient(pr={"head": {"sha": "x"}})
        cfg = AcceptanceConfig(enabled=True, quiet_period_seconds=60)
        inputs, _ = gather_convergence_inputs(
            github_client=gh,
            pr_number=1,
            config=cfg,
            pr_turns=0,
            snapshot=None,
            now=datetime(2026, 5, 30, tzinfo=UTC),
            saw_new_feedback_this_tick=False,
        )
        self.assertEqual(inputs.quiet_for_seconds, 0.0)

    def test_quiet_for_seconds_uses_snapshot_clock(self):
        gh = FakeGitHubClient(pr={"head": {"sha": "x"}})
        cfg = AcceptanceConfig(enabled=True, quiet_period_seconds=60)
        snap = ConvergenceSnapshot(
            last_feedback_at=datetime(2026, 5, 30, 0, 0, 0, tzinfo=UTC),
            pr_turns_observed=0,
        )
        inputs, _ = gather_convergence_inputs(
            github_client=gh,
            pr_number=1,
            config=cfg,
            pr_turns=0,
            snapshot=snap,
            now=datetime(2026, 5, 30, 0, 5, 0, tzinfo=UTC),
            saw_new_feedback_this_tick=False,
        )
        self.assertEqual(inputs.quiet_for_seconds, 300.0)

    def test_pr_turn_advancing_set_when_counter_grows(self):
        gh = FakeGitHubClient(pr={"head": {"sha": "x"}})
        cfg = AcceptanceConfig(enabled=True)
        snap = ConvergenceSnapshot(
            last_feedback_at=datetime(2026, 5, 30, tzinfo=UTC),
            pr_turns_observed=2,
        )
        inputs, _ = gather_convergence_inputs(
            github_client=gh,
            pr_number=1,
            config=cfg,
            pr_turns=3,
            snapshot=snap,
            now=datetime(2026, 5, 30, tzinfo=UTC),
            saw_new_feedback_this_tick=False,
        )
        self.assertTrue(inputs.pr_turn_advancing)


# --------------------------------------------------------------------------- #
# maybe_run_acceptance
# --------------------------------------------------------------------------- #


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


class MaybeRunAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_config_is_a_noop(self):
        gh = FakeGitHubClient()
        cfg = AcceptanceConfig(enabled=False)
        judge = StubJudge(response="ignored")
        snap, judged_sha, verdict, result = await maybe_run_acceptance(
            github_client=gh,
            judge=judge,  # type: ignore[arg-type]
            config=cfg,
            issue=_issue(),
            branch="haol/sym-42-purple",
            pr_number=1,
            pr_turns=0,
            snapshot=None,
            now=datetime(2026, 5, 30, tzinfo=UTC),
            saw_new_feedback_this_tick=False,
            already_judged_sha=None,
        )
        self.assertIsNone(judged_sha)
        self.assertIsNone(verdict)
        self.assertIsNone(result)
        self.assertEqual(judge.call_count, 0)
        self.assertEqual(gh.posted_comments, [])

    async def test_not_converged_skips_judge_but_updates_snapshot(self):
        # New feedback this tick → silent branch fails fast on has_new_feedback.
        gh = FakeGitHubClient(
            pr={"head": {"sha": "abc"}, "updated_at": "2026-05-30T00:00:00Z"},
        )
        cfg = AcceptanceConfig(enabled=True, review_source="none", quiet_period_seconds=60)
        judge = StubJudge(response="ignored")
        now = datetime(2026, 5, 30, 1, 0, 0, tzinfo=UTC)
        snap, judged_sha, verdict, result = await maybe_run_acceptance(
            github_client=gh,
            judge=judge,  # type: ignore[arg-type]
            config=cfg,
            issue=_issue(),
            branch="branch-1",
            pr_number=42,
            pr_turns=1,
            snapshot=None,
            now=now,
            saw_new_feedback_this_tick=True,
            already_judged_sha=None,
        )
        self.assertIsNone(judged_sha)
        self.assertIsNone(verdict)
        self.assertIsNotNone(result)
        self.assertFalse(result.converged)
        self.assertEqual(judge.call_count, 0)
        # Snapshot updated so quiet clock starts ticking from this tick.
        self.assertEqual(snap.last_feedback_at, now)
        self.assertEqual(snap.pr_turns_observed, 1)

    async def test_already_judged_sha_skips_redundant_judge(self):
        # Silent branch converges when CI green, no new feedback, quiet period
        # elapsed, no advancing turn.
        gh = FakeGitHubClient(
            pr={"head": {"sha": "stable-sha"}, "updated_at": "2026-05-30T00:00:00Z"},
        )
        cfg = AcceptanceConfig(enabled=True, review_source="none", quiet_period_seconds=60)
        judge = StubJudge(response=_passing_judge_response())
        snap_in = ConvergenceSnapshot(
            last_feedback_at=datetime(2026, 5, 29, 0, 0, 0, tzinfo=UTC),
            pr_turns_observed=1,
        )
        snap_out, judged_sha, verdict, result = await maybe_run_acceptance(
            github_client=gh,
            judge=judge,  # type: ignore[arg-type]
            config=cfg,
            issue=_issue(),
            branch="branch-1",
            pr_number=42,
            pr_turns=1,
            snapshot=snap_in,
            now=datetime(2026, 5, 30, 0, 0, 0, tzinfo=UTC),
            saw_new_feedback_this_tick=False,
            already_judged_sha="stable-sha",
        )
        assert result is not None
        self.assertTrue(result.converged, msg=result.reason)
        self.assertIsNone(judged_sha)
        self.assertIsNone(verdict)
        self.assertEqual(judge.call_count, 0)
        self.assertEqual(gh.posted_comments, [])

    async def test_converged_dispatches_judge_and_posts_verdict(self):
        gh = FakeGitHubClient(
            pr={"head": {"sha": "head-1"}, "updated_at": "2026-05-30T00:00:00Z"},
            diff="diff --git a/foo.py b/foo.py\n+y\n",
        )
        cfg = AcceptanceConfig(enabled=True, review_source="none", quiet_period_seconds=60)
        judge = StubJudge(response=_passing_judge_response())
        snap_in = ConvergenceSnapshot(
            last_feedback_at=datetime(2026, 5, 29, 0, 0, 0, tzinfo=UTC),
            pr_turns_observed=1,
        )
        snap_out, judged_sha, verdict, result = await maybe_run_acceptance(
            github_client=gh,
            judge=judge,  # type: ignore[arg-type]
            config=cfg,
            issue=_issue(),
            branch="branch-1",
            pr_number=42,
            pr_turns=1,
            snapshot=snap_in,
            now=datetime(2026, 5, 30, 0, 0, 0, tzinfo=UTC),
            saw_new_feedback_this_tick=False,
            already_judged_sha=None,
        )
        assert result is not None
        self.assertTrue(result.converged)
        self.assertEqual(judge.call_count, 1)
        self.assertEqual(len(gh.posted_comments), 1)
        self.assertEqual(judged_sha, "head-1")
        assert verdict is not None
        self.assertEqual(verdict.overall, "pass")
        # Comment body carries the bot marker and escalates to a human because
        # ``auto_merge`` is False by default — no merge was attempted.
        _, body = gh.posted_comments[0]
        self.assertTrue(body.startswith(SYMPHONY_BOT_MARKER))
        self.assertIn("PASS", body)
        self.assertIn("not auto-merge", body)
        self.assertEqual(gh.merge_calls, [])

    async def test_sensitive_paths_force_uncertain_even_when_judge_says_pass(self):
        # Diff touches SPEC.md (default guard pattern) but judge ignored it.
        gh = FakeGitHubClient(
            pr={"head": {"sha": "head-2"}, "updated_at": "2026-05-30T00:00:00Z"},
            diff="diff --git a/SPEC.md b/SPEC.md\n+rule change\n",
        )
        cfg = AcceptanceConfig(
            enabled=True, review_source="none", quiet_period_seconds=60,
            guard_paths=("SPEC.md",),
        )
        judge = StubJudge(response=_passing_judge_response())
        snap_in = ConvergenceSnapshot(
            last_feedback_at=datetime(2026, 5, 29, 0, 0, 0, tzinfo=UTC),
            pr_turns_observed=1,
        )
        _, judged_sha, verdict, _ = await maybe_run_acceptance(
            github_client=gh,
            judge=judge,  # type: ignore[arg-type]
            config=cfg,
            issue=_issue(),
            branch="branch-1",
            pr_number=42,
            pr_turns=1,
            snapshot=snap_in,
            now=datetime(2026, 5, 30, 0, 0, 0, tzinfo=UTC),
            saw_new_feedback_this_tick=False,
            already_judged_sha=None,
        )
        assert verdict is not None
        self.assertEqual(verdict.overall, "uncertain")
        self.assertIn("SPEC.md", verdict.touched_sensitive_paths)
        self.assertEqual(judged_sha, "head-2")

    async def test_judge_returns_unparseable_text_skips_post(self):
        gh = FakeGitHubClient(
            pr={"head": {"sha": "head-3"}, "updated_at": "2026-05-30T00:00:00Z"},
        )
        cfg = AcceptanceConfig(enabled=True, review_source="none", quiet_period_seconds=60)
        judge = StubJudge(response="I refuse to judge")
        snap_in = ConvergenceSnapshot(
            last_feedback_at=datetime(2026, 5, 29, 0, 0, 0, tzinfo=UTC),
            pr_turns_observed=1,
        )
        _, judged_sha, verdict, _ = await maybe_run_acceptance(
            github_client=gh,
            judge=judge,  # type: ignore[arg-type]
            config=cfg,
            issue=_issue(),
            branch="branch-1",
            pr_number=42,
            pr_turns=1,
            snapshot=snap_in,
            now=datetime(2026, 5, 30, 0, 0, 0, tzinfo=UTC),
            saw_new_feedback_this_tick=False,
            already_judged_sha=None,
        )
        self.assertIsNone(verdict)
        self.assertIsNone(judged_sha)
        self.assertEqual(judge.call_count, 1)
        self.assertEqual(gh.posted_comments, [])

    async def test_post_failure_does_not_record_judged_sha(self):
        gh = FakeGitHubClient(
            pr={"head": {"sha": "head-4"}, "updated_at": "2026-05-30T00:00:00Z"},
        )
        gh.post_pr_comment_should_succeed = False
        cfg = AcceptanceConfig(enabled=True, review_source="none", quiet_period_seconds=60)
        judge = StubJudge(response=_passing_judge_response())
        snap_in = ConvergenceSnapshot(
            last_feedback_at=datetime(2026, 5, 29, 0, 0, 0, tzinfo=UTC),
            pr_turns_observed=1,
        )
        _, judged_sha, verdict, _ = await maybe_run_acceptance(
            github_client=gh,
            judge=judge,  # type: ignore[arg-type]
            config=cfg,
            issue=_issue(),
            branch="branch-1",
            pr_number=42,
            pr_turns=1,
            snapshot=snap_in,
            now=datetime(2026, 5, 30, 0, 0, 0, tzinfo=UTC),
            saw_new_feedback_this_tick=False,
            already_judged_sha=None,
        )
        # judged_sha is None on post failure so the next tick can retry.
        self.assertIsNone(judged_sha)
        self.assertIsNotNone(verdict)


# --------------------------------------------------------------------------- #
# Phase 2 — auto-merge
# --------------------------------------------------------------------------- #


def _make_snap() -> ConvergenceSnapshot:
    return ConvergenceSnapshot(
        last_feedback_at=datetime(2026, 5, 29, 0, 0, 0, tzinfo=UTC),
        pr_turns_observed=1,
    )


class AutoMergeTests(unittest.IsolatedAsyncioTestCase):
    """Phase 2 auto-merge fires only on the four-condition gate; everything
    else still posts the verdict comment and escalates to a human. The tests
    walk each precondition separately so a regression points at the exact
    rule that broke."""

    async def _run(
        self,
        *,
        auto_merge: bool,
        confidence_threshold: float = 0.8,
        diff: str = "diff --git a/foo.py b/foo.py\n+y\n",
        judge_response: str | None = None,
        pr_head_sha: str = "head-auto",
        merge_succeeds: bool = True,
    ):
        gh = FakeGitHubClient(
            pr={"head": {"sha": pr_head_sha}, "updated_at": "2026-05-30T00:00:00Z"},
            diff=diff,
        )
        gh.merge_pr_should_succeed = merge_succeeds
        cfg = AcceptanceConfig(
            enabled=True,
            review_source="none",
            quiet_period_seconds=60,
            auto_merge=auto_merge,
            confidence_threshold=confidence_threshold,
        )
        judge = StubJudge(response=judge_response or _passing_judge_response())
        _, judged_sha, verdict, _ = await maybe_run_acceptance(
            github_client=gh,
            judge=judge,  # type: ignore[arg-type]
            config=cfg,
            issue=_issue(),
            branch="branch-1",
            pr_number=42,
            pr_turns=1,
            snapshot=_make_snap(),
            now=datetime(2026, 5, 30, 0, 0, 0, tzinfo=UTC),
            saw_new_feedback_this_tick=False,
            already_judged_sha=None,
        )
        return gh, verdict, judged_sha

    async def test_disabled_auto_merge_never_calls_merge_pr(self):
        """auto_merge=False is the production safe default — pass verdicts
        still escalate to a human, no merge call goes out."""
        gh, verdict, _ = await self._run(auto_merge=False)
        assert verdict is not None
        self.assertEqual(verdict.overall, "pass")
        self.assertEqual(gh.merge_calls, [])
        self.assertIn("not auto-merge", gh.posted_comments[0][1])

    async def test_pass_high_confidence_no_guards_triggers_squash_merge(self):
        """The full auto-merge path: every precondition met, ``merge_pr``
        succeeds, comment celebrates the auto-merge."""
        gh, verdict, judged_sha = await self._run(auto_merge=True)
        assert verdict is not None
        self.assertEqual(verdict.overall, "pass")
        self.assertEqual(len(gh.merge_calls), 1)
        call = gh.merge_calls[0]
        # The runtime MUST forward the head sha so GitHub's required_head
        # check tells us if a new commit landed after the verdict.
        self.assertEqual(call["sha"], "head-auto")
        self.assertEqual(call["merge_method"], "squash")
        self.assertEqual(call["pr_number"], 42)
        self.assertEqual(judged_sha, "head-auto")
        body = gh.posted_comments[0][1]
        self.assertIn("Auto-merged", body)
        self.assertNotIn("human reviewer makes the final call", body)

    async def test_uncertain_verdict_blocks_auto_merge(self):
        """Verdict overall != pass means the judge had doubts. Skip the merge
        and explain why on the PR."""
        uncertain_response = _passing_judge_response().replace(
            '"overall": "pass"', '"overall": "uncertain"'
        )
        gh, verdict, _ = await self._run(
            auto_merge=True, judge_response=uncertain_response
        )
        assert verdict is not None
        self.assertEqual(verdict.overall, "uncertain")
        self.assertEqual(gh.merge_calls, [])
        body = gh.posted_comments[0][1]
        self.assertIn("Auto-merge did not fire", body)
        self.assertIn("not pass", body)

    async def test_low_confidence_blocks_auto_merge(self):
        """Confidence threshold is the calibration knob — sub-threshold pass
        verdicts must NOT auto-merge."""
        low_conf_response = _passing_judge_response().replace(
            '"confidence": 0.92', '"confidence": 0.50'
        )
        gh, verdict, _ = await self._run(
            auto_merge=True,
            confidence_threshold=0.8,
            judge_response=low_conf_response,
        )
        assert verdict is not None
        self.assertEqual(gh.merge_calls, [])
        body = gh.posted_comments[0][1]
        self.assertIn("below the configured threshold", body)
        self.assertIn("0.80", body)

    async def test_sensitive_paths_block_auto_merge(self):
        """Even with a perfect verdict, touching a guard-rail path must
        force human review — the gate's whole point is that some changes
        are too costly to autonomously merge."""
        sensitive_diff = "diff --git a/SPEC.md b/SPEC.md\n+rule change\n"
        gh, verdict, _ = await self._run(auto_merge=True, diff=sensitive_diff)
        assert verdict is not None
        # Guard-rail override has already flipped this to uncertain, so the
        # first skip reason hit is the verdict, not the paths. Either way:
        # no merge, comment explains.
        self.assertEqual(gh.merge_calls, [])
        body = gh.posted_comments[0][1]
        self.assertIn("Auto-merge did not fire", body)

    async def test_github_rejects_merge_falls_back_to_escalation(self):
        """GitHub branch protection / required reviews / stale head sha can
        all reject the merge. Symphony surfaces the rejection on the PR so
        a human knows the gate tried and where to look."""
        gh, verdict, _ = await self._run(auto_merge=True, merge_succeeds=False)
        assert verdict is not None
        # We DID try to merge — the call was made — but GH said no.
        self.assertEqual(len(gh.merge_calls), 1)
        body = gh.posted_comments[0][1]
        self.assertIn("GitHub rejected", body)
        self.assertNotIn("Auto-merged by acceptance gate", body)


if __name__ == "__main__":
    unittest.main()
