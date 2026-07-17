import asyncio
import tempfile
import unittest
from pathlib import Path

from jazzband.auth import MissingLinearTokenError, TokenStore, save_local_linear_token
from jazzband.config import ConfigError, TrackerConfig, WorkflowConfig
from jazzband.workflow import WorkflowError, WorkflowReloader, parse_workflow, render_prompt, watch_workflow


class WorkflowConfigTests(unittest.TestCase):
    def test_parse_workflow_front_matter_and_prompt(self):
        workflow = parse_workflow(
            """---
tracker:
  kind: linear
  api_key: $WORKFLOW_LINEAR_KEY
  project_slug: jazzband-ai-agent-orchestration
---

You are working on {{ issue.identifier }}.
"""
        )

        self.assertEqual("jazzband-ai-agent-orchestration", workflow.config["tracker"]["project_slug"])
        self.assertEqual("You are working on {{ issue.identifier }}.", workflow.prompt_template)

    def test_non_map_front_matter_is_rejected(self):
        with self.assertRaisesRegex(WorkflowError, "workflow_front_matter_must_be_map"):
            parse_workflow("---\n- not\n- a\n- map\n---\nbody")

    def test_tracker_config_defaults_linear_fields(self):
        config = TrackerConfig.from_mapping({"tracker": {"kind": "linear"}})

        self.assertEqual("https://api.linear.app/graphql", config.endpoint)
        self.assertEqual(("Todo", "In Progress"), config.active_states)
        self.assertEqual(("Closed", "Cancelled", "Canceled", "Duplicate", "Done"), config.terminal_states)

    def test_token_resolution_env_wins_over_workflow(self):
        config = TrackerConfig.from_mapping({"tracker": {"api_key": "workflow-token"}})
        token = TokenStore(config, environ={"LINEAR_API_KEY": "env-token"}).resolve_linear_token()

        self.assertEqual("env-token", token)

    def test_token_resolution_supports_workflow_env_reference(self):
        config = TrackerConfig.from_mapping({"tracker": {"api_key": "$WORKFLOW_LINEAR_KEY"}})
        token = TokenStore(config, environ={"WORKFLOW_LINEAR_KEY": "referenced-token"}).resolve_linear_token()

        self.assertEqual("referenced-token", token)

    def test_empty_token_is_missing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = TrackerConfig.from_mapping({"tracker": {"api_key": "$WORKFLOW_LINEAR_KEY"}})

            with self.assertRaisesRegex(MissingLinearTokenError, "missing_tracker_api_key"):
                TokenStore(
                    config,
                    environ={"WORKFLOW_LINEAR_KEY": "  "},
                    credentials_path=Path(temp_dir) / "missing.json",
                ).resolve_linear_token()

    def test_token_resolution_falls_back_to_local_credentials_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            credentials_path = Path(temp_dir) / "credentials.json"
            save_local_linear_token("stored-token", path=credentials_path)
            config = TrackerConfig.from_mapping({"tracker": {"kind": "linear"}})

            token = TokenStore(config, environ={}, credentials_path=credentials_path).resolve_linear_token()

            self.assertEqual("stored-token", token)

    def test_workflow_config_resolves_core_defaults_and_relative_workspace_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workflow_path = Path(temp_dir) / "WORKFLOW.md"
            config = WorkflowConfig.from_mapping(
                {
                    "tracker": {
                        "kind": "linear",
                        "project_slug": "jazzband-ai-agent-orchestration",
                    },
                    "workspace": {"root": "tmp/workspaces"},
                    "agent": {
                        "max_concurrent_agents": "4",
                        "max_concurrent_agents_by_state": {
                            "Todo": 2,
                            "In Progress": "3",
                        },
                    },
                    "codex": {"command": "codex app-server --profile jazzband"},
                },
                workflow_path=workflow_path,
            )

            self.assertEqual((Path(temp_dir) / "tmp" / "workspaces").resolve(), config.workspace.root)
            self.assertEqual(30_000, config.polling.interval_ms)
            self.assertEqual(60_000, config.hooks.timeout_ms)
            self.assertEqual(4, config.agent.max_concurrent_agents)
            self.assertEqual({"todo": 2, "in progress": 3}, dict(config.agent.max_concurrent_agents_by_state))
            self.assertEqual("codex app-server --profile jazzband", config.codex.command)

    def test_agent_state_limits_are_immutable(self):
        config = WorkflowConfig.from_mapping(
            {
                "tracker": {"kind": "linear"},
                "agent": {"max_concurrent_agents_by_state": {"Todo": 2}},
            }
        )

        with self.assertRaises(TypeError):
            config.agent.max_concurrent_agents_by_state["todo"] = 99

    def test_invalid_agent_state_limit_entry_is_rejected(self):
        with self.assertRaisesRegex(ConfigError, "agent_state_limit_must_be_positive"):
            WorkflowConfig.from_mapping(
                {
                    "tracker": {"kind": "linear"},
                    "agent": {"max_concurrent_agents_by_state": {"Todo": 0}},
                }
            )

    def test_workspace_root_supports_env_reference_and_home_expansion(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = WorkflowConfig.from_mapping(
                {
                    "tracker": {"kind": "linear"},
                    "workspace": {"root": "$WORKSPACE_ROOT"},
                },
                workflow_path=Path(temp_dir) / "WORKFLOW.md",
                environ={"WORKSPACE_ROOT": "~/jazzband-integration"},
            )

            self.assertEqual(Path.home() / "jazzband-integration", config.workspace.root)

    def test_missing_workspace_root_env_reference_names_variable(self):
        with self.assertRaisesRegex(ConfigError, "env_var_not_set:WORKSPACE_ROOT"):
            WorkflowConfig.from_mapping(
                {
                    "tracker": {"kind": "linear"},
                    "workspace": {"root": "$WORKSPACE_ROOT"},
                },
                environ={},
            )

    def test_invalid_positive_int_config_is_rejected(self):
        with self.assertRaisesRegex(ConfigError, "agent_max_turns_must_be_positive"):
            WorkflowConfig.from_mapping({"tracker": {"kind": "linear"}, "agent": {"max_turns": 0}})

    def test_invalid_stall_timeout_is_rejected(self):
        with self.assertRaisesRegex(ConfigError, "codex_stall_timeout_ms_must_be_positive"):
            WorkflowConfig.from_mapping({"tracker": {"kind": "linear"}, "codex": {"stall_timeout_ms": 0}})

    def test_render_prompt_uses_strict_issue_context(self):
        rendered = render_prompt(
            "Work on {{ issue.identifier }} attempt={{ attempt }}.",
            issue={"identifier": "IN-170"},
            attempt=2,
        )

        self.assertEqual("Work on IN-170 attempt=2.", rendered)

    def test_render_prompt_rejects_unknown_variable(self):
        with self.assertRaisesRegex(WorkflowError, "template_render_error"):
            render_prompt("Work on {{ issue.missing }}.", issue={"identifier": "IN-170"})

    def test_render_prompt_uses_default_prompt_for_empty_body(self):
        rendered = render_prompt("", issue={"identifier": "IN-170"})

        self.assertEqual("You are working on an issue from Linear.", rendered)

    def test_invalid_yaml_is_parse_error(self):
        with self.assertRaisesRegex(WorkflowError, "workflow_parse_error"):
            parse_workflow("---\ntracker: [broken\n---\nbody")

    def test_reloader_keeps_last_known_good_config_after_invalid_reload(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workflow_path = Path(temp_dir) / "WORKFLOW.md"
            workflow_path.write_text("---\ntracker:\n  kind: linear\n---\nFirst {{ issue.identifier }}\n", encoding="utf-8")
            reloader = WorkflowReloader.for_path(workflow_path)

            first = reloader.load_initial()
            workflow_path.write_text("---\ntracker: [broken\n---\nSecond\n", encoding="utf-8")
            with self.assertLogs("jazzband.workflow", level="ERROR"):
                second = reloader.reload()

            self.assertIs(first, second)
            self.assertIsNotNone(reloader.last_error)

            workflow_path.write_text("---\ntracker:\n  kind: linear\n---\nSecond\n", encoding="utf-8")
            third = reloader.reload()

            self.assertEqual("Second", third.prompt_template)
            self.assertIsNone(reloader.last_error)

    def test_effective_reloader_keeps_last_known_good_config_after_schema_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workflow_path = Path(temp_dir) / "WORKFLOW.md"
            workflow_path.write_text(
                "---\ntracker:\n  kind: linear\npolling:\n  interval_ms: 1000\n---\nFirst\n",
                encoding="utf-8",
            )
            reloader = WorkflowReloader.for_path(workflow_path)
            first = reloader.load_initial_effective()

            workflow_path.write_text(
                "---\ntracker:\n  kind: linear\nagent:\n  max_turns: 0\n---\nSecond\n",
                encoding="utf-8",
            )

            with self.assertLogs("jazzband.workflow", level="ERROR") as logs:
                second = reloader.reload_effective()

            self.assertIs(first, second)
            self.assertIsInstance(reloader.last_error, ConfigError)
            self.assertIn("Rejected WORKFLOW.md reload", logs.output[0])
            self.assertEqual(1000, second.config.polling.interval_ms)
            self.assertEqual("First", second.definition.prompt_template)

            workflow_path.write_text(
                "---\ntracker:\n  kind: linear\npolling:\n  interval_ms: 2000\n---\nThird\n",
                encoding="utf-8",
            )
            third = reloader.reload_effective()

            self.assertEqual(2000, third.config.polling.interval_ms)
            self.assertEqual("Third", third.definition.prompt_template)
            self.assertIsNone(reloader.last_error)


class WorkflowWatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_watch_workflow_detects_file_change_within_one_second(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workflow_path = Path(temp_dir) / "WORKFLOW.md"
            workflow_path.write_text("---\ntracker:\n  kind: linear\n---\nFirst\n", encoding="utf-8")

            watcher = watch_workflow(workflow_path)

            async def wait_for_change():
                async for changes in watcher:
                    return changes
                return None

            task = asyncio.create_task(wait_for_change())
            await asyncio.sleep(0.1)
            workflow_path.write_text("---\ntracker:\n  kind: linear\n---\nSecond\n", encoding="utf-8")

            try:
                changes = await asyncio.wait_for(task, timeout=1)
            finally:
                await watcher.aclose()

            self.assertIsNotNone(changes)


if __name__ == "__main__":
    unittest.main()
