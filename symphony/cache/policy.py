from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CachePolicy:
    """Controls prompt cache eviction and refresh behaviour for Symphony dispatches.

    Attributes:
        ttl_refresh: Re-warm sessions approaching TTL expiry.
        ttl_refresh_threshold_seconds: Seconds before TTL expiry to trigger re-warm.
        prefix_dedup: Reuse a single cached block when tickets share the same static prefix.
        stale_evict: Evict sessions whose prefix hash no longer matches the current prompt.
        bloat_limit_tokens: Token count above which the bloat policy fires.
        bloat_action: Either "warn" (log only) or "evict" (remove session).
    """

    ttl_refresh: bool = False
    ttl_refresh_threshold_seconds: int = 60
    prefix_dedup: bool = True
    stale_evict: bool = True
    bloat_limit_tokens: int = 0
    bloat_action: str = "warn"
