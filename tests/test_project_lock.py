"""Tests for symphony.project_lock: FileLock, WebhookClaim, and helpers."""
from __future__ import annotations

import asyncio
import json
import os
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from symphony.project_lock import (
    HEARTBEAT_INTERVAL_S,
    LOCK_FILE_NAME,
    STALE_THRESHOLD_S,
    FileLock,
    LockInfo,
    ProjectLockError,
    WebhookClaim,
    _parse_claim,
    encode_claim,
    run_heartbeat,
)


# ---------------------------------------------------------------------------
# LockInfo helpers
# ---------------------------------------------------------------------------

class TestLockInfo(unittest.TestCase):
    def _fresh(self) -> LockInfo:
        return LockInfo.for_current_process()

    def test_for_current_process_uses_own_pid_and_host(self) -> None:
        info = self._fresh()
        self.assertEqual(info.pid, os.getpid())
        self.assertEqual(info.host, socket.gethostname())
        self.assertEqual(info.started_at, info.heartbeat)

    def test_fresh_is_not_stale(self) -> None:
        self.assertFalse(self._fresh().is_stale())

    def test_old_heartbeat_is_stale(self) -> None:
        info = LockInfo(
            pid=os.getpid(),
            host=socket.gethostname(),
            started_at="2000-01-01T00:00:00+00:00",
            heartbeat="2000-01-01T00:00:00+00:00",
        )
        self.assertTrue(info.is_stale())

    def test_fresh_same_process_is_alive(self) -> None:
        self.assertTrue(self._fresh().is_alive())

    def test_stale_dead_pid_is_not_alive(self) -> None:
        # Use a PID that cannot exist (max + 1 is invalid on Linux/macOS).
        info = LockInfo(
            pid=999_999_999,
            host=socket.gethostname(),
            started_at="2000-01-01T00:00:00+00:00",
            heartbeat="2000-01-01T00:00:00+00:00",
        )
        self.assertFalse(info.is_alive())

    def test_stale_different_host_is_not_alive(self) -> None:
        info = LockInfo(
            pid=os.getpid(),
            host="other-machine.example.com",
            started_at="2000-01-01T00:00:00+00:00",
            heartbeat="2000-01-01T00:00:00+00:00",
        )
        self.assertFalse(info.is_alive())

    def test_fresh_different_host_is_alive(self) -> None:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        info = LockInfo(
            pid=999,
            host="other-machine.example.com",
            started_at=now,
            heartbeat=now,
        )
        self.assertTrue(info.is_alive())


# ---------------------------------------------------------------------------
# FileLock
# ---------------------------------------------------------------------------

class TestFileLock(unittest.TestCase):
    def _lock(self, root: Path) -> FileLock:
        return FileLock(root)

    def test_acquire_creates_lock_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock = self._lock(root)
            info = lock.acquire()
            self.assertTrue((root / LOCK_FILE_NAME).exists())
            self.assertEqual(info.pid, os.getpid())

    def test_acquire_twice_from_same_process_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock = self._lock(Path(tmp))
            lock.acquire()
            # Second acquire from same PID: is_alive() = True → conflict
            with self.assertRaises(ProjectLockError):
                lock.acquire()

    def test_acquire_replaces_stale_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stale = LockInfo(
                pid=999_999_999,
                host=socket.gethostname(),
                started_at="2000-01-01T00:00:00+00:00",
                heartbeat="2000-01-01T00:00:00+00:00",
            )
            lock_path = root / LOCK_FILE_NAME
            lock_path.write_text(json.dumps(stale.__dict__), encoding="utf-8")

            lock = self._lock(root)
            info = lock.acquire()  # should not raise
            self.assertEqual(info.pid, os.getpid())

    def test_acquire_raises_on_active_lock_from_another_pid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc).isoformat()
            active = LockInfo(
                pid=os.getpid(),  # same PID → process is alive
                host=socket.gethostname(),
                started_at=now,
                heartbeat=now,
            )
            lock_path = root / LOCK_FILE_NAME
            lock_path.write_text(json.dumps(active.__dict__), encoding="utf-8")

            with self.assertRaises(ProjectLockError):
                self._lock(root).acquire()

    def test_release_removes_lock_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock = self._lock(root)
            lock.acquire()
            lock.release()
            self.assertFalse((root / LOCK_FILE_NAME).exists())

    def test_release_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock = self._lock(Path(tmp))
            lock.release()  # no file yet — should not raise
            lock.release()  # already gone — should not raise

    def test_update_refreshes_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock = self._lock(Path(tmp))
            info = lock.acquire()
            original_heartbeat = info.heartbeat
            time.sleep(0.01)
            refreshed = lock.update(info)
            self.assertGreaterEqual(refreshed.heartbeat, original_heartbeat)
            self.assertEqual(refreshed.pid, info.pid)

    def test_acquire_creates_workspace_root_if_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "new" / "nested" / "dir"
            self.assertFalse(root.exists())
            lock = self._lock(root)
            lock.acquire()
            self.assertTrue(root.exists())
            self.assertTrue((root / LOCK_FILE_NAME).exists())

    def test_corrupted_lock_file_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / LOCK_FILE_NAME).write_text("not json", encoding="utf-8")
            lock = self._lock(root)
            info = lock.acquire()  # should not raise
            self.assertEqual(info.pid, os.getpid())


# ---------------------------------------------------------------------------
# encode_claim / _parse_claim
# ---------------------------------------------------------------------------

