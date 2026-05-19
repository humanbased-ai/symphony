from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from symphony.config import HooksConfig, WorkspaceConfig
from symphony.tracker.models import Issue


LOGGER = logging.getLogger(__name__)
HOOK_OUTPUT_LIMIT = 4_000
WORKSPACE_MODE = stat.S_IRWXU
_UNSAFE_WORKSPACE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
_UNSAFE_BRANCH_CHARS = re.compile(r"[^A-Za-z0-9._/-]+")
BARE_REPO_DIRNAME = ".repo.git"

# Per-bare-repo asyncio locks so concurrent `prepare_for_run` calls that
# share a bare repo serialize their clone/fetch step. Keyed by the resolved
# bare-repo path; created lazily on first access. The lock is module-level
# (not per WorkspaceManager) so multiple manager instances pointing at the
# same root still cooperate.
_BARE_REPO_LOCKS: dict[str, "asyncio.Lock"] = {}


def _bare_repo_lock(bare_path: Path) -> "asyncio.Lock":
    key = str(bare_path)
    lock = _BARE_REPO_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _BARE_REPO_LOCKS[key] = lock
    return lock


class WorkspaceError(ValueError):
    """Raised when a workspace path or lifecycle operation is invalid."""


class WorkspaceHookError(RuntimeError):
    """Raised when a blocking workspace lifecycle hook fails."""


class GitCommandError(RuntimeError):
    """Raised when a git command invoked by the workspace manager fails."""


@dataclass(frozen=True)
class Workspace:
    path: Path
    workspace_key: str
    run_id: str
    branch_name: str | None
    run_log_path: Path | None
    created_now: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", Path(self.path).expanduser().resolve())


