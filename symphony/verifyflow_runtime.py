"""Runtime wiring for the VerifyFlow delivery-verification step (IN-569).

Phase 1 is **advisory-only**: once Crosscheck has approved the current head of
a PR, Symphony spawns ``vf step --pr <url>`` exactly once for that head SHA.
VerifyFlow checks out the PR, really executes probes against the acceptance
criteria of the linked Linear issue, keeps report + evidence on disk, and
posts/updates its own idempotent delivery-report comment on the PR. Symphony
only logs the machine-readable JSON line vf prints on stdout — it never
merges, never blocks, and never transitions Linear state on the verdict.

Deliberately independent of the ``acceptance`` subsystem (disabled by
default): Crosscheck = code review, VerifyFlow = delivery verification,
acceptance judge = off. See verifyflow's ``docs/symphony-integration-surface.md``
for the full contract.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

from .acceptance import CrosscheckVerdict, parse_crosscheck_verdict
from .config import VerifyflowConfig

LOGGER = logging.getLogger(__name__)

# (command, timeout_seconds) -> (exit_code | None on timeout, stdout_text)
SpawnFn = Callable[[list[str], int], Awaitable[tuple[int | None, str]]]


@dataclass(frozen=True)
class VerifyflowOutcome:
    """Result of one ``vf step`` spawn.

    ``head_sha`` is recorded by the caller so the same head is never verified
    twice — including after a failed run: an advisory step is logged, not
    retried in a hot loop; the next push re-arms it. ``exit_code`` is None on
    timeout. ``summary`` is vf's parsed stdout JSON line, when present.
    """

    head_sha: str
    exit_code: int | None
    summary: Mapping[str, Any] | None


async def _spawn_vf_step(cmd: list[str], timeout_seconds: int) -> tuple[int | None, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return None, ""
    if proc.returncode != 0:
        LOGGER.debug("vf step stderr: %s", stderr.decode("utf-8", "replace")[-2000:])
    return proc.returncode, stdout.decode("utf-8", "replace")


def _parse_summary_line(stdout: str) -> Mapping[str, Any] | None:
    """vf step prints exactly one JSON object line on stdout; tolerate noise."""
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


async def maybe_run_verifyflow(
    *,
    github_client: Any,
    config: VerifyflowConfig,
    branch: str,
    pr_number: int,
    already_run_sha: str | None,
    spawn: SpawnFn = _spawn_vf_step,
) -> VerifyflowOutcome | None:
    """Run ``vf step`` once per PR head SHA after Crosscheck approves it.

    Returns the outcome when a run happened (caller records ``head_sha`` for
    dedup), or None when this tick's conditions aren't met: no PR data, head
    already verified, or no fresh Crosscheck APPROVE. Timing is owned here —
    vf itself never gates on the review verdict, it only records it.
    """
    pr_data = await asyncio.to_thread(github_client.get_pr, pr_number)
    if not pr_data:
        return None
    head_sha = str((pr_data.get("head") or {}).get("sha") or "")
    pr_url = str(pr_data.get("html_url") or "")
    if not head_sha or not pr_url or head_sha == already_run_sha:
        return None

    comments = await asyncio.to_thread(github_client.list_pr_issue_comments, pr_number)
    review = parse_crosscheck_verdict(comments)
    if review is None or review.verdict is not CrosscheckVerdict.APPROVE:
        return None
    # Log-source verdicts are bound to the reviewed commit; skip a stale one.
    # Comment-source verdicts carry no sha — the newest-comment-wins parse is
    # the best freshness signal available there.
    if review.sha is not None and review.sha != head_sha:
        return None

    cmd = [
        config.command,
        "step",
        "--pr",
        pr_url,
        "--level",
        config.level,
        "--crosscheck-verdict",
        review.verdict.value,
    ]
    LOGGER.info(
        "VerifyFlow step on PR #%d (%s) @ %s", pr_number, branch, head_sha[:12],
    )
    exit_code, stdout = await spawn(cmd, config.timeout_seconds)
    summary = _parse_summary_line(stdout)

    if exit_code == 0 and summary is not None:
        LOGGER.info(
            "VerifyFlow verdict on PR #%d: %s (criteria=%s, report=%s, comment_posted=%s)",
            pr_number,
            summary.get("verdict"),
            summary.get("criteria"),
            summary.get("reportMarkdown"),
            summary.get("prCommentPosted"),
        )
    elif exit_code is None:
        LOGGER.warning(
            "VerifyFlow step timed out after %ds on PR #%d (%s).",
            config.timeout_seconds, pr_number, branch,
        )
    else:
        LOGGER.warning(
            "VerifyFlow step exited %s on PR #%d (%s) — advisory, not retried for this head.",
            exit_code, pr_number, branch,
        )
    return VerifyflowOutcome(head_sha=head_sha, exit_code=exit_code, summary=summary)
