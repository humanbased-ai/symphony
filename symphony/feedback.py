"""Human Feedback Gate — comment signal detection for the In Review state."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

_ANTHROPIC_ENDPOINT = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"
_CLASSIFICATION_MODEL = "claude-haiku-4-5-20251001"
_MAX_TOKENS = 16

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


class FeedbackSignal(Enum):
    APPROVE = "approve"
    CHANGE_REQUEST = "change_request"
    CLOSE = "close"


_SIGNAL_MAP = {
    "APPROVE": FeedbackSignal.APPROVE,
    "CHANGE_REQUEST": FeedbackSignal.CHANGE_REQUEST,
    "CLOSE": FeedbackSignal.CLOSE,
}


def classify_feedback(comments: list[str], *, api_key: str, model: str = _CLASSIFICATION_MODEL) -> FeedbackSignal | None:
    """Use Claude to classify the feedback intent in a list of comments.

    Comments should be in "Author: body" format as returned by LinearClient.
    Returns None if no signal is detected or the API call fails.
    """
    if not comments:
        return None

    body_text = "\n".join(f"- {c}" for c in comments)
    user_content = _USER_TEMPLATE.format(comments=body_text)

    payload = json.dumps({
        "model": model,
        "max_tokens": _MAX_TOKENS,
        "system": _SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_content}],
    }).encode()

    req = urllib.request.Request(
        _ANTHROPIC_ENDPOINT,
        data=payload,
        headers={
            "x-api-key": api_key,
            "anthropic-version": _ANTHROPIC_VERSION,
            "content-type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            response = json.loads(resp.read().decode())
        label = response["content"][0]["text"].strip().upper()
        return _SIGNAL_MAP.get(label)
    except (urllib.error.URLError, KeyError, json.JSONDecodeError, IndexError):
        return None
