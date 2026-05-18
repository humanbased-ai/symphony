from __future__ import annotations

import base64
import hashlib
import json
import secrets
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from symphony.auth import OAuthToken


LINEAR_AUTH_URL = "https://linear.app/oauth/authorize"
LINEAR_TOKEN_URL = "https://api.linear.app/oauth/token"
LINEAR_REVOKE_URL = "https://api.linear.app/oauth/revoke"

DEFAULT_SCOPES = ["read", "write", "issues:create", "comments:create"]
CALLBACK_PORT = 7338
CALLBACK_PATH = "/api/v1/linear/auth/callback"
CALLBACK_TIMEOUT_SECONDS = 120

_NETWORK_TIMEOUT = 30


class OAuthError(RuntimeError):
    """Raised when the OAuth PKCE flow fails."""


@dataclass(frozen=True)
class PKCEChallenge:
    code_verifier: str
    code_challenge: str
    state: str

    @classmethod
    def generate(cls) -> PKCEChallenge:
        verifier = secrets.token_urlsafe(64)
        digest = hashlib.sha256(verifier.encode()).digest()
        challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        state = secrets.token_urlsafe(16)
        return cls(code_verifier=verifier, code_challenge=challenge, state=state)


def build_authorization_url(
    client_id: str,
    redirect_uri: str,
    pkce: PKCEChallenge,
    scopes: list[str] | None = None,
) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes or DEFAULT_SCOPES),
        "code_challenge": pkce.code_challenge,
        "code_challenge_method": "S256",
        "state": pkce.state,
    }
    return f"{LINEAR_AUTH_URL}?{urllib.parse.urlencode(params)}"


def exchange_code(
    client_id: str,
    client_secret: str | None,
    code: str,
    redirect_uri: str,
    code_verifier: str,
) -> dict[str, object]:
    """Exchange authorization code for tokens. Returns the raw Linear token JSON."""
    payload: dict[str, str] = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    }
    if client_secret:
        payload["client_secret"] = client_secret
    return _post_form(LINEAR_TOKEN_URL, payload, "token_exchange_failed")


def refresh_token(
    client_id: str,
    client_secret: str | None,
    refresh_token_value: str,
) -> dict[str, object]:
    """Use a refresh token to get a new access token. Returns the raw Linear token JSON."""
    payload: dict[str, str] = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "refresh_token": refresh_token_value,
    }
    if client_secret:
        payload["client_secret"] = client_secret
    return _post_form(LINEAR_TOKEN_URL, payload, "token_refresh_failed")


def revoke_access_token(
    access_token: str,
    client_id: str,
    client_secret: str | None = None,
) -> None:
    """Revoke the access token via the Linear API."""
    payload = {"access_token": access_token}
    headers: dict[str, str] = {"Content-Type": "application/x-www-form-urlencoded"}
    if client_id and client_secret:
        creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        headers["Authorization"] = f"Basic {creds}"
    data = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(LINEAR_REVOKE_URL, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=_NETWORK_TIMEOUT):
            pass
    except urllib.error.HTTPError as exc:
        if exc.code not in (200, 204):
            raise OAuthError(f"revoke_failed: {exc.code}") from exc


def parse_token_response(data: dict[str, object]) -> OAuthToken:
    """Convert a Linear token response dict to an OAuthToken."""
    from symphony.auth import OAuthToken

    access_token = data.get("access_token")
    if not isinstance(access_token, str) or not access_token.strip():
        raise OAuthError("missing_access_token_in_response")
    expires_in = data.get("expires_in")
    expires_at = None
    if isinstance(expires_in, (int, float)):
        expires_at = datetime.now(UTC) + timedelta(seconds=int(expires_in))
    refresh = data.get("refresh_token")
    return OAuthToken(
        access_token=access_token,
        refresh_token=refresh if isinstance(refresh, str) else None,
        expires_at=expires_at,
        token_type=str(data.get("token_type", "Bearer")),
    )


def _post_form(url: str, payload: dict[str, str], error_prefix: str) -> dict[str, object]:
    data = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_NETWORK_TIMEOUT) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:300]
        raise OAuthError(f"{error_prefix}: {exc.code} {body}") from exc
    except urllib.error.URLError as exc:
        raise OAuthError(f"{error_prefix}: {exc.reason}") from exc
