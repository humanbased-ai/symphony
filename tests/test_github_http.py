"""Tests for GitHubWebhookAPI in symphony.http_server."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import unittest

from symphony.github.webhooks import PRClosedEvent, PRCommentEvent, PRReviewEvent
from symphony.http_server import GitHubWebhookAPI

SECRET = "gh-secret"


def _sig(payload: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), payload, hashlib.sha256).hexdigest()


def _comment_body() -> bytes:
    return json.dumps({
        "action": "created",
        "comment": {"id": 1, "body": "Fix this", "user": {"login": "alice"}},
        "pull_request": {"number": 3, "head": {"ref": "feat/sym-1-abc", "sha": "aaa"}},
        "repository": {"name": "repo", "owner": {"login": "org"}},
    }).encode()


class TestGitHubWebhookAPI(unittest.IsolatedAsyncioTestCase):
    async def test_valid_comment_event_dispatched(self) -> None:
        received: list = []
        api = GitHubWebhookAPI(webhook_secret=SECRET, on_event=lambda e: received.append(e))
        body = _comment_body()
        resp = await api.async_handle_request(
            "POST",
            "/api/v1/webhooks/github",
            body,
            {"x-hub-signature-256": _sig(body), "x-github-event": "pull_request_review_comment"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(received), 1)
        self.assertIsInstance(received[0], PRCommentEvent)

    async def test_missing_signature_rejected(self) -> None:
        api = GitHubWebhookAPI(webhook_secret=SECRET)
        resp = await api.async_handle_request(
            "POST", "/api/v1/webhooks/github", _comment_body(),
            {"x-github-event": "pull_request_review_comment"},
        )
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.body["error"]["code"], "missing_signature")

    async def test_wrong_signature_rejected(self) -> None:
        api = GitHubWebhookAPI(webhook_secret=SECRET)
        resp = await api.async_handle_request(
            "POST", "/api/v1/webhooks/github", _comment_body(),
            {"x-hub-signature-256": "sha256=" + "00" * 32, "x-github-event": "pull_request_review_comment"},
        )
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.body["error"]["code"], "invalid_signature")

    async def test_no_secret_skips_verification(self) -> None:
        received: list = []
        api = GitHubWebhookAPI(webhook_secret=None, on_event=lambda e: received.append(e))
        body = _comment_body()
        resp = await api.async_handle_request(
            "POST", "/api/v1/webhooks/github", body,
            {"x-github-event": "pull_request_review_comment"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(received), 1)

    async def test_missing_event_type_header_rejected(self) -> None:
        api = GitHubWebhookAPI(webhook_secret=None)
        resp = await api.async_handle_request("POST", "/api/v1/webhooks/github", _comment_body(), {})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.body["error"]["code"], "missing_event_type")

    async def test_unrecognized_event_type_skipped(self) -> None:
        api = GitHubWebhookAPI(webhook_secret=None)
        body = json.dumps({"action": "push"}).encode()
        resp = await api.async_handle_request(
            "POST", "/api/v1/webhooks/github", body,
            {"x-github-event": "push"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.body.get("skipped"))

    async def test_wrong_method_rejected(self) -> None:
        api = GitHubWebhookAPI(webhook_secret=None)
        resp = await api.async_handle_request("GET", "/api/v1/webhooks/github", None, {})
        self.assertEqual(resp.status_code, 405)

    async def test_wrong_route_rejected(self) -> None:
        api = GitHubWebhookAPI(webhook_secret=None)
        resp = await api.async_handle_request("POST", "/api/v1/webhooks/linear", _comment_body(), {})
        self.assertEqual(resp.status_code, 404)

    async def test_pr_closed_event_dispatched(self) -> None:
        received: list = []
        api = GitHubWebhookAPI(webhook_secret=None, on_event=lambda e: received.append(e))
        body = json.dumps({
            "action": "closed",
            "pull_request": {"number": 5, "merged": True, "head": {"ref": "feat/sym-2", "sha": "bbb"}},
            "repository": {"name": "repo", "owner": {"login": "org"}},
        }).encode()
        resp = await api.async_handle_request(
            "POST", "/api/v1/webhooks/github", body, {"x-github-event": "pull_request"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIsInstance(received[0], PRClosedEvent)
        assert isinstance(received[0], PRClosedEvent)
        self.assertTrue(received[0].merged)
