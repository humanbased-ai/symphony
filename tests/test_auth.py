"""Tests for symphony.auth — credential store adapters (IN-201)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from symphony.auth import (
    CredentialStore,
    CredentialStoreError,
    FileCredentialStore,
    KeychainCredentialStore,
    MissingLinearTokenError,
    OAuthToken,
    TokenStore,
    redact_tokens,
)
from symphony.config import TrackerConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tracker(**kwargs) -> TrackerConfig:
    defaults: dict[str, object] = {
        "kind": "linear",
        "api_key": None,
    }
    defaults.update(kwargs)
    return TrackerConfig(**defaults)  # type: ignore[arg-type]


def _future() -> datetime:
    return datetime.now(tz=timezone.utc) + timedelta(hours=1)


def _past() -> datetime:
    return datetime.now(tz=timezone.utc) - timedelta(seconds=1)


# ---------------------------------------------------------------------------
# OAuthToken
# ---------------------------------------------------------------------------


class TestOAuthToken:
    def test_round_trip_dict(self) -> None:
        token = OAuthToken(
            access_token="acc",
            refresh_token="ref",
            expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
            token_type="Bearer",
        )
        restored = OAuthToken.from_dict(token.to_dict())
        assert restored.access_token == token.access_token
        assert restored.refresh_token == token.refresh_token
        assert restored.expires_at == token.expires_at
        assert restored.token_type == token.token_type

    def test_round_trip_no_expiry(self) -> None:
        token = OAuthToken(access_token="acc", refresh_token=None, expires_at=None)
        restored = OAuthToken.from_dict(token.to_dict())
        assert restored.expires_at is None
        assert restored.refresh_token is None

    def test_is_expired_no_expiry(self) -> None:
        token = OAuthToken(access_token="acc")
        assert token.is_expired() is False

    def test_is_expired_future(self) -> None:
        token = OAuthToken(access_token="acc", expires_at=_future())
        assert token.is_expired() is False

    def test_is_expired_past(self) -> None:
        token = OAuthToken(access_token="acc", expires_at=_past())
        assert token.is_expired() is True


# ---------------------------------------------------------------------------
# FileCredentialStore
# ---------------------------------------------------------------------------


class TestFileCredentialStore:
    def test_save_and_load_round_trip(self, tmp_path: Path) -> None:
        creds_file = tmp_path / "credentials.json"
        store = FileCredentialStore(path=creds_file)
        token = OAuthToken(
            access_token="tok_access",
            refresh_token="tok_refresh",
            expires_at=datetime(2030, 6, 15, 12, 0, tzinfo=timezone.utc),
        )
        store.save_oauth_token(token)
        loaded = store.load_oauth_token()
        assert loaded is not None
        assert loaded.access_token == token.access_token
        assert loaded.refresh_token == token.refresh_token
        assert loaded.expires_at == token.expires_at
        assert loaded.token_type == token.token_type

    def test_load_returns_none_when_absent(self, tmp_path: Path) -> None:
        store = FileCredentialStore(path=tmp_path / "credentials.json")
        assert store.load_oauth_token() is None

    def test_delete_leaves_no_trace(self, tmp_path: Path) -> None:
        creds_file = tmp_path / "credentials.json"
        store = FileCredentialStore(path=creds_file)
        token = OAuthToken(access_token="tok")
        store.save_oauth_token(token)
        assert store.load_oauth_token() is not None

        store.delete_oauth_token()

        assert store.load_oauth_token() is None
        # The key must not appear in the raw file either
        if creds_file.exists():
            raw = json.loads(creds_file.read_text())
            assert "linear_oauth" not in raw

    def test_delete_idempotent_when_absent(self, tmp_path: Path) -> None:
        store = FileCredentialStore(path=tmp_path / "credentials.json")
        # Should not raise
        store.delete_oauth_token()

    def test_status_shape_authenticated(self, tmp_path: Path) -> None:
        store = FileCredentialStore(path=tmp_path / "credentials.json")
        token = OAuthToken(access_token="tok", expires_at=_future())
        store.save_oauth_token(token)
        status = store.status()
        assert status["source"] == "file"
        assert status["authenticated"] is True
        assert "expires_at" in status
        # Must never expose token material
        assert "tok" not in str(status)

    def test_status_unauthenticated_when_absent(self, tmp_path: Path) -> None:
        store = FileCredentialStore(path=tmp_path / "credentials.json")
        status = store.status()
        assert status["source"] == "file"
        assert status["authenticated"] is False
        assert status["expires_at"] is None

    def test_status_no_token_material(self, tmp_path: Path) -> None:
        creds_file = tmp_path / "credentials.json"
        store = FileCredentialStore(path=creds_file)
        token = OAuthToken(access_token="supersecret", refresh_token="alsosecret")
        store.save_oauth_token(token)
        status = store.status()
        status_str = json.dumps(status)
        assert "supersecret" not in status_str
        assert "alsosecret" not in status_str

    def test_preserves_other_credentials(self, tmp_path: Path) -> None:
        """Saving OAuth token must not clobber existing keys like linear.api_key."""
        creds_file = tmp_path / "credentials.json"
        creds_file.write_text(json.dumps({"linear": {"api_key": "existing_key"}}))
        store = FileCredentialStore(path=creds_file)
        store.save_oauth_token(OAuthToken(access_token="tok"))
        raw = json.loads(creds_file.read_text())
        assert raw["linear"]["api_key"] == "existing_key"
        assert raw["linear"]["access_token"] == "tok"


# ---------------------------------------------------------------------------
# KeychainCredentialStore (tested via FakeKeychain double)
# ---------------------------------------------------------------------------


class FakeKeychain:
    """In-memory keyring double."""

    def __init__(self) -> None:
        self._store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self._store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self._store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self._store.pop((service, username), None)


class FakeKeychainErrors:
    """Minimal stub for keyring.errors."""

    class NoKeyringError(Exception):
        pass


class TestKeychainCredentialStore:
    def _make_store(self) -> tuple[KeychainCredentialStore, FakeKeychain]:
        fake = FakeKeychain()
        fake_errors = FakeKeychainErrors()
        store = KeychainCredentialStore()
        return store, fake

    def test_save_and_load_round_trip(self) -> None:
        fake = FakeKeychain()
        store = KeychainCredentialStore()
        token = OAuthToken(
            access_token="kc_access",
            refresh_token="kc_refresh",
            expires_at=datetime(2031, 1, 1, tzinfo=timezone.utc),
        )

        with (
            patch("keyring.set_password", side_effect=fake.set_password),
            patch("keyring.get_password", side_effect=fake.get_password),
            patch("keyring.errors.NoKeyringError", FakeKeychainErrors.NoKeyringError),
        ):
            store.save_oauth_token(token)
            loaded = store.load_oauth_token()

        assert loaded is not None
        assert loaded.access_token == token.access_token
        assert loaded.refresh_token == token.refresh_token

    def test_load_returns_none_when_absent(self) -> None:
        fake = FakeKeychain()
        store = KeychainCredentialStore()

        with (
            patch("keyring.get_password", side_effect=fake.get_password),
            patch("keyring.errors.NoKeyringError", FakeKeychainErrors.NoKeyringError),
        ):
            result = store.load_oauth_token()

        assert result is None

    def test_delete_removes_token(self) -> None:
        fake = FakeKeychain()
        store = KeychainCredentialStore()
        token = OAuthToken(access_token="kc_tok")

        with (
            patch("keyring.set_password", side_effect=fake.set_password),
            patch("keyring.get_password", side_effect=fake.get_password),
            patch("keyring.delete_password", side_effect=fake.delete_password),
            patch("keyring.errors.NoKeyringError", FakeKeychainErrors.NoKeyringError),
        ):
            store.save_oauth_token(token)
            store.delete_oauth_token()
            loaded = store.load_oauth_token()

        assert loaded is None

    def test_status_shape(self) -> None:
        fake = FakeKeychain()
        store = KeychainCredentialStore()
        token = OAuthToken(access_token="kc_tok", expires_at=_future())

        with (
            patch("keyring.set_password", side_effect=fake.set_password),
            patch("keyring.get_password", side_effect=fake.get_password),
            patch("keyring.errors.NoKeyringError", FakeKeychainErrors.NoKeyringError),
        ):
            store.save_oauth_token(token)
            status = store.status()

        assert status["source"] == "keychain"
        assert status["authenticated"] is True
        assert "kc_tok" not in str(status)

    def test_raises_credential_store_error_on_no_keyring(self) -> None:
        store = KeychainCredentialStore()

        def _raise(*args, **kwargs):
            raise FakeKeychainErrors.NoKeyringError("no backend")

        with (
            patch("keyring.get_password", side_effect=_raise),
            patch("keyring.errors.NoKeyringError", FakeKeychainErrors.NoKeyringError),
        ):
            with pytest.raises(CredentialStoreError):
                store.load_oauth_token()


# ---------------------------------------------------------------------------
# redact_tokens
# ---------------------------------------------------------------------------


class TestRedactTokens:
    def test_redacts_access_token(self) -> None:
        token = OAuthToken(access_token="secret_access")
        result = redact_tokens("token=secret_access here", token)
        assert "secret_access" not in result
        assert "[REDACTED]" in result

    def test_redacts_refresh_token(self) -> None:
        token = OAuthToken(access_token="acc", refresh_token="secret_refresh")
        result = redact_tokens("refresh=secret_refresh here", token)
        assert "secret_refresh" not in result
        assert "[REDACTED]" in result

    def test_redacts_both(self) -> None:
        token = OAuthToken(access_token="acc123", refresh_token="ref456")
        text = "access=acc123 refresh=ref456"
        result = redact_tokens(text, token)
        assert "acc123" not in result
        assert "ref456" not in result

    def test_none_token_returns_unchanged(self) -> None:
        text = "some text"
        assert redact_tokens(text, None) == text

    def test_no_match_returns_unchanged(self) -> None:
        token = OAuthToken(access_token="xyz")
        text = "nothing to redact"
        assert redact_tokens(text, token) == text


# ---------------------------------------------------------------------------
# TokenStore.resolve_linear_token
# ---------------------------------------------------------------------------


class TestTokenStoreResolveLinearToken:
    """Covers existing behaviour + new OAuth resolution step."""

    def test_resolves_from_env_var(self) -> None:
        tracker = _make_tracker()
        store = TokenStore(tracker=tracker, environ={"LINEAR_API_KEY": "env_tok"})
        assert store.resolve_linear_token() == "env_tok"

    def test_resolves_from_tracker_literal(self) -> None:
        tracker = _make_tracker(api_key="literal_key")
        store = TokenStore(tracker=tracker, environ={})
        assert store.resolve_linear_token() == "literal_key"

    def test_resolves_from_tracker_env_ref(self) -> None:
        tracker = _make_tracker(api_key="$MY_KEY")
        store = TokenStore(tracker=tracker, environ={"MY_KEY": "resolved_key"})
        assert store.resolve_linear_token() == "resolved_key"

    def test_resolves_from_credentials_file(self, tmp_path: Path) -> None:
        creds_file = tmp_path / "credentials.json"
        creds_file.write_text(json.dumps({"linear": {"api_key": "file_token"}}))
        tracker = _make_tracker()
        store = TokenStore(tracker=tracker, environ={}, credentials_path=creds_file)
        assert store.resolve_linear_token() == "file_token"

    def test_raises_when_nothing_available(self, tmp_path: Path) -> None:
        tracker = _make_tracker()
        store = TokenStore(tracker=tracker, environ={}, credentials_path=tmp_path / "none.json")
        with pytest.raises(MissingLinearTokenError):
            store.resolve_linear_token()

    def test_resolves_oauth_access_token_when_valid(self, tmp_path: Path) -> None:
        """Returns the OAuth access_token when FileCredentialStore has a non-expired token."""
        creds_file = tmp_path / "credentials.json"
        # Write a valid OAuth token but no classic API key
        oauth_token = OAuthToken(access_token="oauth_tok", expires_at=_future())
        creds_file.parent.mkdir(parents=True, exist_ok=True)
        creds_file.write_text(
            json.dumps({"linear": oauth_token.to_dict()}, indent=2) + "\n"
        )

        tracker = _make_tracker()
        # Patch default_credential_store to return a FileCredentialStore pointed at our tmp file
        with patch(
            "symphony.auth.default_credential_store",
            return_value=FileCredentialStore(path=creds_file),
        ):
            store = TokenStore(tracker=tracker, environ={}, credentials_path=creds_file)
            # credentials_path has a `linear` object with OAuth fields but no api_key
            result = store.resolve_linear_token()

        assert result == "Bearer oauth_tok"

    def test_raises_oauth_token_expired_with_refresh(self, tmp_path: Path) -> None:
        """Expired file OAuth with a refresh token falls through to api_key lookup (not raises).

        Unlike Keychain, the credentials file may also carry a valid api_key, so a
        stale OAuth record must not block resolution of a still-valid api_key.
        """
        creds_file = tmp_path / "credentials.json"
        oauth_token = OAuthToken(
            access_token="expired_tok",
            refresh_token="refresh_tok",
            expires_at=_past(),
        )
        creds_file.parent.mkdir(parents=True, exist_ok=True)
        # Write expired OAuth + a valid legacy api_key in the same `linear` object.
        data = oauth_token.to_dict()
        data["api_key"] = "saved_api_key"
        creds_file.write_text(json.dumps({"linear": data}, indent=2) + "\n")

        tracker = _make_tracker()
        with patch(
            "symphony.auth.default_credential_store",
            return_value=FileCredentialStore(path=creds_file),
        ):
            store = TokenStore(tracker=tracker, environ={}, credentials_path=creds_file)
            # Expired OAuth is skipped; the stored api_key is returned instead.
            result = store.resolve_linear_token()

        assert result == "saved_api_key"

    def test_skips_expired_oauth_without_refresh(self, tmp_path: Path) -> None:
        """Falls through to MissingLinearTokenError when expired token has no refresh_token."""
        creds_file = tmp_path / "credentials.json"
        oauth_token = OAuthToken(
            access_token="expired_tok",
            refresh_token=None,
            expires_at=_past(),
        )
        creds_file.parent.mkdir(parents=True, exist_ok=True)
        creds_file.write_text(
            json.dumps({"linear": oauth_token.to_dict()}, indent=2) + "\n"
        )

        tracker = _make_tracker()
        with patch(
            "symphony.auth.default_credential_store",
            return_value=FileCredentialStore(path=creds_file),
        ):
            store = TokenStore(tracker=tracker, environ={}, credentials_path=creds_file)
            with pytest.raises(MissingLinearTokenError) as exc_info:
                store.resolve_linear_token()

        assert str(exc_info.value) == "missing_tracker_api_key"
