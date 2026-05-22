"""Human Feedback Gate — comment signal detection for the In Review state."""
from __future__ import annotations

import subprocess
from enum import Enum

_SYSTEM_PROMPT = """\
You classify human feedback comments left on a code review issue.
Reply with exactly one word — no punctuation, no explanation:
  APPROVE         reviewer approves; changes are good, ready to merge or mark done
  CHANGE_REQUEST  reviewer wants changes made before the issue can be closed
  CLOSE           reviewer wants to close, cancel, or mark the issue won't-be-fixed
  NONE            no clear feedback signal in the comments
"""

_USER_TEMPLATE = """\
Comments (most recent last):
{comments}

Classification:"""


class ClassifyError(Exception):
    """Raised when the Anthropic API call fails (network error, auth error, etc.)."""


class FeedbackSignal(Enum):
    APPROVE = "approve"
    CHANGE_REQUEST = "change_request"
    CLOSE = "close"


_SIGNAL_MAP = {
    "APPROVE": FeedbackSignal.APPROVE,
    "CHANGE_REQUEST": FeedbackSignal.CHANGE_REQUEST,
    "CLOSE": FeedbackSignal.CLOSE,
}


def classify_feedback(comments: list[str]) -> FeedbackSignal | None:
    """Use Claude CLI to classify the feedback intent in a list of comments.

    Comments should be in "Author: body" format as returned by LinearClient.
    Returns None if no signal is detected or the CLI call fails.
    """
    if not comments:
        return None

    body_text = "\n".join(f"- {c}" for c in comments)
    user_content = _USER_TEMPLATE.format(comments=body_text)
    full_prompt = _SYSTEM_PROMPT.strip() + "\n\n" + user_content

    try:
        result = subprocess.run(
            ["claude", "-p", full_prompt],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise ClassifyError(result.stderr.strip() or "claude exited non-zero")
        label = result.stdout.strip().upper()
        return _SIGNAL_MAP.get(label)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        raise ClassifyError(str(exc)) from exc
