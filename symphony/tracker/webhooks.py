"""Linear webhook receiver: HMAC-SHA256 verification, payload parsing, and webhook lifecycle."""
from __future__ import annotations

import hashlib
import hmac
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


LINEAR_WEBHOOKS_URL = "https://api.linear.app/graphql"

LIST_WEBHOOKS_QUERY = """
query SymphonyListWebhooks($teamId: String!) {
  webhooks(filter: {team: {id: {eq: $teamId}}}) {
    nodes {
      id
      url
      enabled
      label
    }
  }
}
""".strip()

CREATE_WEBHOOK_MUTATION = """
mutation SymphonyCreateWebhook($input: WebhookCreateInput!) {
  webhookCreate(input: $input) {
    success
    webhook {
      id
      url
      enabled
    }
  }
}
""".strip()

DELETE_WEBHOOK_MUTATION = """
mutation SymphonyDeleteWebhook($id: String!) {
  webhookDelete(id: $id) {
    success
  }
}
""".strip()


@dataclass
class LinearWebhookEvent:
    action: str           # "create", "update", "remove"
    type: str             # "Issue", "IssueLabel", etc.
    data: dict            # raw payload .data field
    url: str              # issue URL if present
    created_at: str       # ISO timestamp from payload


def verify_signature(payload_bytes: bytes, signature: str, secret: str) -> bool:
    """HMAC-SHA256. signature is the value of X-Linear-Signature header."""
    if not signature:
        return False
    expected = hmac.new(secret.encode(), payload_bytes, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def parse_webhook_payload(body: bytes) -> LinearWebhookEvent | None:
    """Return None for unrecognized/malformed payloads."""
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return None

    if not isinstance(payload, dict):
        return None

    action = payload.get("action")
    event_type = payload.get("type")
    data = payload.get("data")
    created_at = payload.get("createdAt", "")

    if not isinstance(action, str) or not action:
        return None
    if not isinstance(event_type, str) or not event_type:
        return None
    if not isinstance(data, dict):
        return None

    url = ""
    if isinstance(data, dict):
        url = data.get("url", "") or ""

    return LinearWebhookEvent(
        action=action,
        type=event_type,
        data=data,
        url=url,
        created_at=created_at if isinstance(created_at, str) else "",
    )


class WebhookRegistrar:
    """Manages webhook lifecycle against the Linear API."""

    def __init__(self, api_token: str, base_url: str = "https://api.linear.app") -> None:
        self.api_token = api_token
        self.graphql_url = f"{base_url.rstrip('/')}/graphql"

    async def register(self, url: str, team_id: str, secret: str) -> str:
        """Register webhook; return webhook ID. Idempotent — skip if URL already registered."""
        existing = await self.list_webhooks(team_id)
        for webhook in existing:
            if isinstance(webhook, dict) and webhook.get("url") == url:
                return webhook["id"]

        result = self._graphql(
            CREATE_WEBHOOK_MUTATION,
            {
                "input": {
                    "url": url,
                    "teamId": team_id,
                    "secret": secret,
                    "resourceTypes": ["Issue"],
                }
            },
        )
        webhook_data = (result.get("data") or {}).get("webhookCreate") or {}
        if not webhook_data.get("success"):
            raise WebhookRegistrarError("webhook_create_failed")
        webhook = webhook_data.get("webhook") or {}
        webhook_id = webhook.get("id")
        if not webhook_id:
            raise WebhookRegistrarError("webhook_create_missing_id")
        return webhook_id

    async def unregister(self, webhook_id: str) -> None:
        """Delete a webhook by ID."""
        result = self._graphql(DELETE_WEBHOOK_MUTATION, {"id": webhook_id})
        deleted = (result.get("data") or {}).get("webhookDelete") or {}
        if not deleted.get("success"):
            raise WebhookRegistrarError("webhook_delete_failed")

    async def list_webhooks(self, team_id: str) -> list[dict]:
        """List all webhooks for a team."""
        result = self._graphql(LIST_WEBHOOKS_QUERY, {"teamId": team_id})
        nodes = ((result.get("data") or {}).get("webhooks") or {}).get("nodes") or []
        return [node for node in nodes if isinstance(node, dict)]

    def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
        request = urllib.request.Request(
            self.graphql_url,
            data=payload,
            headers={
                "Authorization": self.api_token,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            raise WebhookRegistrarError(f"http_error:{exc.code}:{raw}") from exc
        except urllib.error.URLError as exc:
            raise WebhookRegistrarError(f"url_error:{exc}") from exc


class WebhookRegistrarError(RuntimeError):
    """Raised when a webhook lifecycle operation fails."""
