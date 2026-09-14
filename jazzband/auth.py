from __future__ import annotations

import json
import os
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from .config import TrackerConfig


DEFAULT_CREDENTIALS_DIR = ".config/jazzband"
DEFAULT_CREDENTIALS_FILE = "credentials.json"


class MissingLinearTokenError(ValueError):
    """Raised when no Linear API token can be resolved."""


class CredentialStoreError(RuntimeError):
    """Raised when the credential store backend is unavailable."""


@dataclass
class OAuthToken:
    """Represents a stored OAuth token."""

    access_token: str
    refresh_token: str | None = None
    expires_at: datetime | None = None
    token_type: str = "Bearer"

    def is_expired(self) -> bool:
        """Return True if the token has expired."""
        if self.expires_at is None:
            return False
        now = datetime.now(tz=timezone.utc)
        # Normalise expires_at to UTC if it has no tzinfo
        exp = self.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return now >= exp

    def to_dict(self) -> dict[str, object]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at.isoformat() if self.expires_at is not None else None,
            "token_type": self.token_type,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> OAuthToken:
        expires_raw = data.get("expires_at")
        expires_at: datetime | None = None
        if isinstance(expires_raw, str):
            expires_at = datetime.fromisoformat(expires_raw)
        return cls(
            access_token=str(data["access_token"]),
            refresh_token=str(data["refresh_token"]) if data.get("refresh_token") is not None else None,
            expires_at=expires_at,
            token_type=str(data.get("token_type", "Bearer")),
        )


class CredentialStore(ABC):
    """Abstract base class for OAuth token storage backends."""

    @abstractmethod
    def save_oauth_token(self, token: OAuthToken) -> None:
        """Persist an OAuth token."""

    @abstractmethod
    def load_oauth_token(self) -> OAuthToken | None:
        """Load a stored OAuth token, or return None if absent."""

    @abstractmethod
    def delete_oauth_token(self) -> None:
        """Remove any stored OAuth token."""

    @abstractmethod
    def status(self) -> dict[str, object]:
        """Return store status without exposing token material."""


