from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

JAZZBAND_HOME = Path.home() / ".jazzband"


def _runs_root(project_slug: str) -> Path:
    return JAZZBAND_HOME / "runs" / project_slug


def _run_id_file(project_slug: str) -> Path:
    return _runs_root(project_slug) / "current_run_id"


def get_or_create_run_id(project_slug: str, *, new: bool = False) -> str:
    """Return the current run ID for this project, creating one if needed.

    Pass new=True to force a fresh run ID (e.g. --new-run CLI flag).
    """
    id_file = _run_id_file(project_slug)
    id_file.parent.mkdir(parents=True, exist_ok=True)
    if not new and id_file.exists():
        return id_file.read_text().strip()
    run_id = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    id_file.write_text(run_id)
    return run_id


def list_runs(project_slug: str) -> list[Path]:
    """Return run directories for this project, newest first."""
    root = _runs_root(project_slug)
    if not root.exists():
        return []
    return sorted(
        [p for p in root.iterdir() if p.is_dir() and (p / "manifest.jsonl").exists()],
        reverse=True,
    )


class ManifestWriter:
    """Appends one JSON line per dispatch to manifest.jsonl inside the run directory."""

    def __init__(self, project_slug: str, run_id: str) -> None:
        self._dir = _runs_root(project_slug) / run_id
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / "manifest.jsonl"

    @property
    def path(self) -> Path:
        return self._path

    @property
    def dir(self) -> Path:
        return self._dir

    def record(
        self,
        *,
        ticket_id: str,
        session_id: str | None = None,
        started_at: float,
        ended_at: float,
        status: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_creation_tokens: int = 0,
        cache_read_tokens: int = 0,
        model: str = "",
        pr_url: str = "",
    ) -> None:
        entry = {
            "ticket_id": ticket_id,
            "session_id": session_id,
            "started_at": started_at,
            "ended_at": ended_at,
            "status": status,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_creation_tokens": cache_creation_tokens,
            "cache_read_tokens": cache_read_tokens,
            "model": model,
            "pr_url": pr_url,
        }
        with self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
