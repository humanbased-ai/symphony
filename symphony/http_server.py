from __future__ import annotations

import asyncio
import inspect
import json
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit


API_PREFIX = "/api/v1"
JSON_HEADERS = {"content-type": "application/json; charset=utf-8"}


StateProvider = Callable[[], Any]
RefreshCallback = Callable[[], Any]


@dataclass(frozen=True)
class HTTPResponse:
    status_code: int
    body: dict[str, Any]
    headers: dict[str, str]
    raw_body: bytes | None = None

    def json_bytes(self) -> bytes:
        return json.dumps(self.body, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def response_bytes(self) -> bytes:
        return self.raw_body if self.raw_body is not None else self.json_bytes()


@dataclass(frozen=True)
class StatusAPI:
    state_provider: StateProvider
    refresh_callback: RefreshCallback | None = None
    started_at: datetime | None = None
    monotonic_started_at: float | None = None

    def __post_init__(self) -> None:
        if self.started_at is None:
            object.__setattr__(self, "started_at", _utc_now())
        if self.monotonic_started_at is None:
            object.__setattr__(self, "monotonic_started_at", time.monotonic())

    def handle_request(self, method: str, path: str, body: bytes | str | None = None) -> HTTPResponse:
        method = method.upper()
        route = _normalized_path(path)
        now = _utc_now()

        if route == f"{API_PREFIX}/health":
            if method != "GET":
                return _error_response(405, "method_not_allowed", "GET is required for this endpoint")
            snapshot = build_state_snapshot(self.state_provider(), now=now)
            uptime_s = max(time.monotonic() - float(self.monotonic_started_at or time.monotonic()), 0.0)
            return _json_response(
                200,
                {
                    "status": "ok",
                    "running": snapshot["counts"]["running"],
                    "uptime_s": round(uptime_s, 3),
                    "generated_at": _iso_datetime(now),
                },
            )

        if route == f"{API_PREFIX}/state":
            if method != "GET":
                return _error_response(405, "method_not_allowed", "GET is required for this endpoint")
            return _json_response(200, build_state_snapshot(self.state_provider(), now=now))

        if route == f"{API_PREFIX}/refresh":
            if method != "POST":
                return _error_response(405, "method_not_allowed", "POST is required for this endpoint")
            if not _body_is_empty_json(body):
                return _error_response(400, "invalid_json", "refresh accepts an empty JSON object")
            if self.refresh_callback is None:
                return _error_response(503, "refresh_unavailable", "no refresh callback is configured")
            result = self.refresh_callback()
            if inspect.isawaitable(result):
                if inspect.iscoroutine(result):
                    result.close()
                return _error_response(500, "async_refresh_callback", "use async_handle_request for async refresh callbacks")
            return _json_response(202, _refresh_response(result, now=now))

        issue_identifier = _issue_identifier_from_route(route)
        if issue_identifier is not None:
            if method != "GET":
                return _error_response(405, "method_not_allowed", "GET is required for this endpoint")
            detail = build_issue_detail(self.state_provider(), issue_identifier, now=now)
            if detail is None:
                return _error_response(404, "issue_not_found", f"issue {issue_identifier!r} is not in current state")
            return _json_response(200, detail)

        return _error_response(404, "route_not_found", f"unknown API route: {route}")

    async def async_handle_request(self, method: str, path: str, body: bytes | str | None = None) -> HTTPResponse:
        method = method.upper()
        route = _normalized_path(path)
        now = _utc_now()
        if route != f"{API_PREFIX}/refresh" or method != "POST" or self.refresh_callback is None:
            return self.handle_request(method, path, body)
        if not _body_is_empty_json(body):
            return _error_response(400, "invalid_json", "refresh accepts an empty JSON object")

        result = self.refresh_callback()
        if inspect.isawaitable(result):
            result = await result
        return _json_response(202, _refresh_response(result, now=now))


WEBHOOK_LINEAR_ROUTE = f"{API_PREFIX}/webhooks/linear"

# Imported lazily to avoid circular imports at module load time.
_LinearWebhookEvent = None


def _get_webhook_types() -> tuple[Any, Any]:
    """Lazy-import webhook helpers to avoid a hard dependency at import time."""
    from symphony.tracker.webhooks import LinearWebhookEvent, parse_webhook_payload, verify_signature  # noqa: PLC0415
    return LinearWebhookEvent, parse_webhook_payload, verify_signature


EventCallback = Callable[["Any"], Awaitable[None]]


@dataclass
class WebhookAPI:
    """Handles POST /api/v1/webhooks/linear.

    webhook_secret — when set, every request must carry a valid X-Linear-Signature.
      Mutable so RuntimeWorkflowReloader can rotate it on WORKFLOW.md reload without
      restarting the HTTP server.
    on_event — async callable invoked for each valid LinearWebhookEvent.
    """

    webhook_secret: str | None = None
    on_event: EventCallback | None = field(default=None, compare=False)

    async def async_handle_request(
        self,
        method: str,
        path: str,
        body: bytes | None,
        headers: Mapping[str, str] | None = None,
    ) -> HTTPResponse:
        method = method.upper()
        route = _normalized_path(path)

        if route != WEBHOOK_LINEAR_ROUTE:
            return _error_response(404, "route_not_found", f"unknown API route: {route}")

        if method != "POST":
            return _error_response(405, "method_not_allowed", "POST is required for this endpoint")

        raw_body = body if body is not None else b""
        headers = headers or {}
        signature = _header_value(headers, "x-linear-signature")

        if self.webhook_secret is not None:
            if not signature:
                return _error_response(401, "missing_signature", "X-Linear-Signature header is required")
            _, _, verify_sig = _get_webhook_types()
            if not verify_sig(raw_body, signature, self.webhook_secret):
                return _error_response(401, "invalid_signature", "HMAC-SHA256 signature mismatch")

        _, parse_payload, _ = _get_webhook_types()
        event = parse_payload(raw_body)
        if event is None:
            return _error_response(400, "malformed_payload", "could not parse webhook payload")

        if self.on_event is not None:
            result = self.on_event(event)
            if inspect.isawaitable(result):
                # Schedule the tick in the background so Linear's POST receives
                # 200 immediately. Awaiting run_tick() here would keep the
                # connection open for the full agent turn timeout.
                asyncio.create_task(result)

        return _json_response(200, {"ok": True})


def build_state_snapshot(state: Any, *, now: datetime | None = None) -> dict[str, Any]:
    now = now or _utc_now()
    running = [_running_summary(entry, now=now) for entry in _collection_values(_field(state, "running", default={}))]
    retrying = [_retry_summary(entry) for entry in _collection_values(_field(state, "retry_attempts", "retrying", default={}))]
    completed = _field(state, "completed", default=set())
    claimed = _field(state, "claimed", default=set())

    return {
        "generated_at": _iso_datetime(now),
        "counts": {
            "running": len(running),
            "retrying": len(retrying),
            "completed": len(completed) if hasattr(completed, "__len__") else 0,
            "claimed": len(claimed) if hasattr(claimed, "__len__") else 0,
        },
        "running": running,
        "retrying": retrying,
        "codex_totals": _codex_totals(state, running),
        "rate_limits": _jsonable(_field(state, "rate_limits", default=None)),
        "recent_events": _recent_events(state),
    }


def build_issue_detail(state: Any, issue_identifier: str, *, now: datetime | None = None) -> dict[str, Any] | None:
    now = now or _utc_now()
    running_entry = _find_running_entry(state, issue_identifier)
    retry_entry = _find_retry_entry(state, issue_identifier)
    if running_entry is None and retry_entry is None:
        return None

    source = running_entry if running_entry is not None else retry_entry
    issue = _field(source, "issue", default={})
    identifier = _field(issue, "identifier", default=None) or _field(source, "identifier", "issue_identifier", default=issue_identifier)
    issue_id = _field(issue, "id", default=None) or _field(source, "issue_id", "id", default=None)
    running_summary = _running_summary(running_entry, now=now) if running_entry is not None else None
    retry_summary = _retry_summary(retry_entry) if retry_entry is not None else None
    last_error = _field(running_entry, "last_error", default=None) if running_entry is not None else None
    if last_error is None and retry_entry is not None:
        last_error = _field(retry_entry, "error", default=None)

    return {
        "issue_identifier": identifier,
        "issue_id": issue_id,
        "status": "running" if running_entry is not None else "retrying",
        "workspace": _workspace_payload(running_entry),
        "attempts": {
            "restart_count": _int_or_zero(_field(source, "restart_count", default=0)),
            "current_retry_attempt": _field(source, "retry_attempt", "attempt", default=None),
        },
        "running": running_summary,
        "retry": retry_summary,
        "logs": _logs_payload(source),
        "recent_events": _recent_events(source),
        "last_error": _jsonable(last_error),
        "tracked": _tracked_payload(issue),
    }


def create_fastapi_app(status_api: StatusAPI) -> Any:
    try:
        from fastapi import FastAPI
        from fastapi.responses import JSONResponse
    except ModuleNotFoundError as exc:
        raise RuntimeError("fastapi_unavailable") from exc

    app = FastAPI()

    @app.get(f"{API_PREFIX}/health")
    def health() -> JSONResponse:
        response = status_api.handle_request("GET", f"{API_PREFIX}/health")
        return JSONResponse(response.body, status_code=response.status_code)

    @app.get(f"{API_PREFIX}/state")
    def state() -> JSONResponse:
        response = status_api.handle_request("GET", f"{API_PREFIX}/state")
        return JSONResponse(response.body, status_code=response.status_code)

    @app.get(f"{API_PREFIX}/{{issue_identifier}}")
    def issue(issue_identifier: str) -> JSONResponse:
        response = status_api.handle_request("GET", f"{API_PREFIX}/{issue_identifier}")
        return JSONResponse(response.body, status_code=response.status_code)

    @app.post(f"{API_PREFIX}/refresh")
    async def refresh() -> JSONResponse:
        response = await status_api.async_handle_request("POST", f"{API_PREFIX}/refresh")
        return JSONResponse(response.body, status_code=response.status_code)

    return app


def _running_summary(entry: Any, *, now: datetime) -> dict[str, Any]:
    issue = _field(entry, "issue", default={})
    started_at = _timestamp(_field(entry, "started_at", "started_at_ms", default=None))
    last_event_at = _timestamp(_field(entry, "last_event_at", "last_event_at_ms", default=None))
    return {
        "issue_id": _field(issue, "id", default=None) or _field(entry, "issue_id", "id", default=None),
        "issue_identifier": _field(issue, "identifier", default=None)
        or _field(entry, "identifier", "issue_identifier", default=None),
        "state": _field(issue, "state", default=None) or _field(entry, "state", default=None),
        "session_id": _field(entry, "session_id", default=None),
        "turn_count": _int_or_zero(_field(entry, "turn_count", default=0)),
        "last_event": _field(entry, "last_event", default=None),
        "last_message": _field(entry, "last_message", default=None),
        "started_at": _iso_datetime(started_at) if started_at is not None else None,
        "last_event_at": _iso_datetime(last_event_at) if last_event_at is not None else None,
        "tokens": _tokens(entry),
        "seconds_running": _seconds_since(started_at, now) if started_at is not None else 0.0,
    }


def _retry_summary(entry: Any) -> dict[str, Any]:
    due_at = _timestamp(_field(entry, "due_at", "due_at_ms", default=None))
    return {
        "issue_id": _field(entry, "issue_id", "id", default=None),
        "issue_identifier": _field(entry, "identifier", "issue_identifier", default=None),
        "attempt": _field(entry, "attempt", default=None),
        "due_at": _iso_datetime(due_at) if due_at is not None else None,
        "error": _field(entry, "error", default=None),
    }


def _codex_totals(state: Any, running: list[dict[str, Any]]) -> dict[str, Any]:
    configured = _field(state, "codex_totals", default=None)
    if configured is not None:
        value = _jsonable(configured)
        if isinstance(value, dict):
            return {
                "input_tokens": _int_or_zero(value.get("input_tokens")),
                "output_tokens": _int_or_zero(value.get("output_tokens")),
                "total_tokens": _int_or_zero(value.get("total_tokens")),
                "seconds_running": float(value.get("seconds_running") or 0),
            }

    input_tokens = sum(item["tokens"]["input_tokens"] for item in running)
    output_tokens = sum(item["tokens"]["output_tokens"] for item in running)
    total_tokens = sum(item["tokens"]["total_tokens"] for item in running)
    seconds_running = sum(float(item.get("seconds_running") or 0) for item in running)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "seconds_running": round(seconds_running, 3),
    }


