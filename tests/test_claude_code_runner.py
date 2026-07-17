from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from jazzband.agents.base import AgentEvent, AgentEventType, AgentRunnerError
from jazzband.agents.claude_code import ClaudeCodeRunner
from jazzband.tracker.models import Issue


def issue() -> Issue:
    return Issue(
        id="issue-1",
        identifier="IN-1",
        title="Test issue",
        description=None,
        priority=1,
        state="In Progress",
        branch_name=None,
        url=None,
    )


class FakeStreamReader:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()

    async def readline(self) -> bytes:
        return await self._queue.get()

    async def read(self) -> bytes:
        return b""

    def push_json(self, payload: dict[str, Any]) -> None:
        self._queue.put_nowait(json.dumps(payload).encode() + b"\n")

    def close(self) -> None:
        self._queue.put_nowait(b"")


class FakeStreamWriter:
    def __init__(self) -> None:
        self.written: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.written.append(data)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class FakeProcess:
    def __init__(self) -> None:
        self.stdout = FakeStreamReader()
        self.stderr = FakeStreamReader()
        self.stdin = FakeStreamWriter()
        self.pid = 9999
        self.returncode: int | None = None
        self.killed = False
        self.terminated = False

    def kill(self) -> None:
        self.killed = True
        self.returncode = 137
        self.stdout.close()
        self.stderr.close()

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 143
        self.stdout.close()
        self.stderr.close()

    async def wait(self) -> int:
        return self.returncode or 0


def _make_runner(**kwargs: Any) -> ClaudeCodeRunner:
    return ClaudeCodeRunner("claude", **kwargs)


class ClaudeCodeRunnerStartSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_session_returns_session_with_workspace(self):
        runner = _make_runner()
        with tempfile.TemporaryDirectory() as tmp:
            session = await runner.start_session(Path(tmp))
        self.assertEqual(Path(tmp).resolve(), session.workspace)
        self.assertIn("claude_state", session.metadata)

    async def test_start_session_rejects_missing_workspace(self):
        runner = _make_runner()
        with self.assertRaises(AgentRunnerError):
            await runner.start_session(Path("/nonexistent/path/xyz"))

    async def test_start_session_rejects_remote_worker(self):
        runner = _make_runner()
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(AgentRunnerError):
                await runner.start_session(Path(tmp), worker_host="remote")


class ClaudeCodeRunnerBuildCommandTests(unittest.TestCase):
    def test_minimal_command(self):
        runner = _make_runner()
        with tempfile.TemporaryDirectory() as tmp:
            cmd = runner._build_command(Path(tmp), session_id=None)
        self.assertIn("--print", cmd)
        self.assertIn("--output-format", cmd)
        self.assertIn("stream-json", cmd)
        self.assertIn("--permission-mode", cmd)
        self.assertIn("bypassPermissions", cmd)
        self.assertNotIn("--resume", cmd)
        self.assertNotIn("--model", cmd)

    def test_resume_included_when_session_id_set(self):
        runner = _make_runner()
        with tempfile.TemporaryDirectory() as tmp:
            cmd = runner._build_command(Path(tmp), session_id="sess-abc")
        idx = list(cmd).index("--resume")
        self.assertEqual("sess-abc", cmd[idx + 1])

    def test_model_included_when_configured(self):
        runner = _make_runner(model="claude-sonnet-4-6")
        with tempfile.TemporaryDirectory() as tmp:
            cmd = runner._build_command(Path(tmp), session_id=None)
        idx = list(cmd).index("--model")
        self.assertEqual("claude-sonnet-4-6", cmd[idx + 1])

    def test_linear_system_prompt_included_when_api_key_set(self):
        runner = _make_runner(linear_api_key="lin_key")
        with tempfile.TemporaryDirectory() as tmp:
            cmd = runner._build_command(Path(tmp), session_id=None)
        self.assertIn("--append-system-prompt", cmd)

    def test_no_linear_system_prompt_without_api_key(self):
        runner = _make_runner()
        with tempfile.TemporaryDirectory() as tmp:
            cmd = runner._build_command(Path(tmp), session_id=None)
        self.assertNotIn("--append-system-prompt", cmd)


