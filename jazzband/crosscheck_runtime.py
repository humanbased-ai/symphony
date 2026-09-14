"""Run the configured reviewer without competing with Jazzband's fixer."""
from __future__ import annotations

import asyncio
import logging
import os
import signal

LOGGER = logging.getLogger(__name__)


class ReviewDispatcher:
    def __init__(self, *, timeout: float = 900) -> None:
        self.timeout = timeout
        self.attempted: dict[str, str] = {}
        self.tasks: dict[str, asyncio.Task[None]] = {}

    def schedule(self, *, branch: str, sha: str, reviewer: str, url: str, token: str) -> None:
        if self.attempted.get(branch) == sha or (branch in self.tasks and not self.tasks[branch].done()):
            return
        self.attempted[branch] = sha
        self.tasks[branch] = asyncio.create_task(self._review(reviewer, url, token))

    async def _review(self, reviewer: str, url: str, token: str) -> None:
        vendor = {"claude_code": "claude", "codex": "codex"}[reviewer]
        env = os.environ.copy()
        env["GITHUB_TOKEN"] = token
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "crosscheck", "review", "--reviewer", vendor, url,
                env=env, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
            await asyncio.wait_for(proc.wait(), timeout=self.timeout)
            if proc.returncode:
                LOGGER.warning("Crosscheck review failed for %s (exit %s); no verdict assumed", url, proc.returncode)
        except (OSError, asyncio.TimeoutError):
            LOGGER.warning("Crosscheck review unavailable or timed out for %s; no verdict assumed", url)
        finally:
            if proc is not None and proc.returncode is None:
                # Crosscheck spawns agent children; terminate the whole review group.
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    os.killpg(proc.pid, signal.SIGKILL)
                    await proc.wait()
                except ProcessLookupError:
                    await proc.wait()

    async def forget(self, branch: str) -> None:
        task = self.tasks.pop(branch, None)
        self.attempted.pop(branch, None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def close(self) -> None:
        for branch in list(self.tasks):
            await self.forget(branch)
