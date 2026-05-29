"""Acceptance gate — decide WHEN a PR has converged enough for final acceptance.

The acceptance agent answers "did it do the right thing" (the original issue,
checked item by item) — distinct from code review, which answers "is the code
good". This module is the pure, I/O-free core that decides *when* the agent
should run: parsing the code-review verdict that gates it and evaluating
convergence. The runtime layer owns the side effects (dispatching the judge,
posting comments, transitioning the tracker, and — in a later phase — merging).

Convergence is pluggable on the review source (see ``AcceptanceConfig``):

- **crosscheck branch** — an external AI reviewer (``@motivation-labs/crosscheck``)
  posts a ``[crosscheck]`` comment ending in ``VERDICT: APPROVE | NEEDS WORK |
  BLOCK``. We converge only when the latest verdict is ``APPROVE``, it is not
  stale relative to the last commit, no auto-fix PR is still open, and a quiet
  period has elapsed.
- **silent branch** — no external reviewer; we converge when Symphony itself has
  gone quiet: no new feedback this tick, CI green, the PR-turn counter is not
  still advancing, and a quiet period has elapsed.

Either way, both ultimately hand off to the same acceptance judge.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from fnmatch import fnmatch
from typing import Any, Iterable, Mapping, Sequence


# Marker crosscheck stamps into the PR comments it posts. Detection is
# case-insensitive and substring-based so brand customization (e.g. a custom
# service_name wrapped in brackets) still matches the default.
CROSSCHECK_COMMENT_MARKER = "[crosscheck]"
# crosscheck ends every review with a line like ``VERDICT: APPROVE``.
_VERDICT_LINE = re.compile(r"VERDICT:\s*(APPROVE|NEEDS WORK|BLOCK)", re.IGNORECASE)


class CrosscheckVerdict(Enum):
    """The three verdicts crosscheck can emit."""

    APPROVE = "APPROVE"
    NEEDS_WORK = "NEEDS WORK"
    BLOCK = "BLOCK"


@dataclass(frozen=True)
class ReviewVerdict:
    """A parsed code-review verdict bound to its provenance.

    ``created_at`` is the comment timestamp (crosscheck-comment source) and is
    None when the verdict came from the ndjson log; ``sha`` is the reviewed
    commit (log source) and is None for the comment source. Convergence uses
    whichever provenance is available to confirm the verdict is not stale.
    """

    verdict: CrosscheckVerdict
    source: str  # "comment" | "log"
    created_at: datetime | None = None
    sha: str | None = None


@dataclass(frozen=True)
class ConvergenceResult:
    converged: bool
    source: str  # "crosscheck" | "silent" | "disabled"
    reason: str


@dataclass(frozen=True)
class AcceptanceCheck:
    requirement: str
    status: str  # "met" | "unmet" | "cannot_tell"
    evidence: str = ""
    confidence: float = 0.0


@dataclass(frozen=True)
class AcceptanceVerdict:
    """Structured output of the acceptance judge.

    ``overall`` is one of ``pass`` / ``fail`` / ``uncertain``. The runtime maps
    this plus ``touched_sensitive_paths`` and ``confidence`` to an action
    (Phase 1: always escalate to a human).
    """

    overall: str
    checks: tuple[AcceptanceCheck, ...] = field(default_factory=tuple)
    touched_sensitive_paths: tuple[str, ...] = field(default_factory=tuple)
    confidence: float = 0.0
    summary_for_human: str = ""


# --------------------------------------------------------------------------- #
# Verdict parsing
# --------------------------------------------------------------------------- #


def _coerce_dt(value: Any) -> datetime | None:
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


def parse_verdict_word(body: str) -> CrosscheckVerdict | None:
    """Extract the VERDICT word from a review body, or None if absent."""
    match = _VERDICT_LINE.search(body or "")
    if not match:
        return None
    token = match.group(1).strip().upper()
    if token == "APPROVE":
        return CrosscheckVerdict.APPROVE
    if token == "BLOCK":
        return CrosscheckVerdict.BLOCK
    return CrosscheckVerdict.NEEDS_WORK


def parse_crosscheck_verdict(comments: Iterable[Mapping[str, Any]]) -> ReviewVerdict | None:
    """Return the latest crosscheck verdict found in PR issue comments.

    ``comments`` are GitHub issue-comment dicts (``body``, ``created_at``).
    Comments are scanned newest-first by ``created_at`` so the most recent
    ``[crosscheck]`` comment carrying a ``VERDICT:`` line wins. Returns None
    when no crosscheck verdict comment is present.
    """
    candidates: list[tuple[datetime, ReviewVerdict]] = []
    for comment in comments:
        body = str(comment.get("body") or "")
        if CROSSCHECK_COMMENT_MARKER.lower() not in body.lower():
            continue
        verdict = parse_verdict_word(body)
        if verdict is None:
            continue
        created = _coerce_dt(comment.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc)
        candidates.append((created, ReviewVerdict(verdict=verdict, source="comment", created_at=created)))
    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0])
    return candidates[-1][1]


def parse_crosscheck_log_verdict(log_text: str, pr_number: int) -> ReviewVerdict | None:
    """Parse the latest ``review_complete`` verdict for ``pr_number`` from an
    ndjson crosscheck log, binding it to the sha from the matching
    ``pr_received`` event. Returns None when the log has no verdict for the PR.

    This is the same-host fallback for ``parse_crosscheck_verdict``; it gives a
    precise sha binding that the comment path cannot.
    """
    last_sha_for_pr: str | None = None
    found: ReviewVerdict | None = None
    for line in log_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(event, dict):
            continue
        if int(event.get("pr") or 0) != pr_number:
            continue
        kind = event.get("event")
        if kind == "pr_received":
            sha = event.get("sha")
            last_sha_for_pr = str(sha) if sha else last_sha_for_pr
        elif kind == "review_complete":
            verdict = parse_verdict_word(f"VERDICT: {event.get('verdict') or ''}")
            if verdict is None:
                continue
            found = ReviewVerdict(
                verdict=verdict,
                source="log",
                created_at=_coerce_dt(event.get("ts")),
                sha=last_sha_for_pr,
            )
    return found


# --------------------------------------------------------------------------- #
# Guard rails
# --------------------------------------------------------------------------- #


def detect_sensitive_paths(
    changed_files: Sequence[str],
    guard_patterns: Sequence[str],
) -> tuple[str, ...]:
    """Return the changed files matching any guard glob pattern.

    A non-empty result forces human escalation regardless of the judge's
    confidence — touching SPEC.md, migrations, CI config, or secrets is too
    costly to get wrong autonomously.
    """
    hits: list[str] = []
    for path in changed_files:
        normalized = str(path).strip()
        if not normalized:
            continue
        for pattern in guard_patterns:
            if _path_matches(normalized, pattern):
                hits.append(normalized)
                break
    return tuple(hits)


def _path_matches(path: str, pattern: str) -> bool:
    # fnmatch treats ``*`` as crossing ``/``; for "**/x/**" style patterns we
    # also match on any path segment so "a/migrations/b.sql" is caught.
    if fnmatch(path, pattern):
        return True
    if pattern.startswith("**/") and pattern.endswith("/**"):
        middle = pattern[3:-3]
        segments = path.split("/")
        return middle in segments
    return False


# --------------------------------------------------------------------------- #
# Convergence
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ConvergenceInputs:
    """Everything the convergence decision needs, gathered by the runtime."""

    review_source: str  # "auto" | "crosscheck" | "none"
    head_sha: str | None = None
    crosscheck_verdict: ReviewVerdict | None = None
    last_commit_at: datetime | None = None
    has_open_autofix_pr: bool = False
    has_new_feedback: bool = False
    ci_green: bool = True
    pr_turn_advancing: bool = False
    quiet_for_seconds: float = 0.0
    quiet_period_seconds: float = 0.0


def evaluate_convergence(inputs: ConvergenceInputs) -> ConvergenceResult:
    """Decide whether a PR has converged enough to run final acceptance.

    Routes to the crosscheck branch when a verdict is present (or required), and
    to the silent branch otherwise. The decision is deliberately conservative:
    judging early is far more dangerous than judging late.
    """
    quiet_ok = inputs.quiet_for_seconds >= inputs.quiet_period_seconds

    use_crosscheck = inputs.review_source == "crosscheck" or (
        inputs.review_source == "auto" and inputs.crosscheck_verdict is not None
    )

    if use_crosscheck:
        verdict = inputs.crosscheck_verdict
        if verdict is None:
            return ConvergenceResult(False, "crosscheck", "no crosscheck verdict yet")
        if verdict.verdict is not CrosscheckVerdict.APPROVE:
            return ConvergenceResult(
                False, "crosscheck", f"latest verdict is {verdict.verdict.value}, not APPROVE"
            )
        if not _verdict_covers_head(verdict, inputs.head_sha, inputs.last_commit_at):
            return ConvergenceResult(False, "crosscheck", "approval is stale relative to latest commit")
        if inputs.has_open_autofix_pr:
            return ConvergenceResult(False, "crosscheck", "an auto-fix PR is still open")
        if not quiet_ok:
            return ConvergenceResult(False, "crosscheck", "quiet period has not elapsed")
        return ConvergenceResult(True, "crosscheck", "crosscheck APPROVE on current head; converged")

    # Silent branch — no external reviewer.
    if inputs.has_new_feedback:
        return ConvergenceResult(False, "silent", "new feedback this tick")
    if not inputs.ci_green:
        return ConvergenceResult(False, "silent", "CI not green")
    if inputs.pr_turn_advancing:
        return ConvergenceResult(False, "silent", "PR-turn counter still advancing")
    if not quiet_ok:
        return ConvergenceResult(False, "silent", "quiet period has not elapsed")
    return ConvergenceResult(True, "silent", "Symphony quiesced; CI green; converged")


def _verdict_covers_head(
    verdict: ReviewVerdict,
    head_sha: str | None,
    last_commit_at: datetime | None,
) -> bool:
    """True when the verdict applies to the current PR head.

    Prefer the precise sha binding (ndjson log source). Fall back to comparing
    the comment timestamp against the last commit time. If neither provenance is
    available, treat as not-covering (stay conservative).
    """
    if verdict.sha is not None and head_sha is not None:
        return verdict.sha == head_sha
    if verdict.created_at is not None and last_commit_at is not None:
        return verdict.created_at >= last_commit_at
    return False