def _refresh_response(result: Any, *, now: datetime) -> dict[str, Any]:
    base = {
        "queued": True,
        "coalesced": False,
        "requested_at": _iso_datetime(now),
        "operations": ["poll", "reconcile"],
    }
    if result is None:
        return base
    if isinstance(result, bool):
        base["queued"] = result
        return base
    value = _jsonable(result)
    if isinstance(value, Mapping):
        base.update(value)
        base.setdefault("requested_at", _iso_datetime(now))
        base.setdefault("operations", ["poll", "reconcile"])
    return base


def _find_running_entry(state: Any, issue_identifier: str) -> Any | None:
    for entry in _collection_values(_field(state, "running", default={})):
        if _matches_issue(entry, issue_identifier):
            return entry
    return None


def _find_retry_entry(state: Any, issue_identifier: str) -> Any | None:
    for entry in _collection_values(_field(state, "retry_attempts", "retrying", default={})):
        if _matches_issue(entry, issue_identifier):
            return entry
    return None


def _matches_issue(entry: Any, issue_identifier: str) -> bool:
    issue = _field(entry, "issue", default={})
    candidates = (
        _field(issue, "identifier", default=None),
        _field(issue, "id", default=None),
        _field(entry, "identifier", "issue_identifier", default=None),
        _field(entry, "issue_id", "id", default=None),
    )
    return any(str(candidate).lower() == issue_identifier.lower() for candidate in candidates if candidate)


