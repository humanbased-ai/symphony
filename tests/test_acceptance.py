"""Tests for the acceptance gate pure core (symphony/acceptance.py)."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from symphony.acceptance import (
    ACCEPTANCE_JUDGE_SYSTEM_PROMPT,
    AcceptanceVerdict,
    ConvergenceInputs,
    CrosscheckVerdict,
    ReviewVerdict,
    detect_sensitive_paths,
    evaluate_convergence,
    parse_crosscheck_log_verdict,
    parse_crosscheck_verdict,
    parse_verdict_word,
)
from symphony.config import DEFAULT_ACCEPTANCE_GUARD_PATHS


def _dt(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


class ParseVerdictWordTests(unittest.TestCase):
    def test_extracts_each_verdict(self):
        self.assertEqual(parse_verdict_word("VERDICT: APPROVE"), CrosscheckVerdict.APPROVE)
        self.assertEqual(parse_verdict_word("VERDICT: NEEDS WORK"), CrosscheckVerdict.NEEDS_WORK)
        self.assertEqual(parse_verdict_word("VERDICT: BLOCK"), CrosscheckVerdict.BLOCK)

    def test_case_insensitive_and_embedded(self):
        body = "## Summary\nlooks fine\n\nverdict:  approve\n"
        self.assertEqual(parse_verdict_word(body), CrosscheckVerdict.APPROVE)

    def test_no_verdict_returns_none(self):
        self.assertIsNone(parse_verdict_word("no verdict here"))
        self.assertIsNone(parse_verdict_word(""))


class ParseCrosscheckCommentTests(unittest.TestCase):
    def test_picks_latest_crosscheck_comment(self):
        comments = [
            {"body": "[crosscheck] review\nVERDICT: NEEDS WORK", "created_at": "2026-05-01T10:00:00Z"},
            {"body": "a human says LGTM", "created_at": "2026-05-01T11:00:00Z"},
            {"body": "[crosscheck] re-review\nVERDICT: APPROVE", "created_at": "2026-05-01T12:00:00Z"},
        ]
        verdict = parse_crosscheck_verdict(comments)
        self.assertIsNotNone(verdict)
        self.assertEqual(verdict.verdict, CrosscheckVerdict.APPROVE)
        self.assertEqual(verdict.source, "comment")
        self.assertEqual(verdict.created_at, _dt("2026-05-01T12:00:00"))

    def test_ignores_non_crosscheck_comments(self):
        comments = [{"body": "human: VERDICT: APPROVE", "created_at": "2026-05-01T10:00:00Z"}]
        self.assertIsNone(parse_crosscheck_verdict(comments))

    def test_crosscheck_without_verdict_line_skipped(self):
        comments = [{"body": "[crosscheck] still thinking", "created_at": "2026-05-01T10:00:00Z"}]
        self.assertIsNone(parse_crosscheck_verdict(comments))

    def test_empty(self):
        self.assertIsNone(parse_crosscheck_verdict([]))


class ParseCrosscheckLogTests(unittest.TestCase):
    LOG = "\n".join(
        [
            '{"ts":"2026-05-01T10:00:00Z","event":"pr_received","pr":129,"sha":"aaa111"}',
            '{"ts":"2026-05-01T10:00:05Z","event":"review_started","pr":129,"reviewer":"claude"}',
            '{"ts":"2026-05-01T10:00:30Z","event":"review_complete","pr":129,"verdict":"NEEDS WORK"}',
            '{"ts":"2026-05-01T11:00:00Z","event":"pr_received","pr":129,"sha":"bbb222"}',
            '{"ts":"2026-05-01T11:00:30Z","event":"review_complete","pr":129,"verdict":"APPROVE"}',
            '{"ts":"2026-05-01T12:00:00Z","event":"review_complete","pr":999,"verdict":"BLOCK"}',
            "not json",
            "",
        ]
    )

    def test_latest_verdict_bound_to_sha(self):
        verdict = parse_crosscheck_log_verdict(self.LOG, 129)
        self.assertIsNotNone(verdict)
        self.assertEqual(verdict.verdict, CrosscheckVerdict.APPROVE)
        self.assertEqual(verdict.sha, "bbb222")
        self.assertEqual(verdict.source, "log")

    def test_unknown_pr_returns_none(self):
        self.assertIsNone(parse_crosscheck_log_verdict(self.LOG, 42))


class DetectSensitivePathsTests(unittest.TestCase):
    def test_matches_top_level_spec(self):
        hits = detect_sensitive_paths(["SPEC.md", "README.md"], DEFAULT_ACCEPTANCE_GUARD_PATHS)
        self.assertEqual(hits, ("SPEC.md",))

    def test_matches_nested_migrations(self):
        hits = detect_sensitive_paths(
            ["app/db/migrations/0001.sql", "app/main.py"], DEFAULT_ACCEPTANCE_GUARD_PATHS
        )
        self.assertEqual(hits, ("app/db/migrations/0001.sql",))

    def test_matches_github_and_secrets_and_keys(self):
        files = [".github/workflows/ci.yml", "config/secrets/prod.env", "deploy/server.pem"]
        hits = detect_sensitive_paths(files, DEFAULT_ACCEPTANCE_GUARD_PATHS)
        self.assertEqual(set(hits), set(files))

    def test_clean_diff_no_hits(self):
        self.assertEqual(detect_sensitive_paths(["a.py", "b.ts"], DEFAULT_ACCEPTANCE_GUARD_PATHS), ())


class ConvergenceCrosscheckBranchTests(unittest.TestCase):
    def _approve(self, sha="head1", created="2026-05-01T12:00:00"):
        return ReviewVerdict(
            verdict=CrosscheckVerdict.APPROVE, source="log", created_at=_dt(created), sha=sha
        )

    def test_converges_on_approve_matching_head(self):
        result = evaluate_convergence(
            ConvergenceInputs(
                review_source="crosscheck",
                head_sha="head1",
                crosscheck_verdict=self._approve(sha="head1"),
                has_open_autofix_pr=False,
                quiet_for_seconds=400,
                quiet_period_seconds=300,
            )
        )
        self.assertTrue(result.converged)
        self.assertEqual(result.source, "crosscheck")

    def test_needs_work_does_not_converge(self):
        v = ReviewVerdict(verdict=CrosscheckVerdict.NEEDS_WORK, source="log", sha="head1")
        result = evaluate_convergence(
            ConvergenceInputs(review_source="crosscheck", head_sha="head1", crosscheck_verdict=v,
                              quiet_for_seconds=400, quiet_period_seconds=300)
        )
        self.assertFalse(result.converged)

    def test_stale_approval_does_not_converge(self):
        result = evaluate_convergence(
            ConvergenceInputs(
                review_source="crosscheck",
                head_sha="head2",  # head moved past the reviewed sha
                crosscheck_verdict=self._approve(sha="head1"),
                quiet_for_seconds=400,
                quiet_period_seconds=300,
            )
        )
        self.assertFalse(result.converged)
        self.assertIn("stale", result.reason)

    def test_open_autofix_pr_blocks(self):
        result = evaluate_convergence(
            ConvergenceInputs(
                review_source="crosscheck",
                head_sha="head1",
                crosscheck_verdict=self._approve(sha="head1"),
                has_open_autofix_pr=True,
                quiet_for_seconds=400,
                quiet_period_seconds=300,
            )
        )
        self.assertFalse(result.converged)

    def test_quiet_period_not_elapsed_blocks(self):
        result = evaluate_convergence(
            ConvergenceInputs(
                review_source="crosscheck",
                head_sha="head1",
                crosscheck_verdict=self._approve(sha="head1"),
                quiet_for_seconds=10,
                quiet_period_seconds=300,
            )
        )
        self.assertFalse(result.converged)

    def test_comment_source_uses_timestamp_binding(self):
        v = ReviewVerdict(
            verdict=CrosscheckVerdict.APPROVE, source="comment",
            created_at=_dt("2026-05-01T12:00:00"),
        )
        ok = evaluate_convergence(
            ConvergenceInputs(
                review_source="crosscheck",
                crosscheck_verdict=v,
                last_commit_at=_dt("2026-05-01T11:00:00"),  # approval newer than commit
                quiet_for_seconds=400, quiet_period_seconds=300,
            )
        )
        self.assertTrue(ok.converged)

        stale = evaluate_convergence(
            ConvergenceInputs(
                review_source="crosscheck",
                crosscheck_verdict=v,
                last_commit_at=_dt("2026-05-01T13:00:00"),  # commit newer than approval
                quiet_for_seconds=400, quiet_period_seconds=300,
            )
        )
        self.assertFalse(stale.converged)


class ConvergenceSilentBranchTests(unittest.TestCase):
    def _quiet(self, **over):
        base = dict(
            review_source="none",
            has_new_feedback=False,
            ci_green=True,
            pr_turn_advancing=False,
            quiet_for_seconds=400,
            quiet_period_seconds=300,
        )
        base.update(over)
        return ConvergenceInputs(**base)

    def test_converges_when_quiesced(self):
        self.assertTrue(evaluate_convergence(self._quiet()).converged)

    def test_new_feedback_blocks(self):
        self.assertFalse(evaluate_convergence(self._quiet(has_new_feedback=True)).converged)

    def test_red_ci_blocks(self):
        self.assertFalse(evaluate_convergence(self._quiet(ci_green=False)).converged)

    def test_advancing_turns_block(self):
        self.assertFalse(evaluate_convergence(self._quiet(pr_turn_advancing=True)).converged)

    def test_quiet_period_blocks(self):
        self.assertFalse(evaluate_convergence(self._quiet(quiet_for_seconds=5)).converged)


class ConvergenceAutoSourceTests(unittest.TestCase):
    def test_auto_uses_crosscheck_when_verdict_present(self):
        v = ReviewVerdict(verdict=CrosscheckVerdict.NEEDS_WORK, source="log", sha="head1")
        result = evaluate_convergence(
            ConvergenceInputs(
                review_source="auto", head_sha="head1", crosscheck_verdict=v,
                quiet_for_seconds=400, quiet_period_seconds=300,
            )
        )
        self.assertEqual(result.source, "crosscheck")
        self.assertFalse(result.converged)

    def test_auto_falls_back_to_silent_without_verdict(self):
        result = evaluate_convergence(
            ConvergenceInputs(
                review_source="auto", crosscheck_verdict=None,
                has_new_feedback=False, ci_green=True, pr_turn_advancing=False,
                quiet_for_seconds=400, quiet_period_seconds=300,
            )
        )
        self.assertEqual(result.source, "silent")
        self.assertTrue(result.converged)


class ConvergenceCrosscheckWaitTests(unittest.TestCase):
    """``auto`` mode grace window for slow crosscheck.

    crosscheck typically posts its ``VERDICT:`` comment minutes after a PR
    opens — well after CI usually goes green. Without a wait window the
    silent branch can race crosscheck and judge before code review
    happens. The grace window holds ``auto`` open until either crosscheck
    arrives or the PR is older than ``crosscheck_wait_seconds``."""

    def _silent_ready_inputs(self, **overrides):
        """All silent-branch preconditions met, so any non-converged
        result must come from the grace window, not a silent block."""
        base = dict(
            review_source="auto",
            crosscheck_verdict=None,
            has_new_feedback=False,
            ci_green=True,
            pr_turn_advancing=False,
            quiet_for_seconds=400,
            quiet_period_seconds=300,
            crosscheck_wait_seconds=1800,
        )
        base.update(overrides)
        return ConvergenceInputs(**base)

    def test_holds_for_crosscheck_when_pr_is_young(self):
        result = evaluate_convergence(self._silent_ready_inputs(pr_age_seconds=120))
        self.assertFalse(result.converged)
        self.assertEqual(result.source, "auto")
        self.assertIn("holding for crosscheck", result.reason)
        # The reason names the remaining seconds so an operator can tell at
        # a glance how long Symphony will wait.
        self.assertIn("1680s", result.reason)

    def test_falls_through_to_silent_after_grace_window(self):
        """Once the PR is older than ``crosscheck_wait_seconds``, treat
        crosscheck as not-connected and let silent fire as before."""
        result = evaluate_convergence(
            self._silent_ready_inputs(pr_age_seconds=1801)
        )
        self.assertTrue(result.converged)
        self.assertEqual(result.source, "silent")

    def test_falls_through_when_pr_age_unknown(self):
        """If runtime could not parse a PR ``created_at`` the wait cannot
        clock — fall through to silent so a parsing hiccup does not freeze
        every acceptance forever."""
        result = evaluate_convergence(self._silent_ready_inputs(pr_age_seconds=None))
        self.assertTrue(result.converged)
        self.assertEqual(result.source, "silent")

    def test_wait_disabled_by_zero_keeps_legacy_behavior(self):
        """``crosscheck_wait_seconds=0`` is the back-compat escape: legacy
        configs and tests that never set the field must keep falling
        through to silent immediately."""
        result = evaluate_convergence(
            self._silent_ready_inputs(crosscheck_wait_seconds=0, pr_age_seconds=10)
        )
        self.assertTrue(result.converged)
        self.assertEqual(result.source, "silent")

    def test_crosscheck_verdict_short_circuits_wait(self):
        """When crosscheck DID post a verdict, the wait window is moot —
        the crosscheck branch handles it directly."""
        v = ReviewVerdict(
            verdict=CrosscheckVerdict.APPROVE,
            source="comment",
            created_at=datetime(2026, 5, 30, tzinfo=timezone.utc),
            sha="head1",
        )
        result = evaluate_convergence(
            self._silent_ready_inputs(
                crosscheck_verdict=v,
                pr_age_seconds=10,
                head_sha="head1",
                last_commit_at=datetime(2026, 5, 29, tzinfo=timezone.utc),
            )
        )
        self.assertTrue(result.converged)
        self.assertEqual(result.source, "crosscheck")

    def test_review_source_crosscheck_ignores_wait(self):
        """In ``crosscheck`` mode the absence of a verdict is already a
        ``not converged`` reason on its own. The wait window only matters
        for ``auto`` mode, which is the only mode that can fall through."""
        result = evaluate_convergence(
            ConvergenceInputs(
                review_source="crosscheck",
                crosscheck_verdict=None,
                pr_age_seconds=10,
                crosscheck_wait_seconds=1800,
            )
        )
        self.assertFalse(result.converged)
        self.assertEqual(result.source, "crosscheck")

    def test_review_source_none_ignores_wait(self):
        """``none`` says 'I do not use crosscheck'; the wait window must
        not stall a user who explicitly opted out of code review."""
        result = evaluate_convergence(
            ConvergenceInputs(
                review_source="none",
                crosscheck_verdict=None,
                has_new_feedback=False,
                ci_green=True,
                pr_turn_advancing=False,
                quiet_for_seconds=400,
                quiet_period_seconds=300,
                pr_age_seconds=10,
                crosscheck_wait_seconds=1800,
            )
        )
        self.assertTrue(result.converged)
        self.assertEqual(result.source, "silent")


class AcceptanceVerdictModelTests(unittest.TestCase):
    def test_defaults(self):
        v = AcceptanceVerdict(overall="uncertain")
        self.assertEqual(v.checks, ())
        self.assertEqual(v.touched_sensitive_paths, ())


class AcceptanceJudgeSystemPromptTests(unittest.TestCase):
    """Lock in the judge's scope boundary at the prompt layer.

    These assertions are intentionally specific: if a future edit drifts the
    prompt into code-review territory or weakens the guard-rail wording, the
    test fails and surfaces the regression in review rather than at runtime.
    """

    def test_declares_scope_as_requirements_not_code_quality(self):
        prompt = ACCEPTANCE_JUDGE_SYSTEM_PROMPT.lower()
        # Affirms the in-scope frame.
        self.assertIn("requirements done item by item", prompt)
        self.assertIn("acceptance judge", prompt)
        # Names the boundary explicitly so a reader cannot miss it.
        self.assertIn("out of scope", prompt)
        self.assertIn("code review", prompt)

    def test_excludes_code_quality_dimensions_by_name(self):
        prompt = ACCEPTANCE_JUDGE_SYSTEM_PROMPT.lower()
        for banned_dimension in (
            "code style",
            "naming",
            "refactor",
            "latent bug",
            "performance",
        ):
            self.assertIn(
                banned_dimension,
                prompt,
                f"prompt must name '{banned_dimension}' as out of scope",
            )

    def test_enforces_guard_rails_and_output_contract(self):
        prompt = ACCEPTANCE_JUDGE_SYSTEM_PROMPT
        # Guard rails force escalation regardless of confidence.
        self.assertIn("SPEC.md", prompt)
        self.assertIn("migrations/", prompt)
        self.assertIn(".github/**", prompt)
        # Output shape mirrors AcceptanceVerdict so the runner can parse it.
        for key in ("overall", "checks", "touched_sensitive_paths", "summary_for_human"):
            self.assertIn(f'"{key}"', prompt)
        # The judge must not propose side effects.
        self.assertIn("Do not propose merging", prompt)


if __name__ == "__main__":
    unittest.main()
