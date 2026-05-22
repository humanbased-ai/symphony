"""GitHub webhook payload parsing and HMAC-SHA256 verification."""
from __future__ import annotations

import hashlib
import hmac
import json
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


GitHubEvent = PRCommentEvent | PRReviewEvent | PRClosedEvent


def verify_signature(payload_bytes: bytes, signature: str, secret: str) -> bool:
    """Verify X-Hub-Signature-256 header (format: 'sha256=<hex>')."""
    if not signature:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), payload_bytes, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def parse_github_payload(body: bytes, event_type: str) -> GitHubEvent | None:
    """Parse a GitHub webhook payload. Returns None for unrecognized / irrelevant events."""
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None

    repo = payload.get("repository") or {}
    repo_owner = (repo.get("owner") or {}).get("login", "")
    repo_name = repo.get("name", "")

    if event_type == "pull_request_review_comment":
        return _parse_review_comment(payload, repo_owner, repo_name)
    if event_type == "pull_request_review":
        return _parse_pr_review(payload, repo_owner, repo_name)
    if event_type == "pull_request":
        return _parse_pr_closed(payload, repo_owner, repo_name)
    return None


# ---------------------------------------------------------------------------
# Internal parsers
# ---------------------------------------------------------------------------

def _parse_review_comment(payload: dict, repo_owner: str, repo_name: str) -> PRCommentEvent | None:
    if payload.get("action") != "created":
        return None
    comment = payload.get("comment") or {}
    pr = payload.get("pull_request") or {}
    head = pr.get("head") or {}
    body = comment.get("body", "")
    if not isinstance(body, str) or not body.strip():
        return None
    return PRCommentEvent(
        pr_number=int(pr.get("number") or 0),
        pr_head_branch=str(head.get("ref") or ""),
        pr_head_sha=str(head.get("sha") or ""),
        comment_body=body.strip(),
        comment_author=str((comment.get("user") or {}).get("login") or "unknown"),
        comment_id=int(comment.get("id") or 0),
        repo_owner=repo_owner,
        repo_name=repo_name,
    )


def _parse_pr_review(payload: dict, repo_owner: str, repo_name: str) -> PRReviewEvent | None:
    if payload.get("action") != "submitted":
        return None
    review = payload.get("review") or {}
    state = str(review.get("state") or "")
    if state not in ("changes_requested", "approved"):
        return None
    pr = payload.get("pull_request") or {}
    head = pr.get("head") or {}
    return PRReviewEvent(
        pr_number=int(pr.get("number") or 0),
        pr_head_branch=str(head.get("ref") or ""),
        review_state=state,
        review_body=str(review.get("body") or "").strip(),
        reviewer=str((review.get("user") or {}).get("login") or "unknown"),
        repo_owner=repo_owner,
        repo_name=repo_name,
    )


def _parse_pr_closed(payload: dict, repo_owner: str, repo_name: str) -> PRClosedEvent | None:
    if payload.get("action") != "closed":
        return None
    pr = payload.get("pull_request") or {}
    head = pr.get("head") or {}
    return PRClosedEvent(
        pr_number=int(pr.get("number") or 0),
        pr_head_branch=str(head.get("ref") or ""),
        merged=bool(pr.get("merged")),
        repo_owner=repo_owner,
        repo_name=repo_name,
    )
