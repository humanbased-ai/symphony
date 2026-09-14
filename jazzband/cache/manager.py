from __future__ import annotations

import logging

from jazzband.cache.base import CacheAdapter, CacheSession
from jazzband.cache.policy import CACHE_TTL_SECONDS, CachePolicy

LOGGER = logging.getLogger(__name__)


class CacheManager:
    """Agent-agnostic entry point for prompt cache management.

    Routes introspection and eviction calls to the appropriate CacheAdapter.
    Full policy enforcement (TTL refresh, stale evict, prefix dedup) is deferred
    until adapters expose stable interfaces — see jazzband/cache/adapters/.
    """

    def __init__(self, adapter: CacheAdapter, policy: CachePolicy | None = None) -> None:
        self._adapter = adapter
        self._policy = policy or CachePolicy()

    def list_sessions(self) -> list[CacheSession]:
        try:
            return self._adapter.list_active_sessions()
        except NotImplementedError:
            return []

    def ensure_warm(self, session_id: str, prefix: str) -> None:
        """Evict stale sessions and re-warm before dispatch.

        Implementation deferred — requires a stable adapter API that does not
        depend on private agent internals.
        """

    def record_session(self, session_id: str) -> None:
        """Record a session after dispatch for TTL and dedup tracking.

        Implementation deferred — see ensure_warm.
        """

    def status_rows(self) -> list[dict[str, object]]:
        rows = []
        for s in self.list_sessions():
            ttl_remaining = max(0.0, CACHE_TTL_SECONDS - s.age_seconds)
            rows.append(
                {
                    "session_id": s.session_id,
                    "agent": s.agent,
                    "prefix_hash": s.prefix_hash[:12],
                    "age_s": round(s.age_seconds, 1),
                    "ttl_remaining_s": round(ttl_remaining, 1),
                    "tokens": s.token_estimate or "?",
                }
            )
        return rows
