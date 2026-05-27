from __future__ import annotations

from symphony.cache.base import CacheAdapter, CacheSession


class CodexAdapter(CacheAdapter):
    """Cache adapter for Codex. Stub — session store format TBD."""

    def list_active_sessions(self) -> list[CacheSession]:
        raise NotImplementedError("CodexAdapter: session store format TBD")

    def get_session_age_seconds(self, session_id: str) -> float:
        raise NotImplementedError("CodexAdapter: session store format TBD")

    def get_prefix_hash(self, session_id: str) -> str:
        raise NotImplementedError("CodexAdapter: session store format TBD")

    def evict(self, session_id: str) -> None:
        raise NotImplementedError("CodexAdapter: session store format TBD")

    def warm(self, session_id: str, prefix: str) -> None:
        raise NotImplementedError("CodexAdapter: session store format TBD")
