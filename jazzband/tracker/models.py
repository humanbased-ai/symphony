from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class Blocker:
    id: str | None
    identifier: str | None
    state: str | None


@dataclass(frozen=True)
class Issue:
    id: str
    identifier: str
    title: str
    description: str | None
    priority: int | None
    state: str
    branch_name: str | None
    url: str | None
    labels: tuple[str, ...] = field(default_factory=tuple)
    blocked_by: tuple[Blocker, ...] = field(default_factory=tuple)
    comments: tuple[str, ...] = field(default_factory=tuple)
    pr_number: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