class ClaudeCodeRunnerTurnTests(unittest.IsolatedAsyncioTestCase):
    async def _run_turn_with_process(
        self,
        process: FakeProcess,
        *,
        linear_api_key: str | None = None,
    ) -> tuple[list[AgentEvent], Any]:
        runner = _make_runner(linear_api_key=linear_api_key)
        events: list[AgentEvent] = []

        async def on_event(e: AgentEvent) -> None:
            events.append(e)

        async def fake_subprocess(*args: Any, **kwargs: Any) -> FakeProcess:
            return process

        with tempfile.TemporaryDirectory() as tmp:
            with patch("jazzband.agents.claude_code.asyncio.create_subprocess_exec", fake_subprocess):
                session = await runner.start_session(Path(tmp))
                result = await runner.run_turn(session, "Do the work.", issue(), on_event)

        return events, result

    async def test_successful_turn_emits_session_started_and_turn_completed(self):
        process = FakeProcess()
        process.stdout.push_json({"type": "system", "subtype": "init", "session_id": "sid-1"})
        process.stdout.push_json({
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "All done.",
            "session_id": "sid-1",
            "usage": {"input_tokens": 100, "output_tokens": 50},
        })
        process.stdout.close()

        events, result = await self._run_turn_with_process(process)

        self.assertTrue(result.success)
        self.assertEqual("turn_completed", result.exit_reason)
        self.assertIsNotNone(result.usage)
        self.assertEqual(100, result.usage.input_tokens)
        self.assertEqual(50, result.usage.output_tokens)
        event_types = [e.type for e in events]
        self.assertIn(AgentEventType.SESSION_STARTED, event_types)
        self.assertIn(AgentEventType.TURN_COMPLETED, event_types)

    async def test_error_result_returns_failed_turn(self):
        process = FakeProcess()
        process.stdout.push_json({"type": "system", "subtype": "init", "session_id": "sid-2"})
        process.stdout.push_json({
            "type": "result",
            "subtype": "error_during_turn",
            "is_error": True,
            "result": "Something went wrong.",
            "session_id": "sid-2",
        })
        process.stdout.close()

        events, result = await self._run_turn_with_process(process)

        self.assertFalse(result.success)
        self.assertEqual("error_during_turn", result.exit_reason)
        self.assertIn(AgentEventType.TURN_FAILED, [e.type for e in events])

    async def test_assistant_message_emits_notification(self):
        process = FakeProcess()
        process.stdout.push_json({"type": "system", "subtype": "init", "session_id": "sid-3"})
        process.stdout.push_json({
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": "Working on it..."}],
            },
        })
        process.stdout.push_json({
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "Done.",
            "session_id": "sid-3",
        })
        process.stdout.close()

        events, result = await self._run_turn_with_process(process)

        notification_events = [e for e in events if e.type == AgentEventType.NOTIFICATION]
        self.assertEqual(1, len(notification_events))
        self.assertIn("Working on it...", notification_events[0].message)

    async def test_session_id_stored_after_init_event(self):
        runner = _make_runner()
        process = FakeProcess()
        process.stdout.push_json({"type": "system", "subtype": "init", "session_id": "stored-sid"})
        process.stdout.push_json({
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "Done.",
            "session_id": "stored-sid",
        })
        process.stdout.close()

        async def fake_subprocess(*args: Any, **kwargs: Any) -> FakeProcess:
            return process

        with tempfile.TemporaryDirectory() as tmp:
            with patch("jazzband.agents.claude_code.asyncio.create_subprocess_exec", fake_subprocess):
                session = await runner.start_session(Path(tmp))
                await runner.run_turn(session, "prompt", issue(), lambda e: asyncio.sleep(0))
                state = session.metadata["claude_state"]

        self.assertEqual("stored-sid", state.session_id)

    async def test_malformed_json_lines_are_skipped(self):
        process = FakeProcess()
        process.stdout.push_json({"type": "system", "subtype": "init", "session_id": "sid-4"})
        process.stdout._queue.put_nowait(b"not valid json\n")
        process.stdout.push_json({
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "Done.",
            "session_id": "sid-4",
        })
        process.stdout.close()

        events, result = await self._run_turn_with_process(process)

        self.assertTrue(result.success)

    async def test_process_exits_without_result_event(self):
        process = FakeProcess()
        process.stdout.push_json({"type": "system", "subtype": "init", "session_id": "sid-5"})
        process.stdout.close()

        events, result = await self._run_turn_with_process(process)

        self.assertTrue(result.success)  # returncode defaults to 0
        self.assertIn("claude_exited", result.exit_reason)

    async def test_linear_api_key_injected_into_env(self):
        captured_env: dict[str, str] = {}

        async def fake_subprocess(*args: Any, **kwargs: Any) -> FakeProcess:
            captured_env.update(kwargs.get("env", {}))
            p = FakeProcess()
            p.stdout.push_json({"type": "result", "subtype": "success", "is_error": False, "result": ""})
            p.stdout.close()
            return p

        runner = _make_runner(linear_api_key="lin_test_key")
        with tempfile.TemporaryDirectory() as tmp:
            with patch("jazzband.agents.claude_code.asyncio.create_subprocess_exec", fake_subprocess):
                session = await runner.start_session(Path(tmp))
                await runner.run_turn(session, "prompt", issue(), lambda e: asyncio.sleep(0))

        self.assertEqual("lin_test_key", captured_env.get("LINEAR_API_KEY"))

    async def test_stop_session_is_noop(self):
        runner = _make_runner()
        with tempfile.TemporaryDirectory() as tmp:
            session = await runner.start_session(Path(tmp))
            await runner.stop_session(session)  # should not raise


class ClaudeCodeRunnerTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_kills_process_and_returns_failure(self):
        process = FakeProcess()
        # never push a result — process hangs

        async def fake_subprocess(*args: Any, **kwargs: Any) -> FakeProcess:
            return process

        runner = ClaudeCodeRunner("claude", turn_timeout_ms=10)

        async def on_event(e: AgentEvent) -> None:
            pass

        with tempfile.TemporaryDirectory() as tmp:
            with patch("jazzband.agents.claude_code.asyncio.create_subprocess_exec", fake_subprocess):
                session = await runner.start_session(Path(tmp))
                result = await runner.run_turn(session, "prompt", issue(), on_event)

        self.assertFalse(result.success)
        self.assertEqual("turn_timeout", result.exit_reason)
        self.assertTrue(process.killed)


class ClaudeCodeRunnerToolRoutingTests(unittest.IsolatedAsyncioTestCase):
    """Phase 4: tool_use / tool_result routing + stop_reason mapping."""

    async def _run_with(self, events_in_order: list[dict[str, Any]]) -> tuple[list[AgentEvent], Any]:
        process = FakeProcess()
        for payload in events_in_order:
            process.stdout.push_json(payload)
        process.stdout.close()
        runner = ClaudeCodeRunner("claude")
        captured: list[AgentEvent] = []

        async def on_event(e: AgentEvent) -> None:
            captured.append(e)

        async def fake_subprocess(*args: Any, **kwargs: Any) -> FakeProcess:
            return process

        with tempfile.TemporaryDirectory() as tmp:
            with patch("jazzband.agents.claude_code.asyncio.create_subprocess_exec", fake_subprocess):
                session = await runner.start_session(Path(tmp))
                result = await runner.run_turn(session, "prompt", issue(), on_event)
        return captured, result

    async def test_tool_use_emits_notification_with_summary(self):
        events, _ = await self._run_with([
            {"type": "system", "subtype": "init", "session_id": "sid"},
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "I'll check the file."},
                        {
                            "type": "tool_use",
                            "id": "toolu_a",
                            "name": "Read",
                            "input": {"file_path": "/path/to/file.py"},
                        },
                    ]
                },
            },
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "ok",
                "session_id": "sid",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        ])

        tool_events = [e for e in events if e.data.get("event") == "tool_use"]
        self.assertEqual(1, len(tool_events))
        self.assertEqual("Read", tool_events[0].data["tool_name"])
        self.assertIn("/path/to/file.py", tool_events[0].message)
        self.assertEqual("toolu_a", tool_events[0].data["tool_use_id"])

    async def test_tool_result_error_is_surfaced(self):
        events, _ = await self._run_with([
            {"type": "system", "subtype": "init", "session_id": "sid"},
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_a",
                            "is_error": True,
                            "content": "permission denied",
                        }
                    ]
                },
            },
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "ok",
                "session_id": "sid",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        ])

        error_events = [e for e in events if e.data.get("event") == "tool_result_error"]
        self.assertEqual(1, len(error_events))
        self.assertIn("permission denied", error_events[0].message)
        self.assertEqual("toolu_a", error_events[0].data["tool_use_id"])

    async def test_tool_result_success_is_not_logged(self):
        # Successful tool results are noisy; only errors should surface.
        events, _ = await self._run_with([
            {"type": "system", "subtype": "init", "session_id": "sid"},
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_a",
                            "is_error": False,
                            "content": "file contents …",
                        }
                    ]
                },
            },
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "ok",
                "session_id": "sid",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        ])

        error_events = [e for e in events if e.data.get("event") == "tool_result_error"]
        self.assertEqual([], error_events)

    async def test_max_tokens_stop_reason_maps_to_failure(self):
        _, result = await self._run_with([
            {"type": "system", "subtype": "init", "session_id": "sid"},
            {
                "type": "result",
                "subtype": "success",
                "stop_reason": "max_tokens",
                "is_error": False,
                "result": "truncated",
                "session_id": "sid",
                "usage": {"input_tokens": 100, "output_tokens": 4096},
            },
        ])

        self.assertFalse(result.success)
        self.assertEqual("max_tokens", result.exit_reason)

    async def test_cache_token_usage_attached_to_result_event(self):
        events, _ = await self._run_with([
            {"type": "system", "subtype": "init", "session_id": "sid"},
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "ok",
                "session_id": "sid",
                "usage": {
                    "input_tokens": 50,
                    "output_tokens": 25,
                    "cache_creation_input_tokens": 2000,
                    "cache_read_input_tokens": 1500,
                },
            },
        ])

        completed = [e for e in events if e.type == AgentEventType.TURN_COMPLETED]
        self.assertEqual(1, len(completed))
        self.assertEqual(
            {"creation": 2000, "read": 1500},
            completed[0].data["cache_tokens"],
        )


if __name__ == "__main__":
    unittest.main()
