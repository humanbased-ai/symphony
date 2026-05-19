"""Per-project mutual exclusion: prevents two Symphony daemons from processing
the same Linear project simultaneously.

Two layers:
  FileLock     — file-based PID lock; always active; catches same-machine conflicts.
  WebhookClaim — encodes a claim heartbeat in the Linear webhook label; active when
                 webhook mode is configured; catches cross-machine conflicts.

Both layers treat a claim as stale after STALE_THRESHOLD_S seconds of silence,
so a crashed daemon releases its claim automatically without manual cleanup.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import socket
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from symphony.tracker.webhooks import WebhookRegistrar

LOGGER = logging.getLogger(__name__)

LOCK_FILE_NAME = ".symphony.lock"
HEARTBEAT_INTERVAL_S = 60
STALE_THRESHOLD_S = 300  # 5 minutes without heartbeat → claim is dead

_CLAIM_PREFIX = "symphony-claim:"


class ProjectLockError(RuntimeError):
    """Raised when another Symphony daemon already holds this project."""


@dataclass
class LockInfo:
    pid: int
    host: str
    started_at: str
    heartbeat: str

    @classmethod
    def for_current_process(cls) -> "LockInfo":
        now = _utcnow()
        return cls(pid=os.getpid(), host=socket.gethostname(), started_at=now, heartbeat=now)

    def is_stale(self) -> bool:
        try:
            age_s = time.time() - datetime.fromisoformat(self.heartbeat).timestamp()
            return age_s > STALE_THRESHOLD_S
        except (ValueError, TypeError, OSError):
            return True

    def is_alive(self) -> bool:
        """True if the owning process appears to still be running."""
        if not self.is_stale():
            return True
        # Stale heartbeat: do a process liveness check only when on the same host.
        if socket.gethostname() == self.host:
            try:
                os.kill(self.pid, 0)
                return True
            except ProcessLookupError:
                return False
            except PermissionError:
                return True  # process exists, no permission to signal — treat as alive
        # Different host with stale heartbeat → assume dead.
        return False


class FileLock:
    """Atomic file-based PID lock stored under workspace_root."""

    def __init__(self, workspace_root: Path) -> None:
        self._path = workspace_root / LOCK_FILE_NAME

    def acquire(self) -> LockInfo:
        """Acquire the lock for the current process.

        Raises ProjectLockError if an active lock from another process exists.
        Silently replaces a stale lock.
        """
        existing = self._read()
        if existing is not None and existing.is_alive():
            raise ProjectLockError(
                f"Symphony is already running for this project.\n"
                f"  Host: {existing.host}  PID: {existing.pid}  "
                f"Active since: {existing.started_at}\n"
                f"Stop that instance first, or delete {self._path} if it is stale."
            )
        if existing is not None:
            LOGGER.warning(
                "Stale project lock found (host=%s pid=%s) — taking over.",
                existing.host,
                existing.pid,
            )
        mine = LockInfo.for_current_process()
        self._write(mine)
        return mine

    def update(self, info: LockInfo) -> LockInfo:
        """Refresh the heartbeat timestamp and persist."""
        refreshed = dataclasses.replace(info, heartbeat=_utcnow())
        self._write(refreshed)
        return refreshed

    def release(self) -> None:
        try:
            self._path.unlink(missing_ok=True)
        except OSError as exc:
            LOGGER.debug("Could not remove project lock file: %s", exc)

    def _read(self) -> LockInfo | None:
        try:
            return LockInfo(**json.loads(self._path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            return None
        except Exception:  # noqa: BLE001
            LOGGER.debug("Ignoring unreadable project lock at %s.", self._path)
            return None

    def _write(self, info: LockInfo) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(dataclasses.asdict(info)), encoding="utf-8")
        tmp.replace(self._path)


class WebhookClaim:
    """Cross-machine claim encoded in the Linear webhook label field.

    Requires webhook mode to be configured (url + team_id). The claim is embedded
    as a JSON payload in the webhook's label so that other Symphony instances can
    detect it when listing webhooks for the same team.
    """

    def __init__(
        self,
        registrar: "WebhookRegistrar",
        team_id: str,
        webhook_url: str,
    ) -> None:
        self._registrar = registrar
        self._team_id = team_id
        self._webhook_url = webhook_url
        self._webhook_id: str | None = None

    async def check(self) -> None:
        """Raise ProjectLockError if an active claim from a different instance exists."""
        try:
            webhooks = await self._registrar.list_webhooks(self._team_id)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Could not list webhooks for project conflict check: %s", exc)
            return  # fail-open: let startup proceed if Linear is unreachable
        for wh in webhooks:
            label = (wh.get("label") or "").strip()
            if not label.startswith(_CLAIM_PREFIX):
                continue
            if wh.get("url") == self._webhook_url:
                continue  # our own prior registration from a previous run — skip
            info = _parse_claim(label)
            if info is None:
                continue
            if info.is_alive():
                raise ProjectLockError(
                    f"Symphony is already running for this project on another machine.\n"
                    f"  Host: {info.host}  PID: {info.pid}  "
                    f"Active since: {info.started_at}\n"
                    f"Stop that instance before starting a new one."
                )
            LOGGER.warning(
                "Stale cross-machine claim (host=%s pid=%s) — ignoring.",
                info.host,
                info.pid,
            )

    def set_webhook_id(self, webhook_id: str) -> None:
        self._webhook_id = webhook_id

    async def update(self, info: LockInfo) -> None:
        """Refresh the claim label on the registered webhook."""
        if self._webhook_id is None:
            return
        try:
            await self._registrar.update_label(self._webhook_id, encode_claim(info))
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("Webhook claim label update failed (non-fatal): %s", exc)


def encode_claim(info: LockInfo) -> str:
    payload = json.dumps(dataclasses.asdict(info), separators=(",", ":"))
    return f"{_CLAIM_PREFIX}{payload}"


def _parse_claim(label: str) -> LockInfo | None:
    try:
        data = json.loads(label[len(_CLAIM_PREFIX):])
        return LockInfo(**data)
    except Exception:  # noqa: BLE001
        return None


async def run_heartbeat(
    file_lock: FileLock,
    lock_info: LockInfo,
    webhook_claim: WebhookClaim | None = None,
) -> None:
    """Background task: refresh file lock and webhook claim on a fixed interval."""
    info = lock_info
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL_S)
        try:
            info = file_lock.update(info)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Project lock heartbeat write failed: %s", exc)
        if webhook_claim is not None:
            await webhook_claim.update(info)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()
