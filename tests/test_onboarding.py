import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jazzband.auth import load_local_linear_token, save_local_linear_token
from jazzband.onboarding import (
    InitConfig,
    default_workspace_root,
    detect_available_runners,

    detect_repo_shape,
    generate_workflow,
    write_workflow,
)
from jazzband.workflow import parse_workflow
from jazzband.config import AcceptanceConfig


class OnboardingTests(unittest.TestCase):
    def test_generate_workflow_uses_preset_and_parseable_front_matter(self):
        content = generate_workflow(
            InitConfig(
                project_slug="jazzband-ai-agent-orchestration",
                preset="codex-safe",
                workspace_root="~/.jazzband/workspaces/jazzband",
                runner="codex",
            )
        )

        workflow = parse_workflow(content)

        self.assertEqual("jazzband-ai-agent-orchestration", workflow.config["tracker"]["project_slug"])
        self.assertEqual(3, workflow.config["agent"]["max_concurrent_agents"])
        self.assertEqual("never", workflow.config["codex"]["approval_policy"])
        self.assertIn("{{ issue.identifier }}", workflow.prompt_template)

    def test_generate_workflow_claude_runner_includes_pr_prompt(self):
        content = generate_workflow(
            InitConfig(
                project_slug="my-project",
                preset="codex-safe",
                workspace_root="~/.jazzband/workspaces/my-project",
                runner="claude_code",
                github_org="acme-corp",
            )
        )

        workflow = parse_workflow(content)

        self.assertEqual("claude_code", workflow.config["agent"]["runner"])
        self.assertNotIn("codex", workflow.config)
        self.assertIn("acme-corp", workflow.prompt_template)
        self.assertIn("{{ issue.identifier }}", workflow.prompt_template)
        self.assertIn("issue.comments", workflow.prompt_template)
        self.assertIn("In Review", workflow.prompt_template)

    def test_default_workspace_root_sanitizes_project_slug(self):
        self.assertEqual("~/.jazzband/workspaces/A-B-C.1", default_workspace_root(" A/B C.1 "))

    def test_generate_workflow_omits_acceptance_by_default_with_github(self):
        """``jazzband init`` ships the acceptance gate disabled by default —
        the gate dispatches an extra judge agent on every PR convergence, so
        new projects must opt in explicitly (--acceptance or an interactive
        ``y``) rather than discover the cost after the fact."""
        content = generate_workflow(
            InitConfig(
                project_slug="my-project",
                preset="codex-safe",
                workspace_root="~/.jazzband/workspaces/my-project",
                runner="claude_code",
                github_org="acme-corp",
                github_repo="widget",
            )
        )
        workflow = parse_workflow(content)
        self.assertNotIn("acceptance", workflow.config)

    def test_generate_workflow_writes_acceptance_block_when_opted_in(self):
        """Explicit opt-in (--acceptance or an interactive ``y``) writes the
        full block with production-safe values: ``auto_merge`` and
        ``bounce_back_on_fail`` stay false so Phase 1 only judges and
        escalates to a human."""
        content = generate_workflow(
            InitConfig(
                project_slug="my-project",
                preset="codex-safe",
                workspace_root="~/.jazzband/workspaces/my-project",
                runner="claude_code",
                github_org="acme-corp",
                github_repo="widget",
                acceptance_enabled=True,
            )
        )
        workflow = parse_workflow(content)

        self.assertIn("acceptance", workflow.config)
        acceptance_block = workflow.config["acceptance"]
        self.assertTrue(acceptance_block["enabled"])
        self.assertFalse(acceptance_block["auto_merge"])
        self.assertEqual("auto", acceptance_block["review_source"])
        # ``crosscheck_wait_seconds`` is written explicitly so a user reading
        # the generated WORKFLOW.md sees the grace-window knob and knows it
        # is tunable, rather than having to discover the default in source.
        self.assertIn("crosscheck_wait_seconds", acceptance_block)
        # ``bounce_back_on_fail`` is written explicitly false. Production
        # default is "judge → comment → human decides"; flipping this to
        # true is the opt-in for fully-automated re-dispatch.
        self.assertIn("bounce_back_on_fail", acceptance_block)
        self.assertFalse(acceptance_block["bounce_back_on_fail"])
        # Sanity: the block round-trips through the same parser the runtime uses.
        # If a future edit drifts the scaffold away from the schema, this fails.
        parsed = AcceptanceConfig.from_mapping(workflow.config)
        self.assertTrue(parsed.enabled)
        self.assertFalse(parsed.auto_merge)
        self.assertGreater(parsed.crosscheck_wait_seconds, 0)
        self.assertIn("SPEC.md", parsed.guard_paths)

    def test_generate_workflow_omits_acceptance_block_when_explicitly_opted_out(self):
        """Explicit ``--no-acceptance`` behaves the same as the default: the
        block is omitted entirely, not written as ``enabled: false``. Empty
        block = no surprise gate on next run; explicit ``false`` would still
        keep the config noise."""
        content = generate_workflow(
            InitConfig(
                project_slug="my-project",
                preset="codex-safe",
                workspace_root="~/.jazzband/workspaces/my-project",
                runner="claude_code",
                github_org="acme-corp",
                github_repo="widget",
                acceptance_enabled=False,
            )
        )
        workflow = parse_workflow(content)
        self.assertNotIn("acceptance", workflow.config)

    def test_generate_workflow_omits_acceptance_block_without_github(self):
        """Without github configured the acceptance gate would no-op (it posts
        verdicts as PR comments). Even an explicit opt-in is overridden:
        omitting the block avoids dangling config that pretends a feature is
        one flag away when it actually is not."""
        content = generate_workflow(
            InitConfig(
                project_slug="my-project",
                preset="codex-safe",
                workspace_root="~/.jazzband/workspaces/my-project",
                runner="codex",
                acceptance_enabled=True,
            )
        )
        workflow = parse_workflow(content)
        self.assertNotIn("acceptance", workflow.config)

    def test_write_workflow_refuses_to_overwrite_by_default(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workflow_path = Path(temp_dir) / "WORKFLOW.md"
            workflow_path.write_text("existing", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "workflow_file_exists"):
                write_workflow(workflow_path, "new")

    def test_local_linear_credentials_round_trip_with_private_file_mode(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "credentials.json"

            saved = save_local_linear_token("lin_secret", path=path)

            self.assertEqual(path, saved)
            self.assertEqual("lin_secret", load_local_linear_token(path=path))
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


class RunnerPickerAndReviewStrategyTests(unittest.TestCase):
    """IN-285: detect_available_runners + review block in generated workflow."""

    def test_detect_available_runners_returns_both_when_present(self):
        # shutil.which returns the path when found, None otherwise. Patch it
        # to simulate both binaries installed.
        with patch("jazzband.onboarding.shutil.which", side_effect=lambda cmd: f"/usr/bin/{cmd}"):
            self.assertEqual(
                ("claude_code", "codex"),
                detect_available_runners(),
            )

    def test_detect_available_runners_returns_only_claude_when_codex_missing(self):
        def which(cmd: str) -> str | None:
            return "/usr/bin/claude" if cmd == "claude" else None

        with patch("jazzband.onboarding.shutil.which", side_effect=which):
            self.assertEqual(("claude_code",), detect_available_runners())

    def test_detect_available_runners_empty_when_neither_present(self):
        with patch("jazzband.onboarding.shutil.which", return_value=None):
            self.assertEqual((), detect_available_runners())

    def test_cross_vendor_review_writes_other_vendor_as_reviewer(self):
        content = generate_workflow(
            InitConfig(
                project_slug="x",
                runner="claude_code",
                workspace_root="/tmp/x",
                review_strategy="cross-vendor",
            )
        )
        workflow = parse_workflow(content)
        review = workflow.config["review"]
        self.assertEqual("cross-vendor", review["strategy"])
        self.assertEqual("codex", review["reviewer"])
        self.assertTrue(review["enabled"])

    def test_cross_vendor_review_with_codex_primary_uses_claude_reviewer(self):
        content = generate_workflow(
            InitConfig(
                project_slug="x",
                runner="codex",
                workspace_root="/tmp/x",
                review_strategy="cross-vendor",
            )
        )
        workflow = parse_workflow(content)
        self.assertEqual("claude_code", workflow.config["review"]["reviewer"])

    def test_single_vendor_review_uses_primary_as_reviewer(self):
        content = generate_workflow(
            InitConfig(
                project_slug="x",
                runner="claude_code",
                workspace_root="/tmp/x",
                review_strategy="single-vendor",
            )
        )
        workflow = parse_workflow(content)
        review = workflow.config["review"]
        self.assertEqual("single-vendor", review["strategy"])
        self.assertEqual("claude_code", review["reviewer"])

    def test_skip_strategy_writes_no_review_block(self):
        content = generate_workflow(
            InitConfig(
                project_slug="x",
                runner="claude_code",
                workspace_root="/tmp/x",
                review_strategy="skip",
            )
        )
        workflow = parse_workflow(content)
        self.assertNotIn("review", workflow.config)

def _git_available() -> bool:
    import shutil as _shutil
    return _shutil.which("git") is not None


def _init_git_with_remote(path: Path) -> None:
    subprocess.check_call(["git", "init", "-q", str(path)])
    subprocess.check_call(
        ["git", "-C", str(path), "remote", "add", "origin", "https://example.com/x.git"]
    )


@unittest.skipUnless(_git_available(), "git is required for repo-shape detection tests")
class DetectRepoShapeTests(unittest.TestCase):
    def test_returns_new_when_no_git_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual("new", detect_repo_shape(tmp))

    def test_returns_new_when_no_remote_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.check_call(["git", "init", "-q", tmp])
            self.assertEqual("new", detect_repo_shape(tmp))

    def test_returns_single_when_remote_set_and_no_monorepo_signals(self):
        with tempfile.TemporaryDirectory() as tmp:
            _init_git_with_remote(Path(tmp))
            self.assertEqual("single", detect_repo_shape(tmp))

    def test_detects_pnpm_workspace_as_monorepo(self):
        with tempfile.TemporaryDirectory() as tmp:
            _init_git_with_remote(Path(tmp))
            (Path(tmp) / "pnpm-workspace.yaml").write_text("packages:\n  - 'apps/*'\n")
            self.assertEqual("monorepo", detect_repo_shape(tmp))

    def test_detects_nx_json_as_monorepo(self):
        with tempfile.TemporaryDirectory() as tmp:
            _init_git_with_remote(Path(tmp))
            (Path(tmp) / "nx.json").write_text("{}")
            self.assertEqual("monorepo", detect_repo_shape(tmp))

    def test_detects_go_work_as_monorepo(self):
        with tempfile.TemporaryDirectory() as tmp:
            _init_git_with_remote(Path(tmp))
            (Path(tmp) / "go.work").write_text("go 1.22\n")
            self.assertEqual("monorepo", detect_repo_shape(tmp))

    def test_detects_npm_workspaces_field_as_monorepo(self):
        with tempfile.TemporaryDirectory() as tmp:
            _init_git_with_remote(Path(tmp))
            (Path(tmp) / "package.json").write_text(json.dumps({"workspaces": ["packages/*"]}))
            self.assertEqual("monorepo", detect_repo_shape(tmp))

    def test_detects_packages_directory_as_monorepo(self):
        with tempfile.TemporaryDirectory() as tmp:
            _init_git_with_remote(Path(tmp))
            packages = Path(tmp) / "packages"
            packages.mkdir()
            (packages / "core").mkdir()
            self.assertEqual("monorepo", detect_repo_shape(tmp))

    def test_empty_packages_directory_is_not_monorepo_signal(self):
        # A bare `packages/` with no children isn't a workspace setup.
        with tempfile.TemporaryDirectory() as tmp:
            _init_git_with_remote(Path(tmp))
            (Path(tmp) / "packages").mkdir()
            self.assertEqual("single", detect_repo_shape(tmp))


class GenerateWorkflowRepoModeTests(unittest.TestCase):
    def test_monorepo_mode_prepends_self_scoping_preamble(self):
        content = generate_workflow(
            InitConfig(
                project_slug="example",
                runner="claude_code",
                github_org="acme",
                github_repo="repo",
                repo_mode="monorepo",
            )
        )
        self.assertIn("Monorepo scope", content)
        self.assertIn("smallest", content)

    def test_new_mode_never_creates_repository_in_issue_workspace(self):
        for runner in ("claude_code", "codex"):
            with self.subTest(runner=runner):
                content = generate_workflow(
                    InitConfig(
                        project_slug="example",
                        runner=runner,
                        github_org="acme",
                        github_repo="repo",
                        repo_mode="new",
                    )
                )
                workflow = parse_workflow(content)
                self.assertEqual(runner, workflow.config["agent"]["runner"])
                self.assertNotIn("New project scope", workflow.prompt_template)
                self.assertNotIn("gh repo create", workflow.prompt_template)
                if runner == "claude_code":
                    self.assertIn("gh repo clone acme/repo .", workflow.prompt_template)
                else:
                    self.assertNotIn("gh repo clone", workflow.prompt_template)

    def test_single_mode_has_no_preamble(self):
        content = generate_workflow(
            InitConfig(
                project_slug="example",
                runner="claude_code",
                github_org="acme",
                github_repo="repo",
                repo_mode="single",
            )
        )
        self.assertNotIn("Monorepo scope", content)
        self.assertNotIn("New project scope", content)


if __name__ == "__main__":
    unittest.main()