@dataclass(frozen=True)
class WorkspaceHookResult:
    name: str
    command: str
    exit_code: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class WorkspaceManager:
    """Per-run workspace lifecycle owner (SPEC §9, PRD §8.1).

    Two modes:

    * Git mode (`workspace.repo_url` configured): maintain a bare clone at
      `<root>/.repo.git` and materialize a fresh `git worktree` per dispatch at
      `<root>/<workspace_key>/<run_id>` on a unique branch. Cleanup forcibly
      removes the worktree and deletes the branch.
    * Plain mode (no `repo_url`): create an empty per-run directory at the same
      path; the agent populates it (typically via `gh repo clone`). Cleanup
      removes the per-run directory only.

    Per-run isolation deliberately narrows SPEC §9.1's per-issue path and §9.2's
    "workspaces are reused across runs" guidance — see PRD §8.1 for the
    isolation matrix and rationale.
    """

    workspace: WorkspaceConfig
    hooks: HooksConfig = HooksConfig()
    environ: Mapping[str, str] | None = None
    logs_root: Path | None = None
    run_id_factory: Callable[[], str] | None = None

    async def prepare_for_run(self, issue: Issue, *, run_id: str | None = None) -> Workspace:
        """Materialize an isolated workspace for one dispatch of ``issue``."""

        workspace_key = sanitize_workspace_key(issue.identifier)
        run_id_value = self._normalize_run_id(run_id)
        branch_name = self._derive_branch_name(issue, run_id_value)
        root = self._ensure_root()
        run_path = self._run_path(workspace_key, run_id_value)

        if not is_path_within_root(run_path, root):
            raise WorkspaceError("workspace_path_outside_root")
        if run_path.exists():
            raise WorkspaceError("workspace_run_path_already_exists")

        # Ensure the per-issue parent exists with owner-only permissions before
        # the per-run worktree is materialized so cleanup of one run cannot
        # affect a sibling run for the same issue.
        issue_dir = run_path.parent
        issue_dir.mkdir(mode=WORKSPACE_MODE, parents=True, exist_ok=True)
        _restrict_owner_permissions(issue_dir)

        if self.workspace.repo_url:
            await self._materialize_git_worktree(run_path, branch_name)
        else:
            run_path.mkdir(mode=WORKSPACE_MODE)
            _restrict_owner_permissions(run_path)

        run_log_path = self._compute_run_log_path(workspace_key, run_id_value)
        if run_log_path is not None:
            run_log_path.parent.mkdir(parents=True, exist_ok=True)

        handle = Workspace(
            path=run_path,
            workspace_key=workspace_key,
            run_id=run_id_value,
            branch_name=branch_name,
            run_log_path=run_log_path,
            created_now=True,
        )

        try:
            await self.run_hook("after_create", handle)
        except Exception:
            await self._best_effort_remove_run(handle)
            raise

        return handle

    async def prepare_for_issue(self, issue: Issue, *, run_id: str | None = None) -> Workspace:
        """Backwards-compatible alias for :meth:`prepare_for_run`."""

        return await self.prepare_for_run(issue, run_id=run_id)

    async def before_run(self, workspace: Workspace) -> WorkspaceHookResult | None:
        self.validate_workspace(workspace.path)
        return await self.run_hook("before_run", workspace)

    async def after_run(self, workspace: Workspace) -> WorkspaceHookResult | None:
        self.validate_workspace(workspace.path)
        return await self.run_hook("after_run", workspace, best_effort=True)

    async def cleanup_for_run(
        self,
        workspace: Workspace,
        *,
        failed: bool = False,
        keep_on_failure: bool = False,
    ) -> bool:
        if failed and keep_on_failure:
            return False

        root = self.workspace.root.expanduser().resolve()
        run_path = Path(workspace.path).expanduser().resolve()
        if not is_path_within_root(run_path, root):
            raise WorkspaceError("workspace_path_outside_root")
        if not run_path.exists():
            return False
        if not run_path.is_dir():
            raise WorkspaceError("workspace_path_exists_not_directory")

        await self.run_hook("before_remove", workspace, best_effort=True)
        await self._remove_run_path(run_path, workspace.branch_name)
        # If no sibling runs remain for this issue, drop the parent dir too.
        parent = run_path.parent
        if parent != root and parent.exists() and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
        return True

    async def cleanup_for_issue(
        self,
        issue: Issue,
        *,
        failed: bool = False,
        keep_on_failure: bool = False,
    ) -> bool:
        return await self.cleanup(issue.identifier, failed=failed, keep_on_failure=keep_on_failure)

    async def cleanup(
        self,
        issue_identifier: str,
        *,
        failed: bool = False,
        keep_on_failure: bool = False,
    ) -> bool:
        """Remove all per-run directories for ``issue_identifier``.

        Used for SPEC §9 terminal cleanup, where every run for a now-terminal
        issue should be reclaimed regardless of which dispatch produced it.
        Each per-run directory has its ``before_remove`` hook invoked while
        the directory still contains its run contents, and the corresponding
        branch in the bare repo (if any) is force-deleted after the worktree
        is removed.
        """

        if failed and keep_on_failure:
            return False

        workspace_key = sanitize_workspace_key(issue_identifier)
        issue_dir = self._issue_path(workspace_key)
        root = self.workspace.root.expanduser().resolve()
        if not is_path_within_root(issue_dir, root):
            raise WorkspaceError("workspace_path_outside_root")
        if not issue_dir.exists():
            return False
        if not issue_dir.is_dir():
            raise WorkspaceError("workspace_path_exists_not_directory")

        branch_map = await self._list_worktree_branches()
        removed_any = False
        for entry in sorted(issue_dir.iterdir()):
            if not entry.is_dir():
                continue
            branch_name = branch_map.get(str(entry.resolve()))
            handle = Workspace(
                path=entry,
                workspace_key=workspace_key,
                run_id=entry.name,
                branch_name=branch_name,
                run_log_path=None,
                created_now=False,
            )
            await self.run_hook("before_remove", handle, best_effort=True)
            await self._remove_run_path(entry, branch_name)
            removed_any = True

        if issue_dir.exists() and issue_dir.is_dir():
            shutil.rmtree(issue_dir, ignore_errors=True)
        return removed_any or not issue_dir.exists()

    async def sweep_stale_worktrees(self) -> int:
        """Force-clean orphan per-run worktrees at startup.

        Symphony does not persist its in-memory running set across restarts, so
        every per-run directory that exists at startup is stale by definition
        and must be removed. In git mode this also prunes worktree metadata in
        the bare repo via ``git worktree remove --force``; in plain mode it
        simply ``rmtree``s the per-run directory.

        Returns the number of per-run directories removed.
        """

        root = self.workspace.root.expanduser().resolve()
        if not root.exists():
            return 0

        bare = self._bare_repo_path()
        # Capture branch map before any cleanup. Some worktree metadata may
        # point at directories that were already deleted out-of-band by a
        # crashed dispatch — those branches won't be reachable via the disk
        # walk below and must be force-deleted separately after prune.
        pre_branch_map = await self._list_worktree_branches() if bare.exists() else {}
        orphan_branches = {
            branch
            for path, branch in pre_branch_map.items()
            if branch and not Path(path).exists()
        }

        # Prune first so branches whose worktree directory has been deleted
        # out-of-band become eligible for ``git branch -D`` (otherwise git
        # rejects the delete because the branch is "used by worktree at …").
        if bare.exists():
            try:
                await _run_git(bare, ["worktree", "prune"])
            except GitCommandError as exc:
                LOGGER.warning("git worktree prune failed: %s", exc)

        removed = 0
        for issue_dir in sorted(root.iterdir()):
            if not issue_dir.is_dir() or issue_dir == bare:
                continue
            for run_dir in sorted(issue_dir.iterdir()):
                if not run_dir.is_dir():
                    continue
                branch_name = pre_branch_map.get(str(run_dir.resolve()))
                await self._remove_run_path(run_dir, branch_name)
                removed += 1
            if issue_dir.exists() and not any(issue_dir.iterdir()):
                issue_dir.rmdir()

        # After prune + disk walk, force-delete any orphan branches that were
        # never represented on disk during this sweep — their worktree dirs
        # had vanished before we started, so the disk walk could not touch
        # them.
        for branch_name in sorted(orphan_branches):
            try:
                await _run_git(bare, ["branch", "-D", branch_name])
            except GitCommandError as exc:
                LOGGER.warning(
                    "git branch -D %s failed during sweep: %s", branch_name, exc
                )

        return removed

    async def run_hook(
        self,
        hook_name: str,
        workspace: Workspace,
        *,
        best_effort: bool = False,
    ) -> WorkspaceHookResult | None:
        command = getattr(self.hooks, hook_name)
        if command is None:
            return None

        try:
            return await _run_shell_hook(
                hook_name,
                command,
                workspace.path,
                timeout_ms=self.hooks.timeout_ms,
                environ=self.environ,
            )
        except WorkspaceHookError:
            if not best_effort:
                raise
            LOGGER.warning("Workspace hook %s failed; continuing", hook_name, exc_info=True)
            return None

    def workspace_path(self, workspace_key: str) -> Path:
        """Return the per-issue parent directory (back-compat helper)."""

        path = self._issue_path(workspace_key)
        if not is_path_within_root(path, self.workspace.root.expanduser().resolve()):
            raise WorkspaceError("workspace_path_outside_root")
        return path

    def validate_workspace(self, path: str | Path) -> Path:
        root = self.workspace.root.expanduser().resolve()
        workspace_path = Path(path).expanduser().resolve()
        if not is_path_within_root(workspace_path, root):
            raise WorkspaceError("workspace_path_outside_root")
        if not workspace_path.is_dir():
            raise WorkspaceError("workspace_path_missing")
        return workspace_path

    def _ensure_root(self) -> Path:
        root = self.workspace.root.expanduser().resolve()
        if root.exists() and not root.is_dir():
            raise WorkspaceError("workspace_root_exists_not_directory")
        root.mkdir(mode=WORKSPACE_MODE, parents=True, exist_ok=True)
        _restrict_owner_permissions(root)
        return root

    def _issue_path(self, workspace_key: str) -> Path:
        return (self.workspace.root.expanduser().resolve() / workspace_key).resolve()

    def _run_path(self, workspace_key: str, run_id: str) -> Path:
        return (self._issue_path(workspace_key) / run_id).resolve()

    def _bare_repo_path(self) -> Path:
        return (self.workspace.root.expanduser().resolve() / BARE_REPO_DIRNAME).resolve()

    def _compute_run_log_path(self, workspace_key: str, run_id: str) -> Path | None:
        if self.logs_root is None:
            return None
        return Path(self.logs_root).expanduser().resolve() / workspace_key / f"{run_id}.log"

    def _normalize_run_id(self, run_id: str | None) -> str:
        if run_id is None:
            generator = self.run_id_factory or _default_run_id
            run_id = generator()
        candidate = _UNSAFE_WORKSPACE_CHARS.sub("_", run_id.strip()).strip("_")
        if not candidate or candidate in {".", ".."}:
            raise WorkspaceError("workspace_run_id_required")
        return candidate

    def _derive_branch_name(self, issue: Issue, run_id: str) -> str | None:
        if not self.workspace.repo_url:
            return None
        base = issue.branch_name or f"issue/{issue.identifier.lower()}"
        sanitized = _UNSAFE_BRANCH_CHARS.sub("-", base.strip()).strip("-/")
        if not sanitized:
            raise WorkspaceError("workspace_branch_name_invalid")
        prefix = self.workspace.branch_prefix or ""
        return f"{prefix}{sanitized}-{run_id}"

    async def _list_worktree_branches(self) -> dict[str, str]:
        """Return ``{worktree_path: branch_name}`` from ``git worktree list --porcelain``.

        Empty when there is no bare repo (plain mode) or when the listing
        fails. Plain-mode callers ignore the result and rmtree per-run dirs as
        before; git-mode callers use it to look up the branch that was created
        by ``git worktree add -b`` so cleanup can ``git branch -D`` it after
        the worktree is removed.
        """

        bare = self._bare_repo_path()
        if not bare.exists():
            return {}
        try:
            stdout, _ = await _run_git(bare, ["worktree", "list", "--porcelain"])
        except GitCommandError as exc:
            LOGGER.warning("git worktree list failed: %s", exc)
            return {}

        result: dict[str, str] = {}
        current_path: str | None = None
        for line in stdout.splitlines():
            if line.startswith("worktree "):
                current_path = line[len("worktree ") :].strip()
            elif line.startswith("branch ") and current_path is not None:
                ref = line[len("branch ") :].strip()
                if ref.startswith("refs/heads/"):
                    result[str(Path(current_path).resolve())] = ref[len("refs/heads/") :]
                else:
                    result[str(Path(current_path).resolve())] = ref
            elif not line.strip():
                current_path = None
        return result

    async def _materialize_git_worktree(self, run_path: Path, branch_name: str | None) -> None:
        if not self.workspace.repo_url:
            raise WorkspaceError("workspace_repo_url_required")
        if branch_name is None:
            raise WorkspaceError("workspace_branch_name_invalid")

        bare = self._bare_repo_path()
        # Serialize clone/fetch on the same bare repo so concurrent
        # dispatches into an empty workspace root don't race
        # ``git clone --bare`` against the same destination (one clone
        # succeeds, the others fail against a half-created directory).
        # Worktree add can run concurrently — each dispatch adds a distinct
        # `<root>/<key>/<run_id>` path so there is no collision.
        async with _bare_repo_lock(bare):
            if not bare.exists():
                await _run_git(
                    bare.parent, ["clone", "--bare", self.workspace.repo_url, bare.name]
                )
            else:
                try:
                    await _run_git(bare, ["fetch", "--prune", "origin"])
                except GitCommandError as exc:
                    LOGGER.warning(
                        "git fetch failed (continuing with cached objects): %s", exc
                    )

        base_ref = self.workspace.default_branch or "HEAD"
        try:
            await _run_git(
                bare,
                ["worktree", "add", str(run_path), "-b", branch_name, base_ref],
            )
        except GitCommandError as exc:
            # Worktree add can leave a half-created directory; clean it up so a
            # subsequent retry does not collide with stale state.
            shutil.rmtree(run_path, ignore_errors=True)
            raise WorkspaceError(f"workspace_git_worktree_add_failed:{exc}") from exc

    async def _remove_run_path(self, run_path: Path, branch_name: str | None) -> None:
        bare = self._bare_repo_path()
        if bare.exists():
            try:
                await _run_git(bare, ["worktree", "remove", "--force", str(run_path)])
            except GitCommandError as exc:
                LOGGER.warning(
                    "git worktree remove failed for %s (%s); falling back to rmtree",
                    run_path,
                    exc,
                )
                shutil.rmtree(run_path, ignore_errors=True)
            if branch_name:
                try:
                    await _run_git(bare, ["branch", "-D", branch_name])
                except GitCommandError as exc:
                    LOGGER.warning(
                        "git branch -D %s failed (%s); branch may be left in bare repo",
                        branch_name,
                        exc,
                    )
        else:
            shutil.rmtree(run_path, ignore_errors=True)

    async def _best_effort_remove_run(self, workspace: Workspace) -> None:
        try:
            await self._remove_run_path(workspace.path, workspace.branch_name)
        except Exception:
            LOGGER.warning(
                "Failed to remove partially-created run path %s",
                workspace.path,
                exc_info=True,
            )


