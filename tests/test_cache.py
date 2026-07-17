import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from jazzband.cache.base import CacheAdapter, CacheSession
from jazzband.cache.manager import CacheManager
from jazzband.cache.policy import CACHE_TTL_SECONDS, CachePolicy
from jazzband.cache.store import CacheStateStore
from jazzband.cache.adapters.claude_code import ClaudeCodeAdapter
from jazzband.cache.adapters.codex import CodexAdapter
from jazzband.cache.adapters.opencode import OpenCodeAdapter


def _session(session_id: str, age_seconds: float = 10.0) -> CacheSession:
    return CacheSession(
        session_id=session_id,
        agent="test",
        prefix_hash="abc123def456",
        age_seconds=age_seconds,
        token_estimate=1000,
    )


class CacheAdapterStubTests(unittest.TestCase):
    """All stub adapters must raise NotImplementedError on every method."""

    def _assert_all_raise(self, adapter: CacheAdapter) -> None:
        with self.assertRaises(NotImplementedError):
            adapter.list_active_sessions()
        with self.assertRaises(NotImplementedError):
            adapter.get_session_age_seconds("x")
        with self.assertRaises(NotImplementedError):
            adapter.get_prefix_hash("x")
        with self.assertRaises(NotImplementedError):
            adapter.evict("x")
        with self.assertRaises(NotImplementedError):
            adapter.warm("x", "prefix")

    def test_claude_code_adapter_is_stub(self):
        self._assert_all_raise(ClaudeCodeAdapter())

    def test_codex_adapter_is_stub(self):
        self._assert_all_raise(CodexAdapter())

    def test_opencode_adapter_is_stub(self):
        self._assert_all_raise(OpenCodeAdapter())


class CacheManagerTests(unittest.TestCase):
    def test_list_sessions_delegates_to_adapter(self):
        adapter = MagicMock(spec=CacheAdapter)
        adapter.list_active_sessions.return_value = [_session("s1")]
        manager = CacheManager(adapter)
        sessions = manager.list_sessions()
        self.assertEqual(1, len(sessions))
        self.assertEqual("s1", sessions[0].session_id)

    def test_list_sessions_returns_empty_for_stub_adapter(self):
        manager = CacheManager(ClaudeCodeAdapter())
        self.assertEqual([], manager.list_sessions())

    def test_status_rows_compute_ttl_remaining(self):
        adapter = MagicMock(spec=CacheAdapter)
        age = 60.0
        adapter.list_active_sessions.return_value = [_session("s1", age_seconds=age)]
        manager = CacheManager(adapter)
        rows = manager.status_rows()
        self.assertEqual(1, len(rows))
        self.assertAlmostEqual(CACHE_TTL_SECONDS - age, rows[0]["ttl_remaining_s"], places=1)

    def test_status_rows_ttl_clamps_at_zero(self):
        adapter = MagicMock(spec=CacheAdapter)
        adapter.list_active_sessions.return_value = [_session("s1", age_seconds=999.0)]
        manager = CacheManager(adapter)
        rows = manager.status_rows()
        self.assertEqual(0, rows[0]["ttl_remaining_s"])

    def test_ensure_warm_and_record_session_are_no_ops(self):
        adapter = MagicMock(spec=CacheAdapter)
        manager = CacheManager(adapter)
        manager.ensure_warm("s1", "prefix")
        manager.record_session("s1")
        adapter.warm.assert_not_called()


class CachePolicyTests(unittest.TestCase):
    def test_default_policy_has_expected_values(self):
        policy = CachePolicy()
        self.assertTrue(policy.ttl_refresh)
        self.assertTrue(policy.prefix_dedup)
        self.assertTrue(policy.stale_evict)
        self.assertEqual(60, policy.ttl_refresh_threshold_seconds)
        self.assertEqual(150_000, policy.bloat_limit_tokens)
        self.assertEqual("warn", policy.bloat_action)


class CacheStateStoreTests(unittest.TestCase):
    def test_load_returns_empty_dict_when_file_missing(self, tmp_path=None):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            store = CacheStateStore(Path(tmp) / "nonexistent.json")
            self.assertEqual({}, store.load())

    def test_save_and_load_round_trips(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache_state.json"
            store = CacheStateStore(path)
            state = {"sessions": [{"session_id": "s1", "agent": "claude_code"}]}
            store.save(state)
            loaded = store.load()
            self.assertEqual(state, loaded)

    def test_load_returns_empty_on_corrupt_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text("not valid json{{{")
            store = CacheStateStore(path)
            self.assertEqual({}, store.load())


if __name__ == "__main__":
    unittest.main()
