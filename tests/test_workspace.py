import os
import shlex
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from symphony.config import HooksConfig, WorkspaceConfig
from symphony.tracker.models import Issue
from symphony.workspace import (
    BARE_REPO_DIRNAME,
    WorkspaceError,
    WorkspaceHookError,
    WorkspaceManager,
    is_path_within_root,
    sanitize_workspace_key,
)


def make_issue(identifier: str = "IN-42", branch_name: str | None = None) -> Issue:
    return Issue(
        id="issue-id",
        identifier=identifier,
        title="Test issue",
        description=None,
        priority=None,
        state="In Progress",
        branch_name=branch_name,
        url=None,
    )


def _git_available() -> bool:
    return shutil.which("git") is not None


def _make_upstream_repo(tmp: Path) -> str:
    """Create a bare upstream repo with one commit on ``main``; return its path."""

    upstream = tmp / "upstream.git"
    subprocess.check_call(["git", "init", "--bare", str(upstream), "--initial-branch=main"])
    seed = tmp / "seed"
    subprocess.check_call(["git", "clone", str(upstream), str(seed)])
    (seed / "README.md").write_text("hello\n", encoding="utf-8")
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@test",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@test",
    }
    subprocess.check_call(["git", "-C", str(seed), "add", "README.md"], env=env)
    subprocess.check_call(["git", "-C", str(seed), "commit", "-m", "init"], env=env)
    subprocess.check_call(["git", "-C", str(seed), "push", "origin", "main"], env=env)
    return str(upstream)


class WorkspaceManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_workspace_created_with_per_run_subdirectory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = WorkspaceManager(WorkspaceConfig(Path(temp_dir) / "workspaces"))

            workspace = await manager.prepare_for_run(make_issue("IN-42 add-retry"), run_id="abc123")

            self.assertEqual("IN-42_add-retry", workspace.workspace_key)
            self.assertEqual("abc123", workspace.run_id)
            self.assertEqual(
                (Path(temp_dir) / "workspaces" / "IN-42_add-retry" / "abc123").resolve(),
                workspace.path,
            )
            self.assertTrue(workspace.created_now)
            self.assertTrue(workspace.path.is_dir())
            self.assertEqual(stat.S_IRWXU, stat.S_IMODE(workspace.path.stat().st_mode))
            self.assertEqual(stat.S_IRWXU, stat.S_IMODE(workspace.path.parent.parent.stat().st_mode))
            # Plain mode (no repo_url) ⇒ no branch is created.
            self.assertIsNone(workspace.branch_name)

    async def test_per_run_dirs_are_unique_when_run_id_not_provided(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = WorkspaceManager(WorkspaceConfig(Path(temp_dir) / "workspaces"))

            first = await manager.prepare_for_run(make_issue("IN-42"))
            second = await manager.prepare_for_run(make_issue("IN-42"))

            self.assertNotEqual(first.run_id, second.run_id)
            self.assertNotEqual(first.path, second.path)
            self.assertTrue(first.path.is_dir())
            self.assertTrue(second.path.is_dir())

    async def test_collision_on_explicit_run_id_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = WorkspaceManager(WorkspaceConfig(Path(temp_dir) / "workspaces"))
            await manager.prepare_for_run(make_issue("IN-42"), run_id="dup")

            with self.assertRaisesRegex(WorkspaceError, "workspace_run_path_already_exists"):
                await manager.prepare_for_run(make_issue("IN-42"), run_id="dup")

    async def test_after_create_runs_for_every_dispatch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "events.log"
            manager = WorkspaceManager(
                WorkspaceConfig(Path(temp_dir) / "workspaces"),
                HooksConfig(after_create=f"printf 'after_create\\n' >> {shlex.quote(str(log_path))}"),
            )

            await manager.prepare_for_run(make_issue("IN-42"), run_id="r1")
            await manager.prepare_for_run(make_issue("IN-42"), run_id="r2")

            self.assertEqual(["after_create", "after_create"], log_path.read_text(encoding="utf-8").splitlines())

    async def test_hooks_run_in_lifecycle_order_and_cleanup_removes_workspace(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "events.log"
            log = shlex.quote(str(log_path))
            manager = WorkspaceManager(
                WorkspaceConfig(Path(temp_dir) / "workspaces"),
                HooksConfig(
                    after_create=f"printf 'after_create\\n' >> {log}",
                    before_run=f"printf 'before_run\\n' >> {log}",
                    after_run=f"printf 'after_run\\n' >> {log}",
                    before_remove=f"printf 'before_remove\\n' >> {log}",
                ),
            )

            workspace = await manager.prepare_for_run(make_issue("IN-42"), run_id="r1")
            await manager.before_run(workspace)
            await manager.after_run(workspace)
            removed = await manager.cleanup_for_run(workspace)

            self.assertTrue(removed)
            self.assertFalse(workspace.path.exists())
            self.assertFalse(workspace.path.parent.exists())
            self.assertEqual(
                ["after_create", "before_run", "after_run", "before_remove"],
                log_path.read_text(encoding="utf-8").splitlines(),
            )

    async def test_cleanup_for_run_leaves_sibling_runs_intact(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = WorkspaceManager(WorkspaceConfig(Path(temp_dir) / "workspaces"))
            run1 = await manager.prepare_for_run(make_issue("IN-42"), run_id="r1")
            run2 = await manager.prepare_for_run(make_issue("IN-42"), run_id="r2")

            removed = await manager.cleanup_for_run(run1)

            self.assertTrue(removed)
            self.assertFalse(run1.path.exists())
            self.assertTrue(run2.path.exists())
            self.assertTrue(run2.path.parent.exists())

    async def test_blocking_hook_failure_aborts_progression(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = WorkspaceManager(
                WorkspaceConfig(Path(temp_dir) / "workspaces"),
                HooksConfig(after_create="exit 7"),
            )

            with self.assertRaisesRegex(WorkspaceHookError, "workspace_hook_failed:after_create:7"):
                await manager.prepare_for_run(make_issue("IN-42"), run_id="r1")

            self.assertFalse((Path(temp_dir) / "workspaces" / "IN-42" / "r1").exists())

    async def test_nonblocking_hooks_are_best_effort(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = WorkspaceManager(
                WorkspaceConfig(Path(temp_dir) / "workspaces"),
                HooksConfig(after_run="exit 2", before_remove="exit 3"),
            )

            workspace = await manager.prepare_for_run(make_issue("IN-42"), run_id="r1")

            with self.assertLogs("symphony.workspace", level="WARNING") as logs:
                self.assertIsNone(await manager.after_run(workspace))
                self.assertTrue(await manager.cleanup_for_run(workspace))

            self.assertFalse(workspace.path.exists())
            self.assertIn("Workspace hook after_run failed", logs.output[0])
            self.assertIn("Workspace hook before_remove failed", logs.output[1])

    async def test_hook_timeout_is_enforced(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = WorkspaceManager(
                WorkspaceConfig(Path(temp_dir) / "workspaces"),
                HooksConfig(before_run="sleep 2", timeout_ms=50),
            )

            workspace = await manager.prepare_for_run(make_issue("IN-42"), run_id="r1")

            with self.assertRaisesRegex(WorkspaceHookError, "workspace_hook_timeout:before_run"):
                await manager.before_run(workspace)

    async def test_cleanup_for_run_can_keep_failed_workspace_for_debugging(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = WorkspaceManager(WorkspaceConfig(Path(temp_dir) / "workspaces"))
            workspace = await manager.prepare_for_run(make_issue("IN-42"), run_id="r1")

            removed = await manager.cleanup_for_run(workspace, failed=True, keep_on_failure=True)

            self.assertFalse(removed)
            self.assertTrue(workspace.path.exists())

    async def test_terminal_cleanup_removes_all_per_run_dirs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = WorkspaceManager(WorkspaceConfig(Path(temp_dir) / "workspaces"))
            await manager.prepare_for_run(make_issue("IN-42"), run_id="r1")
            await manager.prepare_for_run(make_issue("IN-42"), run_id="r2")

            removed = await manager.cleanup("IN-42")

            self.assertTrue(removed)
            self.assertFalse((Path(temp_dir) / "workspaces" / "IN-42").exists())

    async def test_terminal_cleanup_runs_before_remove_with_contents_intact(self):
        # Regression: cleanup used to delete all per-run dirs first and then
        # run before_remove on the (empty) issue dir, so hooks that archive or
        # inspect run contents got nothing. The hook now runs per-run-dir
        # before that dir is deleted.
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "events.log"
            manager = WorkspaceManager(
                WorkspaceConfig(Path(temp_dir) / "workspaces"),
                HooksConfig(
                    # The hook records the run-id directory contents at hook-time;
                    # if the hook sees an empty dir we'd record nothing.
                    before_remove=f"ls -1 . >> {shlex.quote(str(log_path))}",
                ),
            )
            run1 = await manager.prepare_for_run(make_issue("IN-42"), run_id="r1")
            run2 = await manager.prepare_for_run(make_issue("IN-42"), run_id="r2")
            (run1.path / "artifact-a.txt").write_text("a", encoding="utf-8")
            (run2.path / "artifact-b.txt").write_text("b", encoding="utf-8")

            await manager.cleanup("IN-42")

            recorded = log_path.read_text(encoding="utf-8").splitlines()
            self.assertIn("artifact-a.txt", recorded)
            self.assertIn("artifact-b.txt", recorded)

    async def test_workspace_path_validation_rejects_out_of_root_paths(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "workspaces"
            outside = Path(temp_dir) / "outside"
            outside.mkdir()
            manager = WorkspaceManager(WorkspaceConfig(root))

            with self.assertRaisesRegex(WorkspaceError, "workspace_path_outside_root"):
                manager.validate_workspace(outside)

    async def test_per_run_log_path_provided_when_logs_root_configured(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            logs_root = Path(temp_dir) / "logs"
            manager = WorkspaceManager(
                WorkspaceConfig(Path(temp_dir) / "workspaces"),
                logs_root=logs_root,
            )

            workspace = await manager.prepare_for_run(make_issue("IN-42"), run_id="r1")

            self.assertIsNotNone(workspace.run_log_path)
            self.assertEqual(logs_root.resolve() / "IN-42" / "r1.log", workspace.run_log_path)
            # Parent directory exists so the runtime can immediately append.
            self.assertTrue(workspace.run_log_path.parent.is_dir())

    async def test_per_run_log_path_absent_when_logs_root_unset(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = WorkspaceManager(WorkspaceConfig(Path(temp_dir) / "workspaces"))

            workspace = await manager.prepare_for_run(make_issue("IN-42"), run_id="r1")

            self.assertIsNone(workspace.run_log_path)

    async def test_sweep_removes_orphan_per_run_dirs_at_startup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "workspaces"
            manager = WorkspaceManager(WorkspaceConfig(root))
            await manager.prepare_for_run(make_issue("IN-42"), run_id="r1")
            await manager.prepare_for_run(make_issue("IN-99"), run_id="r2")

            removed = await manager.sweep_stale_worktrees()

            self.assertEqual(2, removed)
            self.assertFalse((root / "IN-42").exists())
            self.assertFalse((root / "IN-99").exists())

    async def test_sweep_is_no_op_when_root_missing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = WorkspaceManager(WorkspaceConfig(Path(temp_dir) / "absent"))

            self.assertEqual(0, await manager.sweep_stale_worktrees())

    def test_identifier_sanitization_and_root_containment_helpers(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "workspaces"
            sanitized = sanitize_workspace_key("../../IN-42 add/retry")

            self.assertEqual(".._.._IN-42_add_retry", sanitized)
            self.assertTrue(is_path_within_root(root / sanitized, root))
            self.assertFalse(is_path_within_root(Path(temp_dir) / "elsewhere", root))

            with self.assertRaisesRegex(WorkspaceError, "workspace_identifier_required"):
                sanitize_workspace_key("  ")


class WorkspaceHookEnvironmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_hooks_run_in_workspace_with_configured_environment(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = WorkspaceManager(
                WorkspaceConfig(Path(temp_dir) / "workspaces"),
                HooksConfig(before_run="printf \"$SYMPHONY_MARKER\" > marker.txt"),
                environ={"SYMPHONY_MARKER": "from-hook"},
            )

            workspace = await manager.prepare_for_run(make_issue("IN-42"), run_id="r1")
            await manager.before_run(workspace)

            self.assertEqual("from-hook", (workspace.path / "marker.txt").read_text(encoding="utf-8"))
            self.assertNotIn("SYMPHONY_MARKER", os.environ)


@unittest.skipUnless(_git_available(), "git is required for worktree mode tests")
class WorkspaceManagerGitModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_git_mode_initializes_bare_clone_and_creates_worktree(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp = Path(temp_dir)
            upstream = _make_upstream_repo(tmp)
            manager = WorkspaceManager(
                WorkspaceConfig(root=tmp / "workspaces", repo_url=upstream, default_branch="main"),
            )

            workspace = await manager.prepare_for_run(
                make_issue("IN-42", branch_name="feat/in-42-login"),
                run_id="abc",
            )

            self.assertTrue((tmp / "workspaces" / BARE_REPO_DIRNAME).is_dir())
            self.assertTrue((workspace.path / "README.md").is_file())
            self.assertEqual("feat/in-42-login-abc", workspace.branch_name)
            head = subprocess.check_output(
                ["git", "-C", str(workspace.path), "symbolic-ref", "--short", "HEAD"],
                text=True,
            ).strip()
            self.assertEqual("feat/in-42-login-abc", head)

    async def test_git_mode_falls_back_to_identifier_when_branch_name_missing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp = Path(temp_dir)
            upstream = _make_upstream_repo(tmp)
            manager = WorkspaceManager(
                WorkspaceConfig(root=tmp / "workspaces", repo_url=upstream, default_branch="main"),
            )

            workspace = await manager.prepare_for_run(make_issue("IN-99"), run_id="xyz")

            self.assertEqual("issue/in-99-xyz", workspace.branch_name)

    async def test_git_mode_cleanup_removes_worktree_and_branch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp = Path(temp_dir)
            upstream = _make_upstream_repo(tmp)
            manager = WorkspaceManager(
                WorkspaceConfig(root=tmp / "workspaces", repo_url=upstream, default_branch="main"),
            )
            workspace = await manager.prepare_for_run(
                make_issue("IN-42", branch_name="feat/login"),
                run_id="abc",
            )

            removed = await manager.cleanup_for_run(workspace)

            self.assertTrue(removed)
            self.assertFalse(workspace.path.exists())
            bare = tmp / "workspaces" / BARE_REPO_DIRNAME
            branches = subprocess.check_output(
                ["git", "-C", str(bare), "branch", "--list"], text=True
            )
            self.assertNotIn("feat/login-abc", branches)

    async def test_git_mode_supports_concurrent_runs_for_same_issue(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp = Path(temp_dir)
            upstream = _make_upstream_repo(tmp)
            manager = WorkspaceManager(
                WorkspaceConfig(root=tmp / "workspaces", repo_url=upstream, default_branch="main"),
            )

            first = await manager.prepare_for_run(
                make_issue("IN-42", branch_name="feat/login"), run_id="r1"
            )
            second = await manager.prepare_for_run(
                make_issue("IN-42", branch_name="feat/login"), run_id="r2"
            )

            self.assertNotEqual(first.path, second.path)
            self.assertNotEqual(first.branch_name, second.branch_name)
            self.assertTrue((first.path / "README.md").is_file())
            self.assertTrue((second.path / "README.md").is_file())

    async def test_git_mode_concurrent_first_dispatches_share_one_bare_clone(self):
        # Regression: multiple concurrent prepare_for_run calls into an empty
        # workspace root each saw `.repo.git` missing and tried `git clone
        # --bare` simultaneously. One succeeded, the others failed against the
        # half-created destination, marking the losing workers as failed.
        import asyncio

        with tempfile.TemporaryDirectory() as temp_dir:
            tmp = Path(temp_dir)
            upstream = _make_upstream_repo(tmp)
            manager = WorkspaceManager(
                WorkspaceConfig(root=tmp / "workspaces", repo_url=upstream, default_branch="main"),
            )

            results = await asyncio.gather(
                manager.prepare_for_run(make_issue("IN-A", branch_name="feat/a"), run_id="a"),
                manager.prepare_for_run(make_issue("IN-B", branch_name="feat/b"), run_id="b"),
                manager.prepare_for_run(make_issue("IN-C", branch_name="feat/c"), run_id="c"),
            )

            # All three workspaces materialized successfully.
            for workspace in results:
                self.assertTrue((workspace.path / "README.md").is_file())
            # Exactly one bare clone exists.
            self.assertTrue((tmp / "workspaces" / BARE_REPO_DIRNAME).is_dir())

    async def test_git_mode_terminal_cleanup_deletes_all_per_run_branches(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp = Path(temp_dir)
            upstream = _make_upstream_repo(tmp)
            manager = WorkspaceManager(
                WorkspaceConfig(root=tmp / "workspaces", repo_url=upstream, default_branch="main"),
            )
            await manager.prepare_for_run(
                make_issue("IN-42", branch_name="feat/login"), run_id="r1"
            )
            await manager.prepare_for_run(
                make_issue("IN-42", branch_name="feat/login"), run_id="r2"
            )

            removed = await manager.cleanup("IN-42")

            self.assertTrue(removed)
            bare = tmp / "workspaces" / BARE_REPO_DIRNAME
            branches = subprocess.check_output(
                ["git", "-C", str(bare), "branch", "--list"], text=True
            )
            # Both per-run branches must be gone — no accumulation in .repo.git.
            self.assertNotIn("feat/login-r1", branches)
            self.assertNotIn("feat/login-r2", branches)

    async def test_git_mode_sweep_deletes_orphan_metadata_branches(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp = Path(temp_dir)
            upstream = _make_upstream_repo(tmp)
            manager = WorkspaceManager(
                WorkspaceConfig(root=tmp / "workspaces", repo_url=upstream, default_branch="main"),
            )
            workspace = await manager.prepare_for_run(
                make_issue("IN-42", branch_name="feat/login"), run_id="orphan"
            )
            # Simulate a crashed dispatch: the worktree directory is deleted
            # out-of-band, but git's worktree metadata + branch survive.
            shutil.rmtree(workspace.path)

            removed = await manager.sweep_stale_worktrees()

            self.assertEqual(0, removed)  # disk walk found nothing
            bare = tmp / "workspaces" / BARE_REPO_DIRNAME
            branches = subprocess.check_output(
                ["git", "-C", str(bare), "branch", "--list"], text=True
            )
            self.assertNotIn("feat/login-orphan", branches)

    async def test_git_mode_sweep_force_removes_orphan_worktrees(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp = Path(temp_dir)
            upstream = _make_upstream_repo(tmp)
            manager = WorkspaceManager(
                WorkspaceConfig(root=tmp / "workspaces", repo_url=upstream, default_branch="main"),
            )
            workspace = await manager.prepare_for_run(
                make_issue("IN-42", branch_name="feat/login"), run_id="orphan"
            )
            # Simulate a process crash: the worktree directory survives.
            self.assertTrue(workspace.path.is_dir())

            removed = await manager.sweep_stale_worktrees()

            self.assertEqual(1, removed)
            self.assertFalse(workspace.path.exists())
            bare = tmp / "workspaces" / BARE_REPO_DIRNAME
            worktrees = subprocess.check_output(
                ["git", "-C", str(bare), "worktree", "list", "--porcelain"], text=True
            )
            self.assertNotIn("orphan", worktrees)
