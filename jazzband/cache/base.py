from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class CacheSession:
    session_id: str
    agent: str
    prefix_hash: str
    age_seconds: float
    token_estimate: int | None = None


class CacheAdapter(ABC):
    """Agent-agnostic interface for prompt cache introspection and management."""

    @abstractmethod
    def list_active_sessions(self) -> list[CacheSession]: ...

    @abstractmethod
    def get_session_age_seconds(self, session_id: str) -> float: ...

    @abstractmethod
    def get_prefix_hash(self, session_id: str) -> str: ...

    @abstractmethod
    def evict(self, session_id: str) -> None: ...

    @abstractmethod
    def warm(self, session_id: str, prefix: str) -> None: ...
