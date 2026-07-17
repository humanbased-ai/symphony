from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from jazzband.agents.base import AgentEvent, AgentEventType, AgentRunnerError
from jazzband.agents.codex import CodexRunner, NON_INTERACTIVE_TOOL_INPUT_ANSWER
from jazzband.tracker.models import Issue


def issue() -> Issue:
    return Issue(
        id="issue-175",
        identifier="IN-175",
        title="Codex runner",
        description=None,
        priority=1,
        state="In Progress",
        branch_name=None,
        url=None,
    )


class FakeStdout:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()

    async def readline(self) -> bytes:
        return await self._queue.get()

    def push_json(self, payload: dict[str, Any]) -> None:
        self._queue.put_nowait(json.dumps(payload).encode("utf-8") + b"\n")

    def push_line(self, line: str) -> None:
        self._queue.put_nowait(line.encode("utf-8") + b"\n")

    def close(self) -> None:
        self._queue.put_nowait(b"")


class FakeStdin:
    def __init__(self, on_payload) -> None:
        self.payloads: list[dict[str, Any]] = []
        self._on_payload = on_payload

    def write(self, data: bytes) -> None:
        payload = json.loads(data.decode("utf-8"))
        self.payloads.append(payload)
        self._on_payload(payload)

    async def drain(self) -> None:
        return None


class FakeProcess:
    def __init__(self, on_payload) -> None:
        self.stdout = FakeStdout()
        self.stderr = FakeStdout()
        self.stdin = FakeStdin(on_payload)
        self.pid = 4242
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 143
        self.stdout.close()
        self.stderr.close()

    def kill(self) -> None:
        self.killed = True
        self.returncode = 137
        self.stdout.close()
        self.stderr.close()

    async def wait(self) -> int:
        while self.returncode is None:
            await asyncio.sleep(0)
        return self.returncode


class FakeProcessFactory:
    def __init__(self, handler) -> None:
        self.handler = handler
        self.calls: list[dict[str, Any]] = []
        self.process: FakeProcess | None = None

    async def __call__(self, *argv, **kwargs) -> FakeProcess:
        self.calls.append({"argv": argv, "kwargs": kwargs})
        self.process = FakeProcess(self.handler)
        return self.process


class CodexRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        await asyncio.sleep(0)

    async def test_starts_app_server_session_and_sends_expected_startup_messages(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            factory = FakeProcessFactory(lambda payload: self._startup_responder(factory.process, payload))
            runner = CodexRunner("codex app-server --profile test", process_factory=factory)

            session = await runner.start_session(Path(temp_dir))

            self.assertEqual("thread-175", session.id)
            self.assertEqual(4242, session.process_id)
            self.assertEqual(("bash", "-lc", "codex app-server --profile test"), factory.calls[0]["argv"])
            self.assertEqual(str(Path(temp_dir).resolve()), factory.calls[0]["kwargs"]["cwd"])

            sent = factory.process.stdin.payloads
            self.assertEqual("initialize", sent[0]["method"])
            self.assertTrue(sent[0]["params"]["capabilities"]["experimentalApi"])
            self.assertEqual("initialized", sent[1]["method"])
            self.assertEqual("thread/start", sent[2]["method"])
            self.assertEqual(str(Path(temp_dir).resolve()), sent[2]["params"]["cwd"])
            self.assertEqual("linear_graphql", sent[2]["params"]["dynamicTools"][0]["name"])

            await runner.stop_session(session)

    async def test_run_turn_emits_session_and_completion_events(self):
        events: list[AgentEvent] = []

        async def on_event(event: AgentEvent) -> None:
            events.append(event)

        with tempfile.TemporaryDirectory() as temp_dir:
            factory = FakeProcessFactory(lambda payload: self._startup_and_turn_responder(factory.process, payload))
            runner = CodexRunner(process_factory=factory)
            session = await runner.start_session(Path(temp_dir))

            result = await runner.run_turn(session, "Build the runner", issue(), on_event)

            self.assertTrue(result.success)
            self.assertEqual("turn_completed", result.exit_reason)
            self.assertEqual(["session_started", "turn_completed"], [event.type.value for event in events])
            self.assertEqual("thread-175-turn-175", events[0].session_id)
            turn_start = factory.process.stdin.payloads[3]
            self.assertEqual("turn/start", turn_start["method"])
            self.assertEqual("Build the runner", turn_start["params"]["input"][0]["text"])
            self.assertEqual("IN-175: Codex runner", turn_start["params"]["title"])

            await runner.stop_session(session)

    async def test_tool_call_routes_to_injected_executor_and_returns_normalized_result(self):
        events: list[AgentEvent] = []
        tool_calls: list[tuple[str | None, Any]] = []

        async def on_event(event: AgentEvent) -> None:
            events.append(event)

        def tool_executor(tool_name: str | None, arguments: Any) -> dict[str, Any]:
            tool_calls.append((tool_name, arguments))
            return {"success": True, "response": {"data": {"viewer": {"id": "usr_1"}}}}

        def handler(process: FakeProcess | None, payload: dict[str, Any]) -> None:
            self._startup_responder(process, payload)
            if payload.get("method") == "turn/start":
                process.stdout.push_json({"id": payload["id"], "result": {"turn": {"id": "turn-tool"}}})
                process.stdout.push_json(
                    {
                        "id": 101,
                        "method": "item/tool/call",
                        "params": {
                            "name": "linear_graphql",
                            "arguments": {"query": "query Viewer { viewer { id } }"},
                        },
                    }
                )
                process.stdout.push_json({"method": "turn/completed"})

        with tempfile.TemporaryDirectory() as temp_dir:
            factory = FakeProcessFactory(lambda payload: handler(factory.process, payload))
            runner = CodexRunner(process_factory=factory, tool_executor=tool_executor)
            session = await runner.start_session(Path(temp_dir))

            result = await runner.run_turn(session, "Use Linear", issue(), on_event)

            self.assertTrue(result.success)
            self.assertEqual([("linear_graphql", {"query": "query Viewer { viewer { id } }"})], tool_calls)
            tool_response = factory.process.stdin.payloads[4]
            self.assertEqual(101, tool_response["id"])
            self.assertTrue(tool_response["result"]["success"])
            self.assertIn('"viewer"', tool_response["result"]["output"])
            self.assertEqual("tool_call_completed", events[1].data["event"])

            await runner.stop_session(session)

    async def test_unsupported_tool_call_returns_failure_without_stalling(self):
        events: list[AgentEvent] = []

        async def on_event(event: AgentEvent) -> None:
            events.append(event)

        def handler(process: FakeProcess | None, payload: dict[str, Any]) -> None:
            self._startup_responder(process, payload)
            if payload.get("method") == "turn/start":
                process.stdout.push_json({"id": payload["id"], "result": {"turn": {"id": "turn-unsupported"}}})
                process.stdout.push_json(
                    {"id": 102, "method": "item/tool/call", "params": {"tool": "unknown_tool", "arguments": {}}}
                )
                process.stdout.push_json({"method": "turn/completed"})

        with tempfile.TemporaryDirectory() as temp_dir:
            factory = FakeProcessFactory(lambda payload: handler(factory.process, payload))
            runner = CodexRunner(process_factory=factory)
            session = await runner.start_session(Path(temp_dir))

            result = await runner.run_turn(session, "Use unsupported tool", issue(), on_event)

            self.assertTrue(result.success)
            tool_response = factory.process.stdin.payloads[4]
            self.assertFalse(tool_response["result"]["success"])
            self.assertEqual("unsupported_tool_call", events[1].data["event"])

            await runner.stop_session(session)

    async def test_approval_requests_fail_unless_policy_is_never(self):
        events: list[AgentEvent] = []

        async def on_event(event: AgentEvent) -> None:
            events.append(event)

        def handler(process: FakeProcess | None, payload: dict[str, Any]) -> None:
            self._startup_responder(process, payload)
            if payload.get("method") == "turn/start":
                process.stdout.push_json({"id": payload["id"], "result": {"turn": {"id": "turn-approval"}}})
                process.stdout.push_json(
                    {
                        "id": 201,
                        "method": "item/commandExecution/requestApproval",
                        "params": {"command": "gh pr view"},
                    }
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            factory = FakeProcessFactory(lambda payload: handler(factory.process, payload))
            runner = CodexRunner(process_factory=factory)
            session = await runner.start_session(Path(temp_dir))

            result = await runner.run_turn(session, "Need approval", issue(), on_event)

            self.assertFalse(result.success)
            self.assertEqual("approval_required", result.exit_reason)
            self.assertEqual("approval_required", events[1].data["event"])

            await runner.stop_session(session)

    async def test_approval_requests_auto_approve_when_policy_is_never(self):
        events: list[AgentEvent] = []

        async def on_event(event: AgentEvent) -> None:
            events.append(event)

        def handler(process: FakeProcess | None, payload: dict[str, Any]) -> None:
            self._startup_responder(process, payload)
            if payload.get("method") == "turn/start":
                process.stdout.push_json({"id": payload["id"], "result": {"turn": {"id": "turn-approval"}}})
                process.stdout.push_json(
                    {
                        "id": 202,
                        "method": "item/fileChange/requestApproval",
                        "params": {"path": "jazzband/agents/codex.py"},
                    }
                )
            elif payload.get("id") == 202:
                process.stdout.push_json({"method": "turn/completed"})

        with tempfile.TemporaryDirectory() as temp_dir:
            factory = FakeProcessFactory(lambda payload: handler(factory.process, payload))
            runner = CodexRunner(process_factory=factory, approval_policy="never")
            session = await runner.start_session(Path(temp_dir))

            result = await runner.run_turn(session, "Auto approve", issue(), on_event)

            self.assertTrue(result.success)
            approval_response = factory.process.stdin.payloads[4]
            self.assertEqual({"decision": "acceptForSession"}, approval_response["result"])
            self.assertEqual("approval_auto_approved", events[1].data["event"])

            await runner.stop_session(session)

    async def test_tool_input_request_gets_non_interactive_answer(self):
        events: list[AgentEvent] = []

        async def on_event(event: AgentEvent) -> None:
            events.append(event)

        def handler(process: FakeProcess | None, payload: dict[str, Any]) -> None:
            self._startup_responder(process, payload)
            if payload.get("method") == "turn/start":
                process.stdout.push_json({"id": payload["id"], "result": {"turn": {"id": "turn-input"}}})
                process.stdout.push_json(
                    {
                        "id": 301,
                        "method": "item/tool/requestUserInput",
                        "params": {"questions": [{"id": "freeform", "question": "What should I say?"}]},
                    }
                )
            elif payload.get("id") == 301:
                process.stdout.push_json({"method": "turn/completed"})

        with tempfile.TemporaryDirectory() as temp_dir:
            factory = FakeProcessFactory(lambda payload: handler(factory.process, payload))
            runner = CodexRunner(process_factory=factory)
            session = await runner.start_session(Path(temp_dir))

            result = await runner.run_turn(session, "Handle input prompt", issue(), on_event)

            self.assertTrue(result.success)
            input_response = factory.process.stdin.payloads[4]
            self.assertEqual(
                [NON_INTERACTIVE_TOOL_INPUT_ANSWER],
                input_response["result"]["answers"]["freeform"]["answers"],
            )
            self.assertEqual("tool_input_auto_answered", events[1].data["event"])

            await runner.stop_session(session)

    async def test_malformed_json_like_turn_line_emits_malformed_and_continues(self):
        events: list[AgentEvent] = []

        async def on_event(event: AgentEvent) -> None:
            events.append(event)

        def handler(process: FakeProcess | None, payload: dict[str, Any]) -> None:
            self._startup_responder(process, payload)
            if payload.get("method") == "turn/start":
                process.stdout.push_json({"id": payload["id"], "result": {"turn": {"id": "turn-malformed"}}})
                process.stdout.push_line('{"method":"turn/completed"')
                process.stdout.push_json({"method": "turn/completed"})

        with tempfile.TemporaryDirectory() as temp_dir:
            factory = FakeProcessFactory(lambda payload: handler(factory.process, payload))
            runner = CodexRunner(process_factory=factory)
            session = await runner.start_session(Path(temp_dir))

            result = await runner.run_turn(session, "Malformed frame", issue(), on_event)

            self.assertTrue(result.success)
            self.assertEqual([AgentEventType.SESSION_STARTED, AgentEventType.MALFORMED, AgentEventType.TURN_COMPLETED], [event.type for event in events])

            await runner.stop_session(session)

    async def test_response_timeout_maps_to_startup_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            factory = FakeProcessFactory(lambda _payload: None)
            runner = CodexRunner(process_factory=factory, read_timeout_ms=1)

            with self.assertRaisesRegex(AgentRunnerError, "response_timeout"):
                await runner.start_session(Path(temp_dir))

    def _startup_responder(self, process: FakeProcess | None, payload: dict[str, Any]) -> None:
        if process is None:
            return
        if payload.get("method") == "initialize":
            process.stdout.push_json({"id": payload["id"], "result": {}})
        elif payload.get("method") == "thread/start":
            process.stdout.push_json({"id": payload["id"], "result": {"thread": {"id": "thread-175"}}})

    def _startup_and_turn_responder(self, process: FakeProcess | None, payload: dict[str, Any]) -> None:
        self._startup_responder(process, payload)
        if process is not None and payload.get("method") == "turn/start":
            process.stdout.push_json({"id": payload["id"], "result": {"turn": {"id": "turn-175"}}})
            process.stdout.push_json({"method": "turn/completed", "usage": {"inputTokens": 2, "outputTokens": 3}})


if __name__ == "__main__":
    unittest.main()
