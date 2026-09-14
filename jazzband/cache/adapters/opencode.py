from __future__ import annotations

from jazzband.cache.base import CacheAdapter, CacheSession


class OpenCodeAdapter(CacheAdapter):
    """Cache adapter for OpenCode. Stub — session store format TBD."""

    def list_active_sessions(self) -> list[CacheSession]:
        raise NotImplementedError("OpenCodeAdapter: session store format TBD")

    def get_session_age_seconds(self, session_id: str) -> float:
        raise NotImplementedError("OpenCodeAdapter: session store format TBD")

    def get_prefix_hash(self, session_id: str) -> str:
        raise NotImplementedError("OpenCodeAdapter: session store format TBD")

    def evict(self, session_id: str) -> None:
        raise NotImplementedError("OpenCodeAdapter: session store format TBD")

    def warm(self, session_id: str, prefix: str) -> None:
        raise NotImplementedError("OpenCodeAdapter: session store format TBD")