def _tokens(entry: Any) -> dict[str, int]:
    token_source = _field(entry, "tokens", default={})
    input_tokens = _int_or_zero(_field(token_source, "input_tokens", default=None) or _field(entry, "input_tokens", default=0))
    output_tokens = _int_or_zero(
        _field(token_source, "output_tokens", default=None) or _field(entry, "output_tokens", default=0)
    )
    total_tokens = _int_or_zero(_field(token_source, "total_tokens", default=None) or _field(entry, "total_tokens", default=0))
    if total_tokens == 0:
        total_tokens = input_tokens + output_tokens
    return {"input_tokens": input_tokens, "output_tokens": output_tokens, "total_tokens": total_tokens}


def _workspace_payload(entry: Any) -> dict[str, Any] | None:
    if entry is None:
        return None
    workspace = _field(entry, "workspace", default=None)
    path = _field(workspace, "path", default=None) if workspace is not None else None
    path = path or _field(entry, "workspace_path", default=None)
    if path is None:
        return None
    return {"path": str(path)}


def _logs_payload(source: Any) -> dict[str, Any]:
    logs = _field(source, "logs", default=None)
    if logs is not None:
        value = _jsonable(logs)
        return value if isinstance(value, dict) else {"entries": value}

    log_paths = _field(source, "log_paths", "codex_session_logs", default=())
    entries = []
    for index, path in enumerate(log_paths or ()):
        entries.append({"label": "latest" if index == 0 else f"log-{index + 1}", "path": str(path), "url": None})
    return {"codex_session_logs": entries}