class TestClaimEncoding(unittest.TestCase):
    def test_round_trip(self) -> None:
        info = LockInfo.for_current_process()
        label = encode_claim(info)
        self.assertTrue(label.startswith("symphony-claim:"))
        parsed = _parse_claim(label)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.pid, info.pid)
        self.assertEqual(parsed.host, info.host)

    def test_parse_invalid_returns_none(self) -> None:
        self.assertIsNone(_parse_claim("symphony-claim:not-json"))
        self.assertIsNone(_parse_claim("unrelated-label"))
        self.assertIsNone(_parse_claim(""))


# ---------------------------------------------------------------------------
# WebhookClaim
# ---------------------------------------------------------------------------

class TestWebhookClaim(unittest.TestCase):
    MY_URL = "https://example.com/api/v1/webhooks/linear"
    TEAM_ID = "team-123"

    def _run(self, coro: object) -> object:
        return asyncio.run(coro)  # type: ignore[arg-type]

    def _make_registrar(self, webhooks: list[dict]) -> MagicMock:
        registrar = MagicMock()
        registrar.list_webhooks = AsyncMock(return_value=webhooks)
        registrar.update_label = AsyncMock()
        return registrar

    def test_no_existing_webhooks_passes(self) -> None:
        registrar = self._make_registrar([])
        claim = WebhookClaim(registrar, self.TEAM_ID, self.MY_URL)
        self._run(claim.check())  # should not raise

    def test_own_webhook_url_is_ignored(self) -> None:
        info = LockInfo.for_current_process()
        label = encode_claim(info)
        registrar = self._make_registrar([{"url": self.MY_URL, "label": label}])
        claim = WebhookClaim(registrar, self.TEAM_ID, self.MY_URL)
        self._run(claim.check())  # own URL → ignored, no raise

    def test_active_claim_from_different_host_raises(self) -> None:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        foreign = LockInfo(
            pid=999, host="other-machine", started_at=now, heartbeat=now
        )
        label = encode_claim(foreign)
        registrar = self._make_registrar([
            {"url": "https://other.example.com/webhook", "label": label}
        ])
        claim = WebhookClaim(registrar, self.TEAM_ID, self.MY_URL)
        with self.assertRaises(ProjectLockError):
            self._run(claim.check())

    def test_stale_claim_from_different_host_is_ignored(self) -> None:
        stale = LockInfo(
            pid=999,
            host="other-machine",
            started_at="2000-01-01T00:00:00+00:00",
            heartbeat="2000-01-01T00:00:00+00:00",
        )
        label = encode_claim(stale)
        registrar = self._make_registrar([
            {"url": "https://other.example.com/webhook", "label": label}
        ])
        claim = WebhookClaim(registrar, self.TEAM_ID, self.MY_URL)
        self._run(claim.check())  # stale → ignored, no raise

    def test_webhook_without_symphony_label_is_ignored(self) -> None:
        registrar = self._make_registrar([
            {"url": "https://other.example.com/webhook", "label": "some-other-tool"}
        ])
        claim = WebhookClaim(registrar, self.TEAM_ID, self.MY_URL)
        self._run(claim.check())  # unrelated label → no raise

    def test_list_webhooks_failure_fails_open(self) -> None:
        registrar = MagicMock()
        registrar.list_webhooks = AsyncMock(side_effect=RuntimeError("network error"))
        claim = WebhookClaim(registrar, self.TEAM_ID, self.MY_URL)
        self._run(claim.check())  # should NOT raise — fail-open on network errors

    def test_update_with_no_webhook_id_is_noop(self) -> None:
        registrar = self._make_registrar([])
        claim = WebhookClaim(registrar, self.TEAM_ID, self.MY_URL)
        info = LockInfo.for_current_process()
        self._run(claim.update(info))  # no webhook_id set → noop, no raise
        registrar.update_label.assert_not_called()

    def test_update_calls_registrar_with_encoded_label(self) -> None:
        registrar = self._make_registrar([])
        claim = WebhookClaim(registrar, self.TEAM_ID, self.MY_URL)
        claim.set_webhook_id("wh-abc")
        info = LockInfo.for_current_process()
        self._run(claim.update(info))
        registrar.update_label.assert_called_once()
        call_label = registrar.update_label.call_args[0][1]
        self.assertTrue(call_label.startswith("symphony-claim:"))

    def test_update_failure_does_not_raise(self) -> None:
        registrar = MagicMock()
        registrar.update_label = AsyncMock(side_effect=RuntimeError("api error"))
        claim = WebhookClaim(registrar, self.TEAM_ID, self.MY_URL)
        claim.set_webhook_id("wh-xyz")
        info = LockInfo.for_current_process()
        self._run(claim.update(info))  # should swallow the error


# ---------------------------------------------------------------------------
# run_heartbeat
# ---------------------------------------------------------------------------

class TestRunHeartbeat(unittest.TestCase):
    def test_heartbeat_updates_file_lock_and_webhook_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            file_lock = FileLock(root)
            info = file_lock.acquire()

            update_calls: list[str] = []

            async def _fake_update(new_info: LockInfo) -> None:
                update_calls.append(new_info.heartbeat)

            webhook_claim = MagicMock()
            webhook_claim.update = _fake_update

            async def _run() -> None:
                task = asyncio.create_task(run_heartbeat(file_lock, info, webhook_claim))
                # Wait just past one interval tick using a very short fake interval.
                with patch("symphony.project_lock.HEARTBEAT_INTERVAL_S", 0.05):
                    await asyncio.sleep(0.15)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

            asyncio.run(_run())
            self.assertGreater(len(update_calls), 0)


if __name__ == "__main__":
    unittest.main()
