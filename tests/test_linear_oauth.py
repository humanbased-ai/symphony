"""Tests for the Linear OAuth 2.0 PKCE flow (IN-165)."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from symphony.auth import OAuthToken
from symphony.tracker.linear_oauth import (
    CALLBACK_PATH,
    CALLBACK_PORT,
    OAuthError,
    PKCEChallenge,
    build_authorization_url,
    exchange_code,
    parse_token_response,
    refresh_token,
    revoke_access_token,
)


# ---------------------------------------------------------------------------
# PKCEChallenge
# ---------------------------------------------------------------------------


def test_pkce_generate_produces_valid_challenge():
    pkce = PKCEChallenge.generate()
    assert pkce.code_verifier
    assert pkce.code_challenge
    assert pkce.state
    # S256: challenge is base64url(sha256(verifier))
    import base64
    import hashlib
    expected = base64.urlsafe_b64encode(hashlib.sha256(pkce.code_verifier.encode()).digest()).rstrip(b"=").decode()
    assert pkce.code_challenge == expected


def test_pkce_generate_produces_unique_challenges():
    a = PKCEChallenge.generate()
    b = PKCEChallenge.generate()
    assert a.code_verifier != b.code_verifier
    assert a.state != b.state


# ---------------------------------------------------------------------------
# build_authorization_url
# ---------------------------------------------------------------------------


def test_build_authorization_url_contains_required_params():
    pkce = PKCEChallenge.generate()
    url = build_authorization_url("my-client", "http://localhost:7338/callback", pkce)
    assert "client_id=my-client" in url
    assert "response_type=code" in url
    assert "code_challenge_method=S256" in url
    assert pkce.code_challenge in url
    assert pkce.state in url


def test_build_authorization_url_custom_scopes():
    pkce = PKCEChallenge.generate()
    url = build_authorization_url("cid", "http://localhost/cb", pkce, scopes=["read"])
    assert "scope=read" in url


# ---------------------------------------------------------------------------
# parse_token_response
# ---------------------------------------------------------------------------


def test_parse_token_response_happy_path():
    data: dict[str, Any] = {
        "access_token": "lin_tok_abc",
        "refresh_token": "lin_ref_xyz",
        "expires_in": 3600,
        "token_type": "Bearer",
    }
    token = parse_token_response(data)
    assert token.access_token == "lin_tok_abc"
    assert token.refresh_token == "lin_ref_xyz"
    assert token.expires_at is not None
    assert not token.is_expired()


def test_parse_token_response_no_refresh_token():
    data: dict[str, Any] = {"access_token": "tok", "token_type": "Bearer"}
    token = parse_token_response(data)
    assert token.refresh_token is None
    assert token.expires_at is None


def test_parse_token_response_missing_access_token_raises():
    with pytest.raises(OAuthError, match="missing_access_token"):
        parse_token_response({})


def test_parse_token_response_empty_access_token_raises():
    with pytest.raises(OAuthError, match="missing_access_token"):
        parse_token_response({"access_token": "   "})


# ---------------------------------------------------------------------------
# exchange_code — urllib mock
# ---------------------------------------------------------------------------


def _mock_urlopen(response_body: dict[str, Any]):
    response = MagicMock()
    response.read.return_value = json.dumps(response_body).encode()
    response.__enter__ = lambda s: s
    response.__exit__ = MagicMock(return_value=False)
    return response


@patch("urllib.request.urlopen")
def test_exchange_code_success(mock_open):
    body = {"access_token": "tok", "token_type": "Bearer"}
    mock_open.return_value = _mock_urlopen(body)
    result = exchange_code("client", "secret", "code123", "http://localhost/cb", "verifier")
    assert result["access_token"] == "tok"
    req = mock_open.call_args[0][0]
    assert "grant_type=authorization_code" in req.data.decode()
    assert "code_verifier=verifier" in req.data.decode()


@patch("urllib.request.urlopen")
def test_exchange_code_without_secret(mock_open):
    body = {"access_token": "tok", "token_type": "Bearer"}
    mock_open.return_value = _mock_urlopen(body)
    result = exchange_code("client", None, "code", "http://localhost/cb", "ver")
    assert result["access_token"] == "tok"
    req = mock_open.call_args[0][0]
    assert "client_secret" not in req.data.decode()


@patch("urllib.request.urlopen")
def test_exchange_code_http_error_raises(mock_open):
    exc = urllib.error.HTTPError(url=None, code=400, msg="Bad Request", hdrs=None, fp=MagicMock(read=lambda: b'{"error":"bad"}'))
    mock_open.side_effect = exc
    with pytest.raises(OAuthError, match="token_exchange_failed"):
        exchange_code("c", "s", "code", "http://localhost/cb", "ver")


# ---------------------------------------------------------------------------
# refresh_token — urllib mock
# ---------------------------------------------------------------------------


@patch("urllib.request.urlopen")
def test_refresh_token_success(mock_open):
    body = {"access_token": "new_tok", "token_type": "Bearer"}
    mock_open.return_value = _mock_urlopen(body)
    result = refresh_token("client", "secret", "old_refresh")
    assert result["access_token"] == "new_tok"
    req = mock_open.call_args[0][0]
    assert "grant_type=refresh_token" in req.data.decode()


@patch("urllib.request.urlopen")
def test_refresh_token_http_error_raises(mock_open):
    exc = urllib.error.HTTPError(url=None, code=401, msg="Unauthorized", hdrs=None, fp=MagicMock(read=lambda: b""))
    mock_open.side_effect = exc
    with pytest.raises(OAuthError, match="token_refresh_failed"):
        refresh_token("c", "s", "ref")


# ---------------------------------------------------------------------------
# revoke_access_token — urllib mock
# ---------------------------------------------------------------------------


@patch("urllib.request.urlopen")
def test_revoke_success(mock_open):
    resp = MagicMock()
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    mock_open.return_value = resp
    revoke_access_token("tok", "client", "secret")  # should not raise


@patch("urllib.request.urlopen")
def test_revoke_http_error_raises(mock_open):
    exc = urllib.error.HTTPError(url=None, code=500, msg="Server Error", hdrs=None, fp=MagicMock(read=lambda: b""))
    mock_open.side_effect = exc
    with pytest.raises(OAuthError, match="revoke_failed"):
        revoke_access_token("tok", "client")


@patch("urllib.request.urlopen")
def test_revoke_url_error_raises_oauth_error(mock_open):
    mock_open.side_effect = urllib.error.URLError("connection refused")
    with pytest.raises(OAuthError, match="revoke_failed"):
        revoke_access_token("tok", "client", "secret")


# ---------------------------------------------------------------------------
# OAuthAPI HTTP handler
# ---------------------------------------------------------------------------


from symphony.http_server import OAuthAPI


def test_oauth_status_no_store():
    api = OAuthAPI(credential_store=None)
    resp = api.handle_request("GET", "/api/v1/linear/auth/status")
    assert resp is not None
    assert resp.status_code == 200
    assert resp.body["authenticated"] is False


def test_oauth_status_with_valid_token():
    store = MagicMock()
    store.status.return_value = {"authenticated": True, "source": "file", "expires_at": "2099-01-01T00:00:00Z"}
    api = OAuthAPI(credential_store=store)
    resp = api.handle_request("GET", "/api/v1/linear/auth/status")
    assert resp is not None
    assert resp.status_code == 200
    assert resp.body["authenticated"] is True


def test_oauth_status_never_returns_token_material():
    """The status endpoint must never echo access/refresh tokens."""
    store = MagicMock()
    # A store that (incorrectly) leaked tokens in its status dict still must not
    # reach the response — only the whitelisted keys are surfaced.
    store.status.return_value = {
        "authenticated": True,
        "source": "file",
        "expires_at": "2099-01-01T00:00:00Z",
        "access_token": "leaky_secret",
        "refresh_token": "leaky_refresh",
    }
    api = OAuthAPI(credential_store=store)
    resp = api.handle_request("GET", "/api/v1/linear/auth/status")
    assert resp is not None
    serialized = resp.response_bytes().decode()
    assert "leaky_secret" not in serialized
    assert "leaky_refresh" not in serialized
    assert set(resp.body) == {"authenticated", "source", "expires_at"}


def test_oauth_status_wrong_method():
    api = OAuthAPI()
    resp = api.handle_request("POST", "/api/v1/linear/auth/status")
    assert resp is not None
    assert resp.status_code == 405


def test_oauth_non_auth_route_returns_none():
    api = OAuthAPI()
    resp = api.handle_request("GET", "/api/v1/health")
    assert resp is None


def test_oauth_start_missing_client_id():
    api = OAuthAPI()
    resp = api.handle_request("POST", "/api/v1/linear/auth/start", b"{}")
    assert resp is not None
    assert resp.status_code == 400
    assert "client_id" in resp.body["error"]["code"]


def test_oauth_start_returns_authorization_url():
    api = OAuthAPI()
    body = json.dumps({"client_id": "test-client"}).encode()
    resp = api.handle_request("POST", "/api/v1/linear/auth/start", body)
    assert resp is not None
    assert resp.status_code == 200
    assert "authorization_url" in resp.body
    assert "test-client" in resp.body["authorization_url"]
    assert "state" in resp.body


def test_oauth_start_wrong_method():
    api = OAuthAPI()
    resp = api.handle_request("GET", "/api/v1/linear/auth/start")
    assert resp is not None
    assert resp.status_code == 405


def test_oauth_revoke_no_store():
    api = OAuthAPI(credential_store=None)
    resp = api.handle_request("POST", "/api/v1/linear/auth/revoke")
    assert resp is not None
    assert resp.status_code == 200
    assert resp.body["revoked"] is False


def test_oauth_revoke_no_token():
    store = MagicMock()
    store.load_oauth_token.return_value = None
    api = OAuthAPI(credential_store=store)
    resp = api.handle_request("POST", "/api/v1/linear/auth/revoke")
    assert resp is not None
    assert resp.status_code == 200
    assert resp.body["revoked"] is False


def test_oauth_revoke_deletes_token():
    token = OAuthToken(access_token="tok")
    store = MagicMock()
    store.load_oauth_token.return_value = token
    api = OAuthAPI(credential_store=store)
    resp = api.handle_request("POST", "/api/v1/linear/auth/revoke")
    assert resp is not None
    assert resp.status_code == 200
    assert resp.body["revoked"] is True
    store.delete_oauth_token.assert_called_once()


# ---------------------------------------------------------------------------
# symphony auth CLI commands
# ---------------------------------------------------------------------------


from symphony.cli import auth_main


def test_auth_main_no_args_returns_1():
    rc = auth_main([])
    assert rc == 1


def test_auth_main_unknown_subcommand_returns_1():
    rc = auth_main(["notacommand"])
    assert rc == 1


def test_auth_status_no_oauth_no_apikey(monkeypatch, tmp_path):
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    from symphony.auth import FileCredentialStore
    store = FileCredentialStore(environ={"XDG_CONFIG_HOME": str(tmp_path)})
    with patch("symphony.cli.default_credential_store", return_value=store):
        rc = auth_main(["status"])
    assert rc == 0


def test_auth_login_missing_client_id(monkeypatch):
    monkeypatch.delenv("LINEAR_CLIENT_ID", raising=False)
    rc = auth_main(["login"])
    assert rc == 1


@patch("symphony.cli._run_pkce_flow")
def test_auth_login_stores_token(mock_flow, monkeypatch, tmp_path):
    token = OAuthToken(access_token="new_tok")
    mock_flow.return_value = token
    monkeypatch.setenv("LINEAR_CLIENT_ID", "cid")
    from symphony.auth import FileCredentialStore
    store = FileCredentialStore(environ={"XDG_CONFIG_HOME": str(tmp_path)})
    with patch("symphony.cli.default_credential_store", return_value=store):
        rc = auth_main(["login"])
    assert rc == 0
    mock_flow.assert_called_once()
    assert store.load_oauth_token() is not None


def test_auth_revoke_no_token(monkeypatch, tmp_path):
    from symphony.auth import FileCredentialStore
    store = FileCredentialStore(environ={"XDG_CONFIG_HOME": str(tmp_path)})
    with patch("symphony.cli.default_credential_store", return_value=store):
        rc = auth_main(["revoke", "--local-only"])
    assert rc == 0


def test_auth_revoke_removes_stored_token(monkeypatch, tmp_path):
    from symphony.auth import FileCredentialStore
    store = FileCredentialStore(environ={"XDG_CONFIG_HOME": str(tmp_path)})
    token = OAuthToken(access_token="to_delete")
    store.save_oauth_token(token)
    with patch("symphony.cli.default_credential_store", return_value=store):
        rc = auth_main(["revoke", "--local-only"])
    assert rc == 0
    assert store.load_oauth_token() is None


# ---------------------------------------------------------------------------
# OAuthAPI callback uses stored redirect_uri; server_port as default
# ---------------------------------------------------------------------------


def test_oauth_start_uses_server_port_as_default_redirect():
    """When no port is given, /start uses server_port, not CALLBACK_PORT."""
    api = OAuthAPI(credential_store=None, server_port=7337)
    body = json.dumps({"client_id": "cid"}).encode()
    resp = api.handle_request("POST", "/api/v1/linear/auth/start", body)
    assert resp is not None
    assert resp.status_code == 200
    assert ":7337" in resp.body["redirect_uri"]


def test_oauth_start_explicit_port_overrides_server_port():
    """Explicit port in request body takes precedence over server_port."""
    api = OAuthAPI(credential_store=None, server_port=7337)
    body = json.dumps({"client_id": "cid", "port": 19999}).encode()
    resp = api.handle_request("POST", "/api/v1/linear/auth/start", body)
    assert resp is not None
    assert resp.status_code == 200
    assert ":19999" in resp.body["redirect_uri"]


def test_oauth_callback_uses_stored_redirect_uri():
    """Callback must use the redirect_uri from /start, not a hard-coded port."""
    api = OAuthAPI(credential_store=None, server_port=7337)

    custom_port = 19999
    body = json.dumps({"client_id": "cid", "port": custom_port}).encode()
    start_resp = api.handle_request("POST", "/api/v1/linear/auth/start", body)
    assert start_resp is not None
    assert start_resp.status_code == 200

    state = start_resp.body["state"]
    redirect_uri = start_resp.body["redirect_uri"]
    assert f":{custom_port}" in redirect_uri

    pending = object.__getattribute__(api, "_pending")
    assert state in pending
    stored_redirect = pending[state][3]
    assert stored_redirect == redirect_uri


def test_oauth_callback_missing_state_returns_html_error():
    from symphony.http_server import _CALLBACK_HTML_ERR

    api = OAuthAPI(credential_store=None)
    resp = api.handle_request("GET", "/api/v1/linear/auth/callback?code=abc&state=unknown")
    assert resp is not None
    assert resp.status_code == 400
    assert resp.raw_body == _CALLBACK_HTML_ERR


def test_oauth_callback_error_param_returns_html_error():
    from symphony.http_server import _CALLBACK_HTML_ERR

    api = OAuthAPI(credential_store=None)
    resp = api.handle_request("GET", "/api/v1/linear/auth/callback?error=access_denied")
    assert resp is not None
    assert resp.status_code == 400
    assert resp.raw_body == _CALLBACK_HTML_ERR


def test_oauth_callback_exchanges_code_and_saves_token():
    """A valid callback exchanges the code and persists the resulting token."""
    store = MagicMock()
    api = OAuthAPI(credential_store=store, server_port=7337)

    start_resp = api.handle_request("POST", "/api/v1/linear/auth/start", json.dumps({"client_id": "cid"}).encode())
    state = start_resp.body["state"]

    token = OAuthToken(access_token="exchanged_tok")
    with (
        patch("symphony.tracker.linear_oauth.exchange_code", return_value={"access_token": "exchanged_tok"}) as ex,
        patch("symphony.tracker.linear_oauth.parse_token_response", return_value=token),
    ):
        resp = api.handle_request("GET", f"/api/v1/linear/auth/callback?code=authcode&state={state}")

    assert resp is not None
    assert resp.status_code == 200
    ex.assert_called_once()
    store.save_oauth_token.assert_called_once_with(token)


# ---------------------------------------------------------------------------
# create_status_http_server wires OAuthAPI
# ---------------------------------------------------------------------------


def test_status_http_server_routes_oauth_paths():
    """OAuthAPI routes must be reachable through create_status_http_server."""
    import asyncio
    import threading
    import urllib.request

    from symphony.cli import create_status_http_server
    from symphony.http_server import StatusAPI

    oauth_api = OAuthAPI(credential_store=None)
    status_api = StatusAPI(state_provider=lambda: None)
    loop = asyncio.new_event_loop()

    server = create_status_http_server(status_api, 0, loop=loop, host="127.0.0.1", oauth_api=oauth_api)
    port = server.server_address[1]

    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/linear/auth/status")
        assert resp.status == 200
        data = json.loads(resp.read())
        assert "authenticated" in data
    finally:
        server.shutdown()
        loop.close()