def sanitize_workspace_key(issue_identifier: str) -> str:
    candidate = _UNSAFE_WORKSPACE_CHARS.sub("_", issue_identifier.strip()).strip("_")
    if candidate in {"", ".", ".."}:
        raise WorkspaceError("workspace_identifier_required")
    return candidate


def is_path_within_root(path: str | Path, root: str | Path) -> bool:
    workspace_path = Path(path).expanduser().resolve()
    workspace_root = Path(root).expanduser().resolve()
    return workspace_path == workspace_root or workspace_root in workspace_path.parents


async def _run_shell_hook(
    hook_name: str,
    command: str,
    cwd: Path,
    *,
    timeout_ms: int,
    environ: Mapping[str, str] | None,
) -> WorkspaceHookResult:
    env = os.environ.copy()
    if environ is not None:
        env.update(environ)

    process = await asyncio.create_subprocess_exec(
        "bash",
        "-lc",
        command,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=timeout_ms / 1000)
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.wait()
        raise WorkspaceHookError(f"workspace_hook_timeout:{hook_name}") from exc

    stdout = _decode_and_truncate(stdout_bytes)
    stderr = _decode_and_truncate(stderr_bytes)
    exit_code = process.returncode if process.returncode is not None else -1
    result = WorkspaceHookResult(
        name=hook_name,
        command=command,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
    )
    if exit_code != 0:
        raise WorkspaceHookError(f"workspace_hook_failed:{hook_name}:{exit_code}")
    return result


async def _run_git(cwd: Path, args: list[str]) -> tuple[str, str]:
    cwd_path = Path(cwd).expanduser()
    cwd_path.parent.mkdir(parents=True, exist_ok=True)
    process = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd_path) if cwd_path.exists() else str(cwd_path.parent),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await process.communicate()
    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace")
    if process.returncode not in (0, None):
        raise GitCommandError(
            f"git {' '.join(args)} failed (exit={process.returncode}): {stderr.strip() or stdout.strip()}"
        )
    return stdout, stderr


def _decode_and_truncate(value: bytes) -> str:
    decoded = value.decode("utf-8", errors="replace")
    if len(decoded) <= HOOK_OUTPUT_LIMIT:
        return decoded
    return decoded[:HOOK_OUTPUT_LIMIT] + "\n[truncated]"


def _restrict_owner_permissions(path: Path) -> None:
    path.chmod(WORKSPACE_MODE)


def _default_run_id() -> str:
    return secrets.token_hex(4)
