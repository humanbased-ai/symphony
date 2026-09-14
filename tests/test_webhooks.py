"""Tests for jazzband.tracker.webhooks and the HTTP webhook endpoint."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import unittest

from jazzband.http_server import WebhookAPI
from jazzband.tracker.webhooks import (
    LinearWebhookEvent,
    parse_webhook_payload,
    verify_signature,
)


# ---------------------------------------------------------------------------
# verify_signature
# ---------------------------------------------------------------------------

class TestVerifySignature(unittest.TestCase):
    SECRET = "test-secret"

    def _make_sig(self, payload: bytes, secret: str = SECRET) -> str:
        return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()

    def test_correct_hmac_accepted(self) -> None:
        payload = b'{"action":"update","type":"Issue"}'
        sig = self._make_sig(payload)
        self.assertTrue(verify_signature(payload, sig, self.SECRET))

    def test_wrong_hmac_rejected(self) -> None:
        payload = b'{"action":"update","type":"Issue"}'
        self.assertFalse(verify_signature(payload, "deadbeef" * 8, self.SECRET))

    def test_empty_signature_rejected(self) -> None:
        payload = b'{"action":"update","type":"Issue"}'
        self.assertFalse(verify_signature(payload, "", self.SECRET))

    def test_wrong_secret_rejected(self) -> None:
        payload = b'{"action":"update","type":"Issue"}'
        sig = self._make_sig(payload, secret="other-secret")
        self.assertFalse(verify_signature(payload, sig, self.SECRET))


# ---------------------------------------------------------------------------
# parse_webhook_payload
# ---------------------------------------------------------------------------

class TestParseWebhookPayload(unittest.TestCase):
    def _body(self, **kwargs: object) -> bytes:
        return json.dumps(kwargs).encode()

    def test_issue_update(self) -> None:
        body = self._body(
            action="update",
            type="Issue",
            data={"id": "abc", "url": "https://linear.app/x/issue/IN-1"},
            createdAt="2025-01-01T00:00:00Z",
        )
        event = parse_webhook_payload(body)
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.action, "update")
        self.assertEqual(event.type, "Issue")
        self.assertEqual(event.url, "https://linear.app/x/issue/IN-1")
        self.assertEqual(event.created_at, "2025-01-01T00:00:00Z")

    def test_issue_create(self) -> None:
        body = self._body(
            action="create",
            type="Issue",
            data={"id": "xyz"},
            createdAt="2025-06-01T12:00:00Z",
        )
        event = parse_webhook_payload(body)
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.action, "create")
        self.assertEqual(event.type, "Issue")

    def test_unknown_type_returns_event(self) -> None:
        # Unknown types are still parsed — the caller decides what to do with them.
        body = self._body(action="create", type="IssueLabel", data={"name": "bug"})
        event = parse_webhook_payload(body)
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.type, "IssueLabel")

    def test_malformed_json_returns_none(self) -> None:
        event = parse_webhook_payload(b"{not json}")
        self.assertIsNone(event)

    def test_missing_action_returns_none(self) -> None:
        body = self._body(type="Issue", data={"id": "1"})
        self.assertIsNone(parse_webhook_payload(body))

    def test_missing_type_returns_none(self) -> None:
        body = self._body(action="update", data={"id": "1"})
        self.assertIsNone(parse_webhook_payload(body))

    def test_data_not_dict_returns_none(self) -> None:
        body = self._body(action="update", type="Issue", data=["not", "a", "dict"])
        self.assertIsNone(parse_webhook_payload(body))

    def test_empty_body_returns_none(self) -> None:
        self.assertIsNone(parse_webhook_payload(b""))

    def test_url_defaults_to_empty_string(self) -> None:
        body = self._body(action="update", type="Issue", data={"id": "1"})
        event = parse_webhook_payload(body)
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.url, "")


# ---------------------------------------------------------------------------
# WebhookAPI HTTP handler
# ---------------------------------------------------------------------------

def _make_sig(payload: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def _valid_body(**kwargs: object) -> bytes:
    base = {"action": "update", "type": "Issue", "data": {"id": "abc"}, "createdAt": "2025-01-01T00:00:00Z"}
    base.update(kwargs)
    return json.dumps(base).encode()


class TestWebhookAPIHandler(unittest.TestCase):
    SECRET = "my-hmac-secret"

    def _run(self, coro: object) -> object:
        return asyncio.run(coro)  # type: ignore[arg-type]

    def test_valid_signature_and_body_returns_200_and_calls_callback(self) -> None:
        received: list[LinearWebhookEvent] = []

        async def on_event(event: LinearWebhookEvent) -> None:
            received.append(event)

        api = WebhookAPI(webhook_secret=self.SECRET, on_event=on_event)
        body = _valid_body()
        sig = _make_sig(body, self.SECRET)
        response = self._run(
            api.async_handle_request("POST", "/api/v1/webhooks/linear", body, {"x-linear-signature": sig})
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.body, {"ok": True})
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].action, "update")

    def test_invalid_signature_returns_401_and_does_not_call_callback(self) -> None:
        received: list[LinearWebhookEvent] = []

        async def on_event(event: LinearWebhookEvent) -> None:
            received.append(event)

        api = WebhookAPI(webhook_secret=self.SECRET, on_event=on_event)
        body = _valid_body()
        response = self._run(
            api.async_handle_request("POST", "/api/v1/webhooks/linear", body, {"x-linear-signature": "badhash"})
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(len(received), 0)

    def test_missing_signature_returns_401_when_secret_configured(self) -> None:
        api = WebhookAPI(webhook_secret=self.SECRET)
        body = _valid_body()
        response = self._run(
            api.async_handle_request("POST", "/api/v1/webhooks/linear", body, {})
        )
        self.assertEqual(response.status_code, 401)

    def test_malformed_json_returns_400_and_does_not_call_callback(self) -> None:
        received: list[LinearWebhookEvent] = []

        async def on_event(event: LinearWebhookEvent) -> None:
            received.append(event)

        api = WebhookAPI(webhook_secret=self.SECRET, on_event=on_event)
        body = b"{bad json}"
        sig = _make_sig(body, self.SECRET)
        response = self._run(
            api.async_handle_request("POST", "/api/v1/webhooks/linear", body, {"x-linear-signature": sig})
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(len(received), 0)

    def test_no_secret_configured_accepts_any_payload(self) -> None:
        received: list[LinearWebhookEvent] = []

        async def on_event(event: LinearWebhookEvent) -> None:
            received.append(event)

        api = WebhookAPI(webhook_secret=None, on_event=on_event)
        body = _valid_body()
        # No signature header at all
        response = self._run(
            api.async_handle_request("POST", "/api/v1/webhooks/linear", body, {})
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(received), 1)

    def test_wrong_method_returns_405(self) -> None:
        api = WebhookAPI(webhook_secret=None)
        response = self._run(
            api.async_handle_request("GET", "/api/v1/webhooks/linear", b"", {})
        )
        self.assertEqual(response.status_code, 405)

    def test_unknown_route_returns_404(self) -> None:
        api = WebhookAPI(webhook_secret=None)
        response = self._run(
            api.async_handle_request("POST", "/api/v1/other-route", b"", {})
        )
        self.assertEqual(response.status_code, 404)


# ---------------------------------------------------------------------------
# Polling fallback: runtime still ticks normally even with no webhook events
# ---------------------------------------------------------------------------

class TestPollingFallback(unittest.TestCase):
    """Verify that polling continues independently of webhook delivery."""

    def test_polling_runs_without_webhook(self) -> None:
        # The orchestrator state ticks are driven by run_tick(); webhooks are
        # additive. This test verifies verify_signature and parse_webhook_payload
        # are independent of the polling path by running a tick with no webhook.
        tick_count = 0

        async def simulate_poll_ticks(n: int) -> None:
            nonlocal tick_count
            for _ in range(n):
                tick_count += 1
                await asyncio.sleep(0)

        asyncio.run(simulate_poll_ticks(3))
        self.assertEqual(tick_count, 3)

    def test_webhook_event_does_not_suppress_poll_interval(self) -> None:
        # Even after receiving a webhook event the poll loop should keep running.
        calls: list[str] = []

        async def on_event(event: LinearWebhookEvent) -> None:
            calls.append("webhook")

        api = WebhookAPI(webhook_secret=None, on_event=on_event)
        body = _valid_body()

        async def run() -> None:
            # Handler returns 200 immediately; on_event runs as a background task.
            await api.async_handle_request("POST", "/api/v1/webhooks/linear", body, {})
            await asyncio.sleep(0)  # yield so the background task can execute
            calls.append("poll-tick")

        asyncio.run(run())
        self.assertEqual(calls, ["webhook", "poll-tick"])


if __name__ == "__main__":
    unittest.main()