class FileCredentialStore(CredentialStore):
    """Stores OAuth tokens in `~/.config/jazzband/credentials.json`."""

    def __init__(
        self,
        path: str | Path | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._path = path
        self._environ = environ

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _credentials_path(self) -> Path:
        return (
            Path(self._path).expanduser()
            if self._path is not None
            else default_credentials_path(self._environ)
        )

    def _load_raw(self) -> dict[str, object]:
        creds_path = self._credentials_path()
        try:
            payload = json.loads(creds_path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    # ------------------------------------------------------------------
    # CredentialStore interface
    # ------------------------------------------------------------------

    def save_oauth_token(self, token: OAuthToken) -> None:
        # Merge OAuth fields into the `linear` sub-object so they coexist with
        # any stored api_key without clobbering it.
        creds_path = self._credentials_path()
        creds_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            creds_path.parent.chmod(0o700)
        except OSError:
            pass
        try:
            existing: dict[str, object] = json.loads(creds_path.read_text(encoding="utf-8"))
            if not isinstance(existing, dict):
                existing = {}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            existing = {}
        linear = existing.get("linear")
        if not isinstance(linear, dict):
            linear = {}
        linear.update(token.to_dict())
        existing["linear"] = linear
        creds_path.write_text(
            json.dumps(existing, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        try:
            creds_path.chmod(0o600)
        except OSError:
            pass

    def load_oauth_token(self) -> OAuthToken | None:
        raw = self._load_raw()
        linear = raw.get("linear")
        if not isinstance(linear, dict):
            return None
        if not isinstance(linear.get("access_token"), str):
            return None
        try:
            return OAuthToken.from_dict(linear)
        except (KeyError, ValueError):
            return None

    def delete_oauth_token(self) -> None:
        creds_path = self._credentials_path()
        try:
            payload: dict[str, object] = json.loads(creds_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
        linear = payload.get("linear")
        if isinstance(linear, dict):
            for key in ("access_token", "token_type", "refresh_token", "expires_at"):
                linear.pop(key, None)
        creds_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        try:
            creds_path.chmod(0o600)
        except OSError:
            pass

    def status(self) -> dict[str, object]:
        token = self.load_oauth_token()
        return {
            "source": "file",
            "authenticated": token is not None and not token.is_expired(),
            "expires_at": token.expires_at.isoformat() if token is not None and token.expires_at is not None else None,
        }


class KeychainCredentialStore(CredentialStore):
    """Stores OAuth tokens in macOS Keychain via the `keyring` library."""

    _SERVICE = "jazzband"
    _USERNAME = "linear_oauth"

    def save_oauth_token(self, token: OAuthToken) -> None:
        try:
            import keyring  # noqa: PLC0415
            import keyring.errors  # noqa: PLC0415
        except ImportError as exc:
            raise CredentialStoreError("keyring is not installed") from exc
        try:
            keyring.set_password(self._SERVICE, self._USERNAME, json.dumps(token.to_dict()))
        except keyring.errors.NoKeyringError as exc:
            raise CredentialStoreError("no keyring backend available") from exc

    def load_oauth_token(self) -> OAuthToken | None:
        try:
            import keyring  # noqa: PLC0415
            import keyring.errors  # noqa: PLC0415
        except ImportError as exc:
            raise CredentialStoreError("keyring is not installed") from exc
        try:
            raw = keyring.get_password(self._SERVICE, self._USERNAME)
        except keyring.errors.NoKeyringError as exc:
            raise CredentialStoreError("no keyring backend available") from exc
        if raw is None:
            return None
        try:
            data = json.loads(raw)
            return OAuthToken.from_dict(data)
        except (json.JSONDecodeError, KeyError, ValueError):
            return None

    def delete_oauth_token(self) -> None:
        try:
            import keyring  # noqa: PLC0415
            import keyring.errors  # noqa: PLC0415
        except ImportError as exc:
            raise CredentialStoreError("keyring is not installed") from exc
        try:
            keyring.delete_password(self._SERVICE, self._USERNAME)
        except keyring.errors.NoKeyringError as exc:
            raise CredentialStoreError("no keyring backend available") from exc
        except keyring.errors.PasswordDeleteError:
            # Credential not found — already absent, nothing to revoke.
            pass
        except Exception as exc:
            raise CredentialStoreError(f"keychain delete failed: {exc}") from exc

    def status(self) -> dict[str, object]:
        try:
            token = self.load_oauth_token()
        except CredentialStoreError:
            return {"source": "keychain", "authenticated": False, "expires_at": None}
        return {
            "source": "keychain",
            "authenticated": token is not None and not token.is_expired(),
            "expires_at": token.expires_at.isoformat() if token is not None and token.expires_at is not None else None,
        }


def default_credential_store(environ: Mapping[str, str] | None = None) -> CredentialStore:
    """Return the best available credential store for this platform."""
    if sys.platform == "darwin":
        try:
            import keyring  # noqa: F401, PLC0415
            return KeychainCredentialStore()
        except ImportError:
            pass
    return FileCredentialStore(environ=environ)


@dataclass(frozen=True)
class TokenStore:
    tracker: TrackerConfig
    environ: Mapping[str, str] | None = None
    credentials_path: Path | None = None

    def resolve_linear_token(self) -> str:
        env = self.environ if self.environ is not None else os.environ

        env_token = _non_empty(env.get("LINEAR_API_KEY"))
        if env_token is not None:
            return env_token

        configured = self.tracker.api_key
        if configured is not None:
            if configured.startswith("$"):
                resolved = _non_empty(env.get(configured[1:]))
                if resolved is not None:
                    return resolved
            else:
                resolved = _non_empty(configured)
                if resolved is not None:
                    return resolved

        # OAuth token lookup: Keychain first (when no explicit path), then file.
        # Resolution order per PRD: env → WORKFLOW → Keychain → credentials file.
        # When a custom credentials_path is set (e.g. tests or --credentials-path),
        # skip Keychain and go directly to the file-backed store.
        if self.credentials_path is None:
            try:
                keychain = KeychainCredentialStore()
                oauth = keychain.load_oauth_token()
                if oauth is not None and not oauth.is_expired():
                    return f"{oauth.token_type} {oauth.access_token}"
                # Expired Keychain OAuth: fall through to file/api_key lookup.
                # This patch does not refresh tokens, so an expired record must
                # not permanently block other valid credentials.
            except CredentialStoreError:
                pass

        try:
            file_store = FileCredentialStore(path=self.credentials_path, environ=env)
            oauth = file_store.load_oauth_token()
            if oauth is not None and not oauth.is_expired():
                return f"{oauth.token_type} {oauth.access_token}"
            # Expired or absent file OAuth: fall through to legacy api_key lookup.
            # Unlike Keychain, the credentials file may also carry a valid api_key
            # that should not be blocked by a stale OAuth record.
        except CredentialStoreError:
            pass

        stored = load_local_linear_token(path=self.credentials_path, environ=env)
        if stored is not None:
            return stored

        raise MissingLinearTokenError("missing_tracker_api_key")


def default_credentials_path(environ: Mapping[str, str] | None = None) -> Path:
    env = environ if environ is not None else os.environ
    configured_home = _non_empty(env.get("XDG_CONFIG_HOME"))
    if configured_home is not None:
        return Path(configured_home).expanduser() / "jazzband" / DEFAULT_CREDENTIALS_FILE
    return Path.home() / DEFAULT_CREDENTIALS_DIR / DEFAULT_CREDENTIALS_FILE


def load_local_linear_token(
    *,
    path: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    credentials_path = Path(path).expanduser() if path is not None else default_credentials_path(environ)
    try:
        payload = json.loads(credentials_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError):
        return None

    if not isinstance(payload, dict):
        return None
    linear = payload.get("linear")
    if not isinstance(linear, dict):
        return None
    token = linear.get("api_key")
    return _non_empty(token) if isinstance(token, str) else None


def save_local_linear_token(
    token: str,
    *,
    path: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    resolved = _non_empty(token)
    if resolved is None:
        raise MissingLinearTokenError("missing_tracker_api_key")
    return _save_credentials({"linear": {"api_key": resolved}}, path=path, environ=environ)


def load_local_github_token(
    *,
    path: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    credentials_path = Path(path).expanduser() if path is not None else default_credentials_path(environ)
    try:
        payload = json.loads(credentials_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(payload, dict):
        return None
    github = payload.get("github")
    if not isinstance(github, dict):
        return None
    token = github.get("token")
    return _non_empty(token) if isinstance(token, str) else None


def save_local_github_token(
    token: str,
    *,
    path: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    resolved = _non_empty(token)
    if resolved is None:
        raise ValueError("empty_github_token")
    return _save_credentials({"github": {"token": resolved}}, path=path, environ=environ)


def _save_credentials(
    updates: dict[str, object],
    *,
    path: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    credentials_path = Path(path).expanduser() if path is not None else default_credentials_path(environ)
    credentials_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        credentials_path.parent.chmod(0o700)
    except OSError:
        pass

    try:
        existing: dict[str, object] = json.loads(credentials_path.read_text(encoding="utf-8"))
        if not isinstance(existing, dict):
            existing = {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        existing = {}

    existing.update(updates)
    credentials_path.write_text(
        json.dumps(existing, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    try:
        credentials_path.chmod(0o600)
    except OSError:
        pass
    return credentials_path


def redact_secret(text: object, secrets: list[str | None]) -> str:
    redacted = str(text)
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def redact_tokens(text: str, token: OAuthToken | None) -> str:
    """Replace access_token and refresh_token values in *text* with '[REDACTED]'."""
    if token is None:
        return text
    result = text
    if token.access_token:
        result = result.replace(token.access_token, "[REDACTED]")
    if token.refresh_token:
        result = result.replace(token.refresh_token, "[REDACTED]")
    return result


def _non_empty(value: str | None) -> str | None:
    if value is None:
        return None

    trimmed = value.strip()
    return trimmed or None
