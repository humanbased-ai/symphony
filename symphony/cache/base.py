from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CacheSession:
    session_id: str
    agent: str
    prefix_hash: str
    age_seconds: float

    def __repr__(self) -> str:
        return (
            f"CacheSession(session_id={self.session_id!r}, agent={self.agent!r}, "
            f"prefix_hash={self.prefix_hash[:12]!r}, age_seconds={self.age_seconds})"
        )
