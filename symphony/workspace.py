from __future__ import annotations

import asyncio
import json
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
METADATA_MODE = stat.S_IRUSR | stat.S_IWUSR
_UNSAFE_WORKSPACE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
_UNSAFE_BRANCH_CHARS = re.compile(r"[^A-Za-z0-9._/-]+")
BARE_REPO_DIRNAME = ".repo.git"
RUN_METADATA_SUFFIX = ".symphony-workspace.json"

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
      removes the worktree and deletes the branch Symphony recorded at create
      time, even if the worktree later checks out a different branch.
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
        self._write_run_metadata(handle)

        try:
            await self.run_hook("after_create", handle)
        except Exception:
            await self._best_effort_remove_run(handle)
            raise

        return handle

    async def prepare_for_issue(self, issue: Issue, *, run_id: str | None = None) -> Workspace:
        """Backwards-compatible alias for :meth:`prepare_for_run`."""

        return await self.prepare_for_run(issue, run_id=run_id)

    async def prepare_for_pr_feedback(
        self,
        issue: Issue,
        existing_branch: str,
        *,
        run_id: str | None = None,
    ) -> Workspace:
        """Materialize a workspace on an already-pushed branch for PR feedback runs.

        Unlike prepare_for_run, this checks out *existing_branch* from the remote
        rather than creating a new branch, so the agent can push changes back to
        the same PR branch.
        """
        workspace_key = sanitize_workspace_key(issue.identifier)
        run_id_value = self._normalize_run_id(run_id)
        root = self._ensure_root()
        run_path = self._run_path(workspace_key, run_id_value)

        if not is_path_within_root(run_path, root):
            raise WorkspaceError("workspace_path_outside_root")
        if run_path.exists():
            raise WorkspaceError("workspace_run_path_already_exists")

        issue_dir = run_path.parent
        issue_dir.mkdir(mode=WORKSPACE_MODE, parents=True, exist_ok=True)
        _restrict_owner_permissions(issue_dir)

        if self.workspace.repo_url:
            await self._materialize_existing_branch_worktree(run_path, existing_branch, run_id_value)
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
            branch_name=existing_branch,
            run_log_path=run_log_path,
            created_now=True,
        )
        self._write_run_metadata(handle)

        try:
            await self.run_hook("after_create", handle)
        except Exception:
            await self._best_effort_remove_run(handle)
            raise

        return handle

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
        self._remove_run_metadata(workspace.workspace_key, workspace.run_id)
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

        removed_any = False
        for entry in sorted(issue_dir.iterdir()):
            if not entry.is_dir():
                continue
            branch_name = self._read_run_branch_name(workspace_key, entry.name)
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
            self._remove_run_metadata(workspace_key, entry.name)
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
        metadata_by_path = self._list_run_metadata()
        orphan_metadata_paths: set[Path] = set()
        orphan_branches: set[str] = set()
        for run_path, (branch_name, metadata_path) in metadata_by_path.items():
            if not Path(run_path).exists():
                orphan_metadata_paths.add(metadata_path)
                if branch_name:
                    orphan_branches.add(branch_name)

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
                branch_name = metadata_by_path.get(str(run_dir.resolve()), (None, None))[0]
                await self._remove_run_path(run_dir, branch_name)
                self._remove_run_metadata(issue_dir.name, run_dir.name)
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
        for metadata_path in sorted(orphan_metadata_paths):
            metadata_path.unlink(missing_ok=True)
            issue_dir = metadata_path.parent
            if issue_dir.exists() and issue_dir.is_dir() and not any(issue_dir.iterdir()):
                issue_dir.rmdir()

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

    def _metadata_path(self, workspace_key: str, run_id: str) -> Path:
        return (self._issue_path(workspace_key) / f".{run_id}{RUN_METADATA_SUFFIX}").resolve()

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

    def _write_run_metadata(self, workspace: Workspace) -> None:
        metadata_path = self._metadata_path(workspace.workspace_key, workspace.run_id)
        metadata_path.write_text(
            json.dumps(
                {
                    "workspace_key": workspace.workspace_key,
                    "run_id": workspace.run_id,
                    "branch_name": workspace.branch_name,
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        metadata_path.chmod(METADATA_MODE)

    def _read_run_branch_name(self, workspace_key: str, run_id: str) -> str | None:
        metadata_path = self._metadata_path(workspace_key, run_id)
        if not metadata_path.exists():
            return None
        try:
            data = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            LOGGER.warning("Failed to read workspace metadata %s: %s", metadata_path, exc)
            return None
        branch_name = data.get("branch_name")
        return branch_name if isinstance(branch_name, str) and branch_name else None

    def _remove_run_metadata(self, workspace_key: str, run_id: str) -> None:
        self._metadata_path(workspace_key, run_id).unlink(missing_ok=True)

    def _list_run_metadata(self) -> dict[str, tuple[str | None, Path]]:
        root = self.workspace.root.expanduser().resolve()
        result: dict[str, tuple[str | None, Path]] = {}
        if not root.exists():
            return result
        for issue_dir in sorted(root.iterdir()):
            if not issue_dir.is_dir() or issue_dir.name == BARE_REPO_DIRNAME:
                continue
            for metadata_path in sorted(issue_dir.glob(f"*{RUN_METADATA_SUFFIX}")):
                try:
                    data = json.loads(metadata_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    LOGGER.warning("Failed to read workspace metadata %s: %s", metadata_path, exc)
                    continue
                run_id = data.get("run_id")
                if not isinstance(run_id, str) or not run_id:
                    continue
                branch_name = data.get("branch_name")
                if not isinstance(branch_name, str):
                    branch_name = None
                run_path = self._run_path(issue_dir.name, run_id)
                result[str(run_path)] = (branch_name, metadata_path)
        return result

    async def _materialize_existing_branch_worktree(
        self, run_path: Path, branch_name: str, run_id: str
    ) -> None:
        """Check out an existing remote branch into a new worktree for PR feedback."""
        if not self.workspace.repo_url:
            raise WorkspaceError("workspace_repo_url_required")

        bare = self._bare_repo_path()
        async with _bare_repo_lock(bare):
            if not bare.exists():
                await _run_git(
                    bare.parent, ["clone", "--bare", self.workspace.repo_url, bare.name]
                )
            else:
                try:
                    await _run_git(bare, ["fetch", "--prune", "origin"])
                except GitCommandError as exc:
                    LOGGER.warning("git fetch failed before PR feedback checkout: %s", exc)

        # Use a unique local branch name to avoid conflicts with the original branch
        # that may still be registered in the bare repo from the initial run.
        # In a bare clone, git fetch stores branches as refs/heads/* (not
        # refs/remotes/origin/*), so we reference the branch directly without
        # the "origin/" prefix.
        local_branch = f"{branch_name}-fb-{run_id}"
        try:
            await _run_git(
                bare,
                ["worktree", "add", "-b", local_branch, str(run_path), branch_name],
            )
        except GitCommandError as exc:
            shutil.rmtree(run_path, ignore_errors=True)
            raise WorkspaceError(f"workspace_git_worktree_add_failed:{exc}") from exc

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
        finally:
            self._remove_run_metadata(workspace.workspace_key, workspace.run_id)


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
