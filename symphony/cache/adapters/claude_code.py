from __future__ import annotations

from symphony.cache.base import CacheAdapter, CacheSession


class ClaudeCodeAdapter(CacheAdapter):
    """Cache adapter for Claude Code.

    Full implementation is deferred until Claude Code exposes a stable
    cache-state API. The session store lives at ~/.claude/projects/*/
    as private JSONL files with no documented format or stability guarantee;
    parsing it would couple Symphony to Claude Code internals that change
    without notice.
    """

    def list_active_sessions(self) -> list[CacheSession]:
        raise NotImplementedError(
            "ClaudeCodeAdapter: awaiting a stable cache-state API from Claude Code"
        )

    def get_session_age_seconds(self, session_id: str) -> float:
        raise NotImplementedError(
            "ClaudeCodeAdapter: awaiting a stable cache-state API from Claude Code"
        )

    def get_prefix_hash(self, session_id: str) -> str:
        raise NotImplementedError(
            "ClaudeCodeAdapter: awaiting a stable cache-state API from Claude Code"
        )

    def evict(self, session_id: str) -> None:
        raise NotImplementedError(
            "ClaudeCodeAdapter: awaiting a stable cache-state API from Claude Code"
        )

    def warm(self, session_id: str, prefix: str) -> None:
        raise NotImplementedError(
            "ClaudeCodeAdapter: awaiting a stable cache-state API from Claude Code"
        )
