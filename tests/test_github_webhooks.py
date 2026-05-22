"""Tests for symphony.github.webhooks — payload parsing and signature verification."""
from __future__ import annotations

import hashlib
import hmac
import json
import unittest

from symphony.github.webhooks import (
    PRClosedEvent,
    PRCommentEvent,
    PRReviewEvent,
    parse_github_payload,
    verify_signature,
)


SECRET = "test-secret"


def _sig(payload: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# verify_signature
# ---------------------------------------------------------------------------

class TestVerifySignature(unittest.TestCase):
    def test_valid_signature_accepted(self) -> None:
        payload = b'{"action":"created"}'
        self.assertTrue(verify_signature(payload, _sig(payload), SECRET))

    def test_wrong_signature_rejected(self) -> None:
        self.assertFalse(verify_signature(b"data", "sha256=" + "ab" * 32, SECRET))

    def test_empty_signature_rejected(self) -> None:
        self.assertFalse(verify_signature(b"data", "", SECRET))

    def test_missing_prefix_rejected(self) -> None:
        payload = b"data"
        raw_hex = hmac.new(SECRET.encode(), payload, hashlib.sha256).hexdigest()
        self.assertFalse(verify_signature(payload, raw_hex, SECRET))

    def test_wrong_secret_rejected(self) -> None:
        payload = b"data"
        self.assertFalse(verify_signature(payload, _sig(payload, "other"), SECRET))


# ---------------------------------------------------------------------------
# parse_github_payload — pull_request_review_comment
# ---------------------------------------------------------------------------

def _pr_comment_payload(
    *,
    action: str = "created",
    pr_number: int = 7,
    branch: str = "feat/sym-42-thing-abc",
    sha: str = "deadbeef",
    comment_body: str = "Please fix this",
    author: str = "alice",
    comment_id: int = 99,
    owner: str = "myorg",
    repo: str = "myrepo",
) -> bytes:
    return json.dumps({
        "action": action,
        "comment": {"id": comment_id, "body": comment_body, "user": {"login": author}},
        "pull_request": {"number": pr_number, "head": {"ref": branch, "sha": sha}},
        "repository": {"name": repo, "owner": {"login": owner}},
    }).encode()


class TestParsePRReviewComment(unittest.TestCase):
    def test_created_comment_parsed(self) -> None:
        event = parse_github_payload(_pr_comment_payload(), "pull_request_review_comment")
        self.assertIsInstance(event, PRCommentEvent)
        assert isinstance(event, PRCommentEvent)
        self.assertEqual(event.pr_number, 7)
        self.assertEqual(event.pr_head_branch, "feat/sym-42-thing-abc")
        self.assertEqual(event.comment_body, "Please fix this")
        self.assertEqual(event.comment_author, "alice")
        self.assertEqual(event.repo_owner, "myorg")
        self.assertEqual(event.repo_name, "myrepo")

    def test_non_created_action_ignored(self) -> None:
        payload = _pr_comment_payload(action="edited")
        self.assertIsNone(parse_github_payload(payload, "pull_request_review_comment"))

    def test_empty_body_ignored(self) -> None:
        payload = _pr_comment_payload(comment_body="   ")
        self.assertIsNone(parse_github_payload(payload, "pull_request_review_comment"))

    def test_unknown_event_type_returns_none(self) -> None:
        self.assertIsNone(parse_github_payload(_pr_comment_payload(), "push"))

    def test_invalid_json_returns_none(self) -> None:
        self.assertIsNone(parse_github_payload(b"not-json", "pull_request_review_comment"))


# ---------------------------------------------------------------------------
# parse_github_payload — pull_request_review
# ---------------------------------------------------------------------------

def _pr_review_payload(
    *,
    action: str = "submitted",
    state: str = "changes_requested",
    review_body: str = "Needs work",
    reviewer: str = "bob",
    pr_number: int = 7,
    branch: str = "feat/sym-42-thing-abc",
    owner: str = "myorg",
    repo: str = "myrepo",
) -> bytes:
    return json.dumps({
        "action": action,
        "review": {"state": state, "body": review_body, "user": {"login": reviewer}},
        "pull_request": {"number": pr_number, "head": {"ref": branch, "sha": "abc"}},
        "repository": {"name": repo, "owner": {"login": owner}},
    }).encode()


class TestParsePRReview(unittest.TestCase):
    def test_changes_requested_parsed(self) -> None:
        event = parse_github_payload(_pr_review_payload(state="changes_requested"), "pull_request_review")
        self.assertIsInstance(event, PRReviewEvent)
        assert isinstance(event, PRReviewEvent)
        self.assertEqual(event.review_state, "changes_requested")
        self.assertEqual(event.reviewer, "bob")
        self.assertEqual(event.review_body, "Needs work")

    def test_approved_parsed(self) -> None:
        event = parse_github_payload(_pr_review_payload(state="approved"), "pull_request_review")
        self.assertIsInstance(event, PRReviewEvent)
        assert isinstance(event, PRReviewEvent)
        self.assertEqual(event.review_state, "approved")

    def test_commented_review_ignored(self) -> None:
        payload = _pr_review_payload(state="commented")
        self.assertIsNone(parse_github_payload(payload, "pull_request_review"))

    def test_non_submitted_action_ignored(self) -> None:
        payload = _pr_review_payload(action="dismissed")
        self.assertIsNone(parse_github_payload(payload, "pull_request_review"))


# ---------------------------------------------------------------------------
# parse_github_payload — pull_request closed
# ---------------------------------------------------------------------------

def _pr_closed_payload(
    *,
    merged: bool = True,
    pr_number: int = 7,
    branch: str = "feat/sym-42-thing-abc",
    owner: str = "myorg",
    repo: str = "myrepo",
) -> bytes:
    return json.dumps({
        "action": "closed",
        "pull_request": {
            "number": pr_number,
            "merged": merged,
            "head": {"ref": branch, "sha": "abc"},
        },
        "repository": {"name": repo, "owner": {"login": owner}},
    }).encode()


class TestParsePRClosed(unittest.TestCase):
    def test_merged_pr_parsed(self) -> None:
        event = parse_github_payload(_pr_closed_payload(merged=True), "pull_request")
        self.assertIsInstance(event, PRClosedEvent)
        assert isinstance(event, PRClosedEvent)
        self.assertTrue(event.merged)
        self.assertEqual(event.pr_number, 7)

    def test_abandoned_pr_parsed(self) -> None:
        event = parse_github_payload(_pr_closed_payload(merged=False), "pull_request")
        self.assertIsInstance(event, PRClosedEvent)
        assert isinstance(event, PRClosedEvent)
        self.assertFalse(event.merged)

    def test_opened_action_ignored(self) -> None:
        payload = json.dumps({
            "action": "opened",
            "pull_request": {"number": 7, "merged": False, "head": {"ref": "main", "sha": "abc"}},
            "repository": {"name": "r", "owner": {"login": "o"}},
        }).encode()
        self.assertIsNone(parse_github_payload(payload, "pull_request"))
