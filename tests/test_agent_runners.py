import tempfile
import unittest
from pathlib import Path

from jazzband.agents.base import (
    APIAgentRunner,
    AgentEvent,
    AgentEventCallback,
    AgentEventType,
    AgentRunner,
    AgentRunnerError,
    AgentSession,
    BaseRunner,
    CLIAgentRunner,
    TaskResult,
    TokenUsage,
    TurnResult,
)
from jazzband.tracker.models import Issue


def issue() -> Issue:
    return Issue(
        id="issue-1",
        identifier="IN-171",
        title="Agent runner contracts",
        description=None,
        priority=1,
        state="Todo",
        branch_name=None,
        url=None,
    )


class DummyCLIRunner(CLIAgentRunner):
    name = "dummy_cli"

    async def start_session(self, workspace: Path, *, worker_host: str | None = None) -> AgentSession:
        return AgentSession(id="session-1", workspace=workspace, worker_host=worker_host, process_id=123)

    async def run_turn(
        self,
        session: AgentSession,
        prompt: str,
        issue: Issue,
        on_event: AgentEventCallback,
    ) -> TurnResult:
        await on_event(
            AgentEvent(
                type=AgentEventType.TURN_COMPLETED,
                message=prompt,
                issue_id=issue.id,
                issue_identifier=issue.identifier,
                session_id=session.id,
            )
        )
        return TurnResult(
            success=True,
            exit_reason="turn_completed",
            usage=TokenUsage.from_input_output(10, 5),
        )

    async def stop_session(self, session: AgentSession) -> None:
        return None


class DummyAPIRunner(APIAgentRunner):
    name = "dummy_api"

    async def run_task(
        self,
        workspace: Path,
        prompt: str,
        issue: Issue,
        on_event: AgentEventCallback,
    ) -> TaskResult:
        await on_event(AgentEvent(type=AgentEventType.TASK_COMPLETED, issue_identifier=issue.identifier))
        return TaskResult(
            success=True,
            exit_reason="task_completed",
            output_paths=(workspace / "artifact.txt",),
            usage=TokenUsage(input_tokens=3, output_tokens=4, total_tokens=7),
        )


class AgentRunnerTests(unittest.IsolatedAsyncioTestCase):
    def test_base_runner_is_shared_by_session_and_api_runners(self):
        self.assertIsInstance(DummyCLIRunner("codex app-server"), BaseRunner)
        self.assertIsInstance(DummyAPIRunner(), BaseRunner)

    def test_agent_runner_contract_is_abstract(self):
        with self.assertRaises(TypeError):
            AgentRunner()  # type: ignore[abstract]

    async def test_cli_runner_normalizes_command_and_runs_turn_callback(self):
        events: list[AgentEvent] = []

        async def on_event(event: AgentEvent) -> None:
            events.append(event)

        with tempfile.TemporaryDirectory() as temp_dir:
            runner = DummyCLIRunner('codex app-server --profile "jazzband dev"')
            session = await runner.start_session(Path(temp_dir), worker_host="local")
            result = await runner.run_turn(session, "Continue", issue(), on_event)
            await runner.stop_session(session)

        self.assertEqual(("codex", "app-server", "--profile", "jazzband dev"), runner.command)
        self.assertEqual(Path(session.workspace), session.workspace.resolve())
        self.assertEqual("local", session.worker_host)
        self.assertTrue(result.success)
        self.assertEqual(TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15), result.usage)
        self.assertEqual([AgentEventType.TURN_COMPLETED], [event.type for event in events])
        self.assertEqual("session-1", events[0].session_id)

    def test_cli_runner_rejects_empty_command(self):
        with self.assertRaisesRegex(AgentRunnerError, "agent_command_required"):
            DummyCLIRunner("   ")

    async def test_api_runner_returns_normalized_artifact_paths(self):
        events: list[AgentEvent] = []

        async def on_event(event: AgentEvent) -> None:
            events.append(event)

        with tempfile.TemporaryDirectory() as temp_dir:
            result = await DummyAPIRunner().run_task(Path(temp_dir), "Generate", issue(), on_event)

        self.assertTrue(result.success)
        self.assertEqual("task_completed", result.exit_reason)
        self.assertEqual(1, len(result.output_paths))
        self.assertTrue(result.output_paths[0].is_absolute())
        self.assertEqual([AgentEventType.TASK_COMPLETED], [event.type for event in events])

    def test_agent_event_rejects_unknown_type(self):
        with self.assertRaises(ValueError):
            AgentEvent(type="turn_compelted")  # type: ignore[arg-type]

    def test_agent_session_normalizes_workspace_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session = AgentSession(id="session-1", workspace=Path(temp_dir) / ".." / Path(temp_dir).name)

        self.assertTrue(session.workspace.is_absolute())
        self.assertEqual(session.workspace, session.workspace.resolve())

    def test_token_usage_rejects_negative_values_and_merges(self):
        usage = TokenUsage.from_input_output(2, 3).merge(TokenUsage(input_tokens=1, output_tokens=4, total_tokens=5))

        self.assertEqual(TokenUsage(input_tokens=3, output_tokens=7, total_tokens=10), usage)
        with self.assertRaisesRegex(ValueError, "token_usage_must_be_non_negative"):
            TokenUsage(input_tokens=-1)
        with self.assertRaisesRegex(ValueError, "token_usage_total_mismatch"):
            TokenUsage(input_tokens=1, output_tokens=2, total_tokens=999)


if __name__ == "__main__":
    unittest.main()