def _tracked_payload(issue: Any) -> dict[str, Any]:
    if issue is None:
        return {}
    return {
        key: value
        for key, value in {
            "title": _field(issue, "title", default=None),
            "description": _field(issue, "description", default=None),
            "priority": _field(issue, "priority", default=None),
            "state": _field(issue, "state", default=None),
            "branch_name": _field(issue, "branch_name", default=None),
            "url": _field(issue, "url", default=None),
            "labels": _field(issue, "labels", default=None),
        }.items()
        if value is not None
    }


def _recent_events(source: Any) -> list[dict[str, Any]]:
    events = _field(source, "recent_events", "events", default=())
    result = []
    for event in events or ():
        value = _jsonable(event)
        if isinstance(value, Mapping):
            result.append(dict(value))
        else:
            result.append({"message": str(value)})
    return result


def _field(source: Any, *names: str, default: Any = None) -> Any:
    if source is None:
        return default
    for name in names:
        if isinstance(source, Mapping) and name in source:
            return source[name]
        if hasattr(source, name):
            return getattr(source, name)
    return default


def _collection_values(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        return list(value.values())
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return list(value) if hasattr(value, "__iter__") and not isinstance(value, (str, bytes)) else []


def _timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        number = float(value)
        if number <= 0:
            return None
        if number > 10_000_000_000:
            number = number / 1000
        return datetime.fromtimestamp(number, timezone.utc)
    return None


def _iso_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _seconds_since(started_at: datetime, now: datetime) -> float:
    return round(max((now - started_at).total_seconds(), 0.0), 3)


def _int_or_zero(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return {key: _jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if isinstance(value, datetime):
        return _iso_datetime(value)
    if isinstance(value, Path):
        return str(value)
    return value


def _body_is_empty_json(body: bytes | str | None) -> bool:
    if body is None:
        return True
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")
    if not body.strip():
        return True
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return False
    return parsed == {}


def _normalized_path(path: str) -> str:
    parsed = urlsplit(path)
    route = parsed.path.rstrip("/") or "/"
    return unquote(route)


def _issue_identifier_from_route(route: str) -> str | None:
    prefix = f"{API_PREFIX}/"
    if not route.startswith(prefix):
        return None
    tail = route[len(prefix) :]
    if not tail or "/" in tail or tail in {"health", "state", "refresh"}:
        return None
    return tail


def _json_response(status_code: int, body: dict[str, Any]) -> HTTPResponse:
    return HTTPResponse(status_code=status_code, body=body, headers=dict(JSON_HEADERS))


def _error_response(status_code: int, code: str, message: str) -> HTTPResponse:
    return _json_response(status_code, {"error": {"code": code, "message": message}})


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# OAuth API — /api/v1/linear/auth/* endpoints (desktop/setup integration)
# ---------------------------------------------------------------------------

_OAUTH_PREFIX = f"{API_PREFIX}/linear/auth"
_CALLBACK_HTML_OK = b"<!doctype html><html><body><h1>Authenticated!</h1><p>You may close this window.</p></body></html>"
_CALLBACK_HTML_ERR = b"<!doctype html><html><body><h1>Authentication failed.</h1><p>You may close this window.</p></body></html>"


@dataclass
class OAuthAPI:
    """Handles Linear OAuth endpoints for desktop/setup integration.

    Pending PKCE challenges are stored in-memory and expire after
    CALLBACK_TIMEOUT_SECONDS from the /start call.

    ``server_port`` is the port this API is served on; it is used as the
    default redirect port when ``/start`` is called without an explicit
    ``port`` in the request body.  Without it, the redirect URI would point
    at CALLBACK_PORT (7338) even though the daemon listens on 7337 (or
    whatever ``symphony run --port`` specifies).

    No token material is ever returned in any response body — only an
    ``authenticated`` flag, ``source``, and ``expires_at`` are surfaced.
    """

    credential_store: Any | None = None  # CredentialStore, optional
    server_port: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "_pending", {})  # state -> (PKCEChallenge, client_id, client_secret, redirect_uri)

    def handle_request(self, method: str, path: str, body: bytes | str | None = None) -> HTTPResponse | None:
        """Return a response if the path is an OAuth route, else None."""
        method = method.upper()
        route = _normalized_path(path)

        if route == f"{_OAUTH_PREFIX}/status":
            if method != "GET":
                return _error_response(405, "method_not_allowed", "GET required")
            return self._handle_status()

        if route == f"{_OAUTH_PREFIX}/start":
            if method != "POST":
                return _error_response(405, "method_not_allowed", "POST required")
            return self._handle_start(body)

        if route == f"{_OAUTH_PREFIX}/callback":
            if method != "GET":
                return _error_response(405, "method_not_allowed", "GET required")
            return self._handle_callback(path)

        if route == f"{_OAUTH_PREFIX}/revoke":
            if method != "POST":
                return _error_response(405, "method_not_allowed", "POST required")
            return self._handle_revoke()

        return None

    def _handle_status(self) -> HTTPResponse:
        if self.credential_store is None:
            return _json_response(200, {"authenticated": False, "source": "none", "expires_at": None})
        try:
            status = self.credential_store.status()
        except Exception as exc:
            return _json_response(200, {"authenticated": False, "source": "error", "error": str(exc)})
        return _json_response(200, {k: status.get(k) for k in ("authenticated", "source", "expires_at")})

    def _handle_start(self, body: bytes | str | None) -> HTTPResponse:
        try:
            from symphony.tracker.linear_oauth import (  # noqa: PLC0415
                CALLBACK_PATH,
                CALLBACK_PORT,
                PKCEChallenge,
                build_authorization_url,
            )
        except ImportError as exc:
            return _error_response(500, "oauth_unavailable", str(exc))

        raw: dict[str, object] = {}
        if body:
            try:
                raw = json.loads(body if isinstance(body, str) else body.decode())
            except (json.JSONDecodeError, UnicodeDecodeError):
                return _error_response(400, "invalid_json", "request body must be JSON")

        client_id = raw.get("client_id") or ""
        if not isinstance(client_id, str) or not client_id.strip():
            return _error_response(400, "missing_client_id", "client_id is required")
        default_port = self.server_port if self.server_port is not None else CALLBACK_PORT
        port = int(raw.get("port") or default_port)
        redirect_uri = f"http://localhost:{port}{CALLBACK_PATH}"

        pkce = PKCEChallenge.generate()
        pending: dict[str, object] = object.__getattribute__(self, "_pending")
        pending[pkce.state] = (pkce, client_id.strip(), raw.get("client_secret") or None, redirect_uri)

        auth_url = build_authorization_url(client_id.strip(), redirect_uri, pkce)
        return _json_response(200, {"authorization_url": auth_url, "state": pkce.state, "redirect_uri": redirect_uri})

    def _handle_callback(self, raw_path: str) -> HTTPResponse:
        from urllib.parse import parse_qs, urlsplit  # noqa: PLC0415
        try:
            from symphony.tracker.linear_oauth import exchange_code, parse_token_response  # noqa: PLC0415
        except ImportError as exc:
            return _error_response(500, "oauth_unavailable", str(exc))

        params = parse_qs(urlsplit(raw_path).query)
        code = (params.get("code") or [None])[0]
        state = (params.get("state") or [None])[0]
        error = (params.get("error") or [None])[0]

        if error:
            return HTTPResponse(
                status_code=400,
                body={"error": {"code": "oauth_denied", "message": error}},
                headers={"content-type": "text/html; charset=utf-8"},
                raw_body=_CALLBACK_HTML_ERR,
            )

        pending: dict[str, object] = object.__getattribute__(self, "_pending")
        entry = pending.pop(state, None) if state else None
        if entry is None or not code:
            return HTTPResponse(
                status_code=400,
                body={"error": {"code": "invalid_callback", "message": "unknown state or missing code"}},
                headers={"content-type": "text/html; charset=utf-8"},
                raw_body=_CALLBACK_HTML_ERR,
            )

        pkce, client_id, client_secret, redirect_uri = entry
        try:
            token_data = exchange_code(str(client_id), client_secret, code, str(redirect_uri), pkce.code_verifier)
            token = parse_token_response(token_data)
            if self.credential_store is not None:
                self.credential_store.save_oauth_token(token)
        except Exception as exc:
            return _error_response(500, "token_exchange_failed", str(exc))

        return HTTPResponse(
            status_code=200,
            body={},
            headers={"content-type": "text/html; charset=utf-8"},
            raw_body=_CALLBACK_HTML_OK,
        )

    def _handle_revoke(self) -> HTTPResponse:
        if self.credential_store is None:
            return _json_response(200, {"revoked": False, "reason": "no_credential_store"})
        try:
            token = self.credential_store.load_oauth_token()
            if token is None:
                return _json_response(200, {"revoked": False, "reason": "not_authenticated"})
            self.credential_store.delete_oauth_token()
        except Exception as exc:
            return _error_response(500, "revoke_failed", str(exc))
        return _json_response(200, {"revoked": True})


def _header_value(headers: Mapping[str, str], name: str) -> str:
    """Case-insensitive header lookup. Returns empty string if not found."""
    lower = name.lower()
    for key, value in headers.items():
        if key.lower() == lower:
            return value
    return ""
