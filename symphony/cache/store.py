from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)

DEFAULT_STORE_PATH = Path.home() / ".symphony" / "cache_state.json"


class CacheStateStore:
    """Persists cache session state to disk so Symphony has a cross-run view."""

    def __init__(self, path: Path = DEFAULT_STORE_PATH) -> None:
        self._path = path

    def load(self) -> dict[str, Any]:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text())
        except Exception as exc:
            LOGGER.warning("cache_state_load_failed: %s", exc)
            return {}

    def save(self, state: dict[str, Any]) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(state, indent=2))
        except Exception as exc:
            LOGGER.warning("cache_state_save_failed: %s", exc)
