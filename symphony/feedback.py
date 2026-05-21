"""Human Feedback Gate — comment signal detection for the In Review state."""
from __future__ import annotations

import re
from enum import Enum


class FeedbackSignal(Enum):
    APPROVE = "approve"
    CHANGE_REQUEST = "change_request"
    CLOSE = "close"


_APPROVE_PATTERNS = re.compile(
    r"(?:\b(?:lgtm|looks good(?: to me)?|approved?|ship it|merge it|good to go)\b|👍|✅)",
    re.IGNORECASE,
)

_CHANGE_PATTERNS = re.compile(
    r"(?:\b(?:please (?:change|fix|update|revise|redo)|change request|needs? (?:changes?|work|revision)|request changes?)\b|fix:|change:)",
    re.IGNORECASE,
)

_CLOSE_PATTERNS = re.compile(
    r"\b(close[ds]?|won'?t fix|wontfix|cancel(led|ed)?|drop( this)?|not needed|out of scope)\b",
    re.IGNORECASE,
)


def detect_feedback_signal(comments: list[str]) -> FeedbackSignal | None:
    """Scan comments for the first human feedback signal.

    Comments are expected in the format "Author: body" as returned by
    LinearClient.fetch_issue_comments(). Only the body portion is matched.
    Returns the first signal found in reverse order (most recent first), or
    None if no signal is detected.
    """
    for comment in reversed(comments):
        body = comment.split(": ", 1)[1] if ": " in comment else comment
        if _CLOSE_PATTERNS.search(body):
            return FeedbackSignal.CLOSE
        if _CHANGE_PATTERNS.search(body):
            return FeedbackSignal.CHANGE_REQUEST
        if _APPROVE_PATTERNS.search(body):
            return FeedbackSignal.APPROVE
    return None
