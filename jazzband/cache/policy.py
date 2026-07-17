from __future__ import annotations

from dataclasses import dataclass

# Anthropic prompt cache TTL as of 2025. No published SLA; verify before hardcoding.
CACHE_TTL_SECONDS = 300


@dataclass(frozen=True)
class CachePolicy:
    ttl_refresh: bool = True
    ttl_refresh_threshold_seconds: int = 60
    prefix_dedup: bool = True
    stale_evict: bool = True
    bloat_limit_tokens: int = 150_000
    bloat_action: str = "warn"  # "warn" | "evict"
