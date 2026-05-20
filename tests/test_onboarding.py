import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from symphony.auth import load_local_linear_token, save_local_linear_token
from symphony.onboarding import (
    InitConfig,
    default_workspace_root,
    detect_available_runners,
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


class RunnerPickerAndReviewStrategyTests(unittest.TestCase):
    """IN-285: detect_available_runners + review block in generated workflow."""

    def test_detect_available_runners_returns_both_when_present(self):
        # shutil.which returns the path when found, None otherwise. Patch it
        # to simulate both binaries installed.
        with patch("symphony.onboarding.shutil.which", side_effect=lambda cmd: f"/usr/bin/{cmd}"):
            self.assertEqual(
                ("claude_code", "codex"),
                detect_available_runners(),
            )

    def test_detect_available_runners_returns_only_claude_when_codex_missing(self):
        def which(cmd: str) -> str | None:
            return "/usr/bin/claude" if cmd == "claude" else None

        with patch("symphony.onboarding.shutil.which", side_effect=which):
            self.assertEqual(("claude_code",), detect_available_runners())

    def test_detect_available_runners_empty_when_neither_present(self):
        with patch("symphony.onboarding.shutil.which", return_value=None):
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


if __name__ == "__main__":
    unittest.main()
