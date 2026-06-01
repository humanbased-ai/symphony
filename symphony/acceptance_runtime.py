"""Acceptance gate runtime — gather inputs, dispatch the judge, post the verdict.

This module is the I/O glue between the pure decision core in
``symphony.acceptance`` and Symphony's PR polling loop. ``runtime.py`` calls
``maybe_run_acceptance()`` at the tail of every PR-poll tick; everything that
talks to GitHub or spawns a judge subprocess lives here, not in the core.

Phase 1 contract: only judge + comment + escalate. No merge, no bounce-back.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from symphony.acceptance import (
    ACCEPTANCE_JUDGE_SYSTEM_PROMPT,
    AcceptanceCheck,
    AcceptanceVerdict,
    ConvergenceInputs,
    ConvergenceResult,
    CrosscheckVerdict,
    ReviewVerdict,
    detect_sensitive_paths,
    evaluate_convergence,
    parse_crosscheck_verdict,
)
from symphony.config import AcceptanceConfig
from symphony.tracker.models import Issue


LOGGER = logging.getLogger(__name__)

# Same marker the rest of runtime.py uses; keeps a token-shared bot's own comments
# from being re-classified as feedback on the next poll tick.
SYMPHONY_BOT_MARKER = "<!-- symphony -->"
DEFAULT_JUDGE_TIMEOUT_MS = 600_000
# Trim the diff before handing it to the judge; very large PRs would otherwise
# burn judge tokens on lines no human reviewer reads either.
JUDGE_DIFF_TRUNCATE = 60_000


@dataclass(frozen=True)
class ConvergenceSnapshot:
    """Per-branch state the runtime carries between ticks.

    ``last_feedback_at`` clocks ``quiet_for_seconds``; ``pr_turns_observed`` is
    compared against the live ``_pr_turns`` counter to fill ``pr_turn_advancing``.
    """

    last_feedback_at: datetime
    pr_turns_observed: int


__all__ = (
    "ClaudeCodeJudgeRunner",
    "ConvergenceSnapshot",
    "JUDGE_DIFF_TRUNCATE",
    "dispatch_acceptance_judge",
    "gather_convergence_inputs",
    "parse_acceptance_verdict",
    "render_verdict_comment",
    "render_judge_user_prompt",
    "extract_changed_files_from_diff",
)


# --------------------------------------------------------------------------- #
# Convergence input gathering
# --------------------------------------------------------------------------- #


def gather_convergence_inputs(
    *,
    github_client: Any,
    pr_number: int,
    config: AcceptanceConfig,
    pr_turns: int,
    snapshot: ConvergenceSnapshot | None,
    now: datetime,
    saw_new_feedback_this_tick: bool,
) -> tuple[ConvergenceInputs, dict[str, Any]]:
    """Snapshot every field ``evaluate_convergence`` needs from live GitHub state.

    Returns ``(inputs, raw)`` where ``raw`` exposes ``pr`` data and ``comments``
    for the caller to reuse (so the judge dispatch path does not re-fetch them).
    """
    pr = github_client.get_pr(pr_number) or {}
    head_sha = ((pr.get("head") or {}).get("sha")) or None
    # PR-level ``updated_at`` is the safest cross-version proxy for last commit
    # time without an extra API round-trip. It moves on PR description edits
    # too, which only makes the gate more conservative (stale-detection fires
    # more readily) — acceptable since judging early is the bigger risk.
    last_commit_at = _coerce_datetime(pr.get("updated_at"))

    comments = github_client.list_pr_issue_comments(pr_number) or []
    crosscheck = parse_crosscheck_verdict(comments)

    failed = github_client.get_pr_failed_check_runs(pr_number) or []
    ci_green = len(failed) == 0

    if snapshot is None:
        quiet_for = 0.0
    else:
        quiet_for = max(0.0, (now - snapshot.last_feedback_at).total_seconds())

    pr_turn_advancing = snapshot is not None and pr_turns > snapshot.pr_turns_observed

    inputs = ConvergenceInputs(
        review_source=config.review_source,
        head_sha=head_sha,
        crosscheck_verdict=crosscheck,
        last_commit_at=last_commit_at,
        # Phase 1 does not yet detect external auto-fix PRs (crosscheck's
        # cr-autofix branch name is not codified in Symphony's config). When
        # an auto-fix PR is open it almost always bumps PR turns or posts new
        # feedback within a tick, so ``has_new_feedback`` / ``pr_turn_advancing``
        # cover the case in practice.
        has_open_autofix_pr=False,
        has_new_feedback=saw_new_feedback_this_tick,
        ci_green=ci_green,
        pr_turn_advancing=pr_turn_advancing,
        quiet_for_seconds=quiet_for,
        quiet_period_seconds=float(config.quiet_period_seconds),
    )
    return inputs, {"pr": pr, "comments": comments, "head_sha": head_sha}


def _coerce_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value.strip():
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


# --------------------------------------------------------------------------- #
# Judge dispatch
# --------------------------------------------------------------------------- #


class ClaudeCodeJudgeRunner:
    """One-shot ``claude`` CLI invocation for the acceptance judge.

    Distinct from ``symphony.agents.claude_code.ClaudeCodeRunner``: the judge
    runner is no-session, no-workspace-writes, and only extracts the final
    assistant text from the stream-json ``result`` event. It uses a temp dir
    as the cwd because the CLI requires one, but the judge is forbidden from
    writing files there (the prompt constrains it to JSON output).
    """

    def __init__(
        self,
        *,
        command: str = "claude",
        model: str | None = None,
        turn_timeout_ms: int = DEFAULT_JUDGE_TIMEOUT_MS,
    ) -> None:
        self.command = command
        self.model = model
        self.turn_timeout_ms = turn_timeout_ms

    async def judge(self, system_prompt: str, user_prompt: str) -> str | None:
        """Return the final assistant text, or None on error / timeout."""
        cmd = [
            self.command,
            "--print",
            "--verbose",
            "--output-format", "stream-json",
            "--permission-mode", "default",
            "--append-system-prompt", system_prompt,
        ]
        if self.model:
            cmd += ["--model", self.model]

        with tempfile.TemporaryDirectory(prefix="symphony-acceptance-") as cwd:
            try:
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                    env=os.environ.copy(),
                    limit=4 * 1024 * 1024,
                )
            except (FileNotFoundError, OSError) as exc:
                LOGGER.warning("acceptance_judge_launch_failed: %s", exc)
                return None

            if process.stdin is None:
                process.kill()
                await process.wait()
                return None

            process.stdin.write(user_prompt.encode())
            await process.stdin.drain()
            process.stdin.close()

            stderr_task = asyncio.create_task(_drain(process.stderr))
            try:
                final_text = await asyncio.wait_for(
                    self._read_final_text(process),
                    timeout=self.turn_timeout_ms / 1000,
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                LOGGER.warning("acceptance_judge_timeout")
                return None
            finally:
                await asyncio.gather(stderr_task, return_exceptions=True)
            return final_text

    @staticmethod
    async def _read_final_text(process: asyncio.subprocess.Process) -> str | None:
        if process.stdout is None:
            return None
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            try:
                event = json.loads(line.decode())
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if event.get("type") == "result":
                if event.get("is_error"):
                    return None
                text = event.get("result", "")
                return text if isinstance(text, str) and text else None
        return None


async def _drain(stream: asyncio.StreamReader | None) -> None:
    if stream is None:
        return
    while True:
        line = await stream.readline()
        if not line:
            break


async def dispatch_acceptance_judge(
    judge: ClaudeCodeJudgeRunner,
    *,
    issue: Issue,
    diff: str,
    crosscheck_verdict: ReviewVerdict | None = None,
) -> AcceptanceVerdict | None:
    """Run the judge and return a parsed verdict, or None on any failure."""
    user_prompt = render_judge_user_prompt(issue, diff, crosscheck_verdict)
    raw = await judge.judge(ACCEPTANCE_JUDGE_SYSTEM_PROMPT, user_prompt)
    if not raw:
        return None
    return parse_acceptance_verdict(raw)


def render_judge_user_prompt(
    issue: Issue,
    diff: str,
    crosscheck_verdict: ReviewVerdict | None,
) -> str:
    """Build the user-message payload the judge sees alongside the system prompt.

    Keeps the layout predictable so the judge can anchor each requirement to
    the diff or the issue description; the system prompt already tells it what
    shape to return.
    """
    trimmed_diff = (diff or "").strip()
    if len(trimmed_diff) > JUDGE_DIFF_TRUNCATE:
        trimmed_diff = trimmed_diff[:JUDGE_DIFF_TRUNCATE] + "\n\n[... diff truncated ...]"

    description = (issue.description or "").strip() or "(no description on issue)"
    parts = [
        "# Original issue",
        "",
        f"**{issue.identifier}** — {issue.title}",
        "",
        description,
        "",
        "# Diff under review",
        "",
        "```diff",
        trimmed_diff or "(no diff available)",
        "```",
    ]
    if crosscheck_verdict is not None:
        verdict_label = crosscheck_verdict.verdict.value
        source = crosscheck_verdict.source
        parts.extend([
            "",
            "# Prior code-review verdict",
            "",
            f"crosscheck verdict (source={source}): **{verdict_label}**",
            "",
            "Code review has signed off on code quality. Re-check whether the",
            "diff actually satisfies the issue's requirements; do not re-do",
            "code review.",
        ])
    parts.extend([
        "",
        "---",
        "",
        "Decide whether this PR satisfies the issue. Return the JSON object",
        "exactly as specified in your instructions, with no prose around it.",
    ])
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# Verdict parsing
# --------------------------------------------------------------------------- #


_VALID_OVERALL = {"pass", "fail", "uncertain"}
_VALID_STATUS = {"met", "unmet", "cannot_tell"}


def parse_acceptance_verdict(raw: str) -> AcceptanceVerdict | None:
    """Parse the judge's response into ``AcceptanceVerdict``.

    Tolerates surrounding code fences / prose by extracting the first balanced
    JSON object. Returns None when ``overall`` is missing or unrecognized; the
    caller treats None as "judge produced nothing usable" and skips posting.
    """
    text = (raw or "").strip()
    blob = _extract_json_object(text)
    if blob is None:
        return None
    try:
        data = json.loads(blob)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    overall = str(data.get("overall") or "").strip().lower()
    if overall not in _VALID_OVERALL:
        return None

    checks: list[AcceptanceCheck] = []
    raw_checks = data.get("checks")
    if isinstance(raw_checks, list):
        for entry in raw_checks:
            if not isinstance(entry, dict):
                continue
            status = str(entry.get("status") or "").strip().lower()
            if status not in _VALID_STATUS:
                status = "cannot_tell"
            try:
                confidence = float(entry.get("confidence") or 0.0)
            except (TypeError, ValueError):
                confidence = 0.0
            confidence = max(0.0, min(1.0, confidence))
            requirement = str(entry.get("requirement") or "").strip()
            if not requirement:
                continue
            checks.append(AcceptanceCheck(
                requirement=requirement,
                status=status,
                evidence=str(entry.get("evidence") or "").strip(),
                confidence=confidence,
            ))

    touched_raw = data.get("touched_sensitive_paths") or []
    if isinstance(touched_raw, list):
        touched = tuple(str(p) for p in touched_raw if str(p).strip())
    else:
        touched = ()

    try:
        confidence = float(data.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    summary = str(data.get("summary_for_human") or "").strip()
    return AcceptanceVerdict(
        overall=overall,
        checks=tuple(checks),
        touched_sensitive_paths=touched,
        confidence=confidence,
        summary_for_human=summary,
    )


def _extract_json_object(text: str) -> str | None:
    """Return the largest balanced ``{...}`` substring, or None if none found.

    Tolerates judges that wrap output in ```json ... ``` fences or prepend a
    sentence like "Here is the JSON:". A naive ``text.find("{")`` would fail
    on objects nested inside the prose; we walk the string tracking depth.
    """
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for idx in range(start, len(text)):
        ch = text[idx]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:idx + 1]
    return None


# --------------------------------------------------------------------------- #
# Verdict rendering
# --------------------------------------------------------------------------- #


def render_verdict_comment(
    verdict: AcceptanceVerdict,
    *,
    escalation: bool = True,
    convergence: ConvergenceResult | None = None,
    merged: bool = False,
    auto_merge_skip_reason: str | None = None,
) -> str:
    """Render the judge verdict as a Markdown PR comment for human reviewers.

    The first line is the ``SYMPHONY_BOT_MARKER`` so the poller's own filtering
    skips it on the next tick rather than re-classifying it as user feedback.

    ``merged`` reflects the actual auto-merge outcome — when True the comment
    celebrates the auto-merge and skips the human-escalation tail. When False
    and ``auto_merge_skip_reason`` is set, the comment names the specific
    reason auto-merge did not fire, so the gate's behavior stays auditable
    from the PR thread alone without digging into Symphony logs.
    """
    overall_label = verdict.overall.upper()
    lines: list[str] = [
        SYMPHONY_BOT_MARKER,
        f"## Acceptance verdict: **{overall_label}**",
        "",
        verdict.summary_for_human or "_(judge returned no summary)_",
        "",
    ]
    if verdict.checks:
        lines.append("### Per-requirement checks")
        lines.append("")
        for check in verdict.checks:
            head = f"- `[{check.status}]` {check.requirement}"
            if check.confidence:
                head += f" _(confidence {check.confidence:.2f})_"
            lines.append(head)
            if check.evidence:
                lines.append(f"  - Evidence: {check.evidence}")
        lines.append("")
    if verdict.touched_sensitive_paths:
        lines.append("### Guard-rail paths touched (forces escalation)")
        lines.append("")
        for path in verdict.touched_sensitive_paths:
            lines.append(f"- `{path}`")
        lines.append("")
    if convergence is not None:
        lines.append(f"_Convergence source: {convergence.source} — {convergence.reason}_")
        lines.append("")
    lines.append(f"_Overall confidence: {verdict.confidence:.2f}_")
    if merged:
        lines.append("")
        lines.append(
            "**Auto-merged by acceptance gate** — pass verdict at confidence "
            "at or above threshold and no guard-rail paths touched. The PR was "
            "squash-merged via GitHub's merge API."
        )
    elif escalation:
        lines.append("")
        if auto_merge_skip_reason:
            lines.append(
                f"**Auto-merge did not fire** — {auto_merge_skip_reason}. "
                "A human reviewer makes the final call."
            )
        else:
            lines.append(
                "**Symphony will not auto-merge this PR.** A human reviewer "
                "makes the final call."
            )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Diff helpers
# --------------------------------------------------------------------------- #


_DIFF_FILE_HEADER = re.compile(r"^diff --git a/(?P<a>.+?) b/(?P<b>.+)$", re.MULTILINE)


def extract_changed_files_from_diff(diff: str) -> tuple[str, ...]:
    """Pull the changed file paths out of a unified ``git diff``.

    Both the ``a/`` and ``b/`` sides are emitted because guard-path detection
    cares about the post-rename path while file deletes carry only the ``a/``
    side. Duplicates are removed but ordering is preserved.
    """
    if not diff:
        return ()
    seen: dict[str, None] = {}
    for match in _DIFF_FILE_HEADER.finditer(diff):
        for key in ("a", "b"):
            path = match.group(key).strip()
            if path and path != "/dev/null" and path not in seen:
                seen[path] = None
    return tuple(seen.keys())


# --------------------------------------------------------------------------- #
# Orchestrator: end-to-end "should we judge, and if so post the verdict"
# --------------------------------------------------------------------------- #


async def maybe_run_acceptance(
    *,
    github_client: Any,
    judge: ClaudeCodeJudgeRunner,
    config: AcceptanceConfig,
    issue: Issue,
    branch: str,
    pr_number: int,
    pr_turns: int,
    snapshot: ConvergenceSnapshot | None,
    now: datetime,
    saw_new_feedback_this_tick: bool,
    already_judged_sha: str | None,
) -> tuple[ConvergenceSnapshot, str | None, AcceptanceVerdict | None, ConvergenceResult | None]:
    """Run the acceptance check for one PR-poll tick.

    Returns ``(new_snapshot, judged_sha, verdict, convergence)`` so the caller
    can update its per-branch state. ``judged_sha`` is non-None only when this
    tick actually posted a verdict; the caller uses it to suppress re-judging
    the same head sha on subsequent ticks.
    """
    if not config.enabled or github_client is None:
        return (snapshot or _initial_snapshot(now, pr_turns), None, None, None)

    inputs, raw = await asyncio.to_thread(
        gather_convergence_inputs,
        github_client=github_client,
        pr_number=pr_number,
        config=config,
        pr_turns=pr_turns,
        snapshot=snapshot,
        now=now,
        saw_new_feedback_this_tick=saw_new_feedback_this_tick,
    )
    result = evaluate_convergence(inputs)
    LOGGER.debug(
        "acceptance_convergence pr=#%d issue=%s converged=%s source=%s reason=%s",
        pr_number, issue.identifier, result.converged, result.source, result.reason,
    )

    last_feedback_at = (
        now if saw_new_feedback_this_tick
        else (snapshot.last_feedback_at if snapshot else now)
    )
    new_snapshot = ConvergenceSnapshot(
        last_feedback_at=last_feedback_at,
        pr_turns_observed=pr_turns,
    )

    if not result.converged:
        return (new_snapshot, None, None, result)

    head_sha = raw.get("head_sha")
    if head_sha and already_judged_sha == head_sha:
        LOGGER.debug(
            "acceptance_already_judged pr=#%d sha=%s — skipping re-judge",
            pr_number, head_sha,
        )
        return (new_snapshot, None, None, result)

    diff = await asyncio.to_thread(github_client.get_pr_diff, pr_number)
    LOGGER.info(
        "acceptance_dispatch pr=#%d issue=%s source=%s",
        pr_number, issue.identifier, result.source,
    )
    verdict = await dispatch_acceptance_judge(
        judge,
        issue=issue,
        diff=diff,
        crosscheck_verdict=inputs.crosscheck_verdict,
    )
    if verdict is None:
        LOGGER.warning(
            "acceptance_judge_no_verdict pr=#%d issue=%s",
            pr_number, issue.identifier,
        )
        return (new_snapshot, None, None, result)

    # Guard-rail override: if the diff touches sensitive paths the judge may
    # have missed, force ``uncertain`` so humans see it. The judge's own
    # ``touched_sensitive_paths`` is merged in.
    changed_files = extract_changed_files_from_diff(diff)
    sensitive = detect_sensitive_paths(changed_files, config.guard_paths)
    if sensitive:
        merged_touched = tuple(dict.fromkeys((*verdict.touched_sensitive_paths, *sensitive)))
        if verdict.overall == "pass" or verdict.touched_sensitive_paths != merged_touched:
            verdict = dataclasses.replace(
                verdict,
                overall="uncertain" if verdict.overall == "pass" else verdict.overall,
                touched_sensitive_paths=merged_touched,
            )

    # Phase 2 auto-merge decision. Off by default — ``config.auto_merge`` must
    # be explicitly true. Even then we only fire on the safest combination:
    # pass + confidence at or above the threshold + zero touched guard paths.
    # GitHub branch protection / required reviews remain the final tripwire:
    # if ``merge_pr`` returns False (e.g. GH says CI is not green, reviews are
    # missing, or HEAD has advanced past ``head_sha``), we fall back to the
    # human-escalation comment path so nothing slips through silently.
    merged = False
    skip_reason: str | None = None
    if config.auto_merge:
        skip_reason = _auto_merge_skip_reason(verdict, config.confidence_threshold)
        if skip_reason is None:
            merged = await asyncio.to_thread(
                github_client.merge_pr,
                pr_number,
                sha=head_sha,
                merge_method="squash",
            )
            if not merged:
                # GitHub-side rejection (branch protection, stale sha, missing
                # reviews). Surface it on the PR rather than silently failing.
                skip_reason = "GitHub rejected the merge (branch protection, stale head sha, or missing required reviews)"

    body = render_verdict_comment(
        verdict,
        escalation=not merged,
        convergence=result,
        merged=merged,
        auto_merge_skip_reason=skip_reason if config.auto_merge and not merged else None,
    )
    posted = await asyncio.to_thread(github_client.post_pr_comment, pr_number, body)
    judged_sha = head_sha if posted else None
    if merged:
        LOGGER.info(
            "acceptance_auto_merged pr=#%d issue=%s confidence=%.2f sha=%s",
            pr_number, issue.identifier, verdict.confidence, head_sha,
        )
    if posted:
        LOGGER.info(
            "acceptance_verdict_posted pr=#%d issue=%s overall=%s confidence=%.2f merged=%s",
            pr_number, issue.identifier, verdict.overall, verdict.confidence, merged,
        )
    else:
        LOGGER.warning(
            "acceptance_verdict_post_failed pr=#%d issue=%s",
            pr_number, issue.identifier,
        )
    return (new_snapshot, judged_sha, verdict, result)


def _auto_merge_skip_reason(
    verdict: AcceptanceVerdict, confidence_threshold: float
) -> str | None:
    """Return why auto-merge should be skipped, or None when all four
    preconditions hold. Returning a string also gives the PR comment a
    human-readable explanation without forcing every caller to recompute it.
    """
    if verdict.overall != "pass":
        return f"verdict is {verdict.overall}, not pass"
    if verdict.confidence < confidence_threshold:
        return (
            f"overall confidence {verdict.confidence:.2f} is below the "
            f"configured threshold {confidence_threshold:.2f}"
        )
    if verdict.touched_sensitive_paths:
        return (
            "diff touches guard-rail paths "
            f"({', '.join(verdict.touched_sensitive_paths)})"
        )
    return None


def _initial_snapshot(now: datetime, pr_turns: int) -> ConvergenceSnapshot:
    return ConvergenceSnapshot(last_feedback_at=now, pr_turns_observed=pr_turns)
