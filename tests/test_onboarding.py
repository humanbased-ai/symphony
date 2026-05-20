import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from symphony.auth import load_local_linear_token, save_local_linear_token
from symphony.onboarding import (
    InitConfig,
    default_workspace_root,
    detect_repo_shape,
    generate_workflow,
    write_workflow,
)
from symphony.workflow import parse_workflow


class OnboardingTests(unittest.TestCase):
    def test_generate_workflow_uses_preset_and_parseable_front_matter(self):
        content = generate_workflow(
            InitConfig(
                project_slug="symphony-ai-agent-orchestration",
                preset="codex-safe",
                workspace_root="~/.symphony/workspaces/symphony",
                runner="codex",
            )
        )

        workflow = parse_workflow(content)

        self.assertEqual("symphony-ai-agent-orchestration", workflow.config["tracker"]["project_slug"])
        self.assertEqual(1, workflow.config["agent"]["max_concurrent_agents"])
        self.assertEqual("never", workflow.config["codex"]["approval_policy"])
        self.assertIn("{{ issue.identifier }}", workflow.prompt_template)

    def test_generate_workflow_claude_runner_includes_pr_prompt(self):
        content = generate_workflow(
            InitConfig(
                project_slug="my-project",
                preset="codex-safe",
                workspace_root="~/.symphony/workspaces/my-project",
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
        self.assertEqual("~/.symphony/workspaces/A-B-C.1", default_workspace_root(" A/B C.1 "))

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

    def test_new_mode_includes_gh_repo_create_hint(self):
        content = generate_workflow(
            InitConfig(
                project_slug="example",
                runner="claude_code",
                github_org="acme",
                github_repo="repo",
                repo_mode="new",
            )
        )
        self.assertIn("New project scope", content)
        self.assertIn("gh repo create acme/repo", content)

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
