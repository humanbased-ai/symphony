"""GitHub PR event dataclasses used by the polling feedback loop."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PRCommentEvent:
    """A new review-level inline comment on a pull request."""
    pr_number: int
    pr_head_branch: str
    pr_head_sha: str
    comment_body: str
    comment_author: str
    comment_id: int
    repo_owner: str
    repo_name: str


@dataclass(frozen=True)
class PRReviewEvent:
    """A pull_request_review submission (changes_requested or approved)."""
    pr_number: int
    pr_head_branch: str
    review_state: str   # "changes_requested" | "approved"
    review_body: str
    reviewer: str
    repo_owner: str
    repo_name: str


@dataclass(frozen=True)
class PRClosedEvent:
    """A pull request was closed (merged or abandoned)."""
    pr_number: int
    pr_head_branch: str
    merged: bool
    repo_owner: str
    repo_name: str


@dataclass(frozen=True)
class PROpenedEvent:
    """A pull request was opened."""
    pr_number: int
    pr_head_branch: str
    repo_owner: str
    repo_name: str


GitHubEvent = PRCommentEvent | PRReviewEvent | PRClosedEvent | PROpenedEvent
