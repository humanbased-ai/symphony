import os
import stat
import tempfile
import unittest
from pathlib import Path

from symphony.auth import load_local_linear_token, save_local_linear_token
from symphony.config import AcceptanceConfig
from symphony.onboarding import InitConfig, default_workspace_root, generate_workflow, write_workflow
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
        self.assertEqual(3, workflow.config["agent"]["max_concurrent_agents"])
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

    def test_generate_workflow_writes_disabled_acceptance_block_when_github_configured(self):
        """``symphony init`` scaffolds an acceptance block so users do not need
        to consult docs to enable the gate — they only flip ``enabled`` to true.
        The block is intentionally disabled by default so existing onboarding
        behavior does not change for anyone who ignores it."""
        content = generate_workflow(
            InitConfig(
                project_slug="my-project",
                preset="codex-safe",
                workspace_root="~/.symphony/workspaces/my-project",
                runner="claude_code",
                github_org="acme-corp",
                github_repo="widget",
            )
        )
        workflow = parse_workflow(content)

        self.assertIn("acceptance", workflow.config)
        acceptance_block = workflow.config["acceptance"]
        self.assertFalse(acceptance_block["enabled"])
        self.assertFalse(acceptance_block["auto_merge"])
        self.assertEqual("auto", acceptance_block["review_source"])
        # Sanity: the block round-trips through the same parser the runtime uses.
        # If a future edit drifts the scaffold away from the schema, this fails.
        parsed = AcceptanceConfig.from_mapping(workflow.config)
        self.assertFalse(parsed.enabled)
        self.assertEqual("auto", parsed.review_source)
        self.assertIn("SPEC.md", parsed.guard_paths)

    def test_generate_workflow_omits_acceptance_block_without_github(self):
        """Without github configured the acceptance gate would no-op (it posts
        verdicts as PR comments). Omitting the block avoids dangling config
        that pretends a feature is one flag away when it actually is not."""
        content = generate_workflow(
            InitConfig(
                project_slug="my-project",
                preset="codex-safe",
                workspace_root="~/.symphony/workspaces/my-project",
                runner="codex",
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


if __name__ == "__main__":
    unittest.main()
