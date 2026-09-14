from __future__ import annotations

import asyncio
import json
import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from jazzband.agents.base import (
    AgentEvent,
    AgentEventCallback,
    AgentEventType,
    AgentRunnerError,
    AgentSession,
    CLIAgentRunner,
    TokenUsage,
    TurnResult,
)
from jazzband.tools.linear_graphql import LINEAR_GRAPHQL_TOOL_NAME, LinearGraphQLTool
from jazzband.tracker.linear import LinearClient
from jazzband.tracker.models import Issue


DEFAULT_CODEX_COMMAND = "codex app-server"
DEFAULT_READ_TIMEOUT_MS = 5_000
DEFAULT_TURN_TIMEOUT_MS = 3_600_000
DEFAULT_APPROVAL_POLICY = "on-request"
DEFAULT_THREAD_SANDBOX = "workspace-write"
NON_INTERACTIVE_TOOL_INPUT_ANSWER = "This is a non-interactive session. Operator input is unavailable."

INITIALIZE_REQUEST_ID = 1
THREAD_START_REQUEST_ID = 2
FIRST_TURN_REQUEST_ID = 3


DynamicToolExecutor = Callable[[str | None, Any], dict[str, Any]]
SubprocessFactory = Callable[..., Awaitable["CodexProcess"]]


class CodexStreamWriter(Protocol):
    def write(self, data: bytes) -> None: ...
    async def drain(self) -> None: ...


class CodexStreamReader(Protocol):
    async def readline(self) -> bytes: ...


class CodexProcess(Protocol):
    stdin: CodexStreamWriter | None
    stdout: CodexStreamReader | None
    stderr: CodexStreamReader | None
    pid: int | None
    returncode: int | None

    def terminate(self) -> None: ...
    def kill(self) -> None: ...
    async def wait(self) -> int: ...


@dataclass
class _CodexSessionState:
    process: CodexProcess
    thread_id: str
    stderr_task: asyncio.Task[None] | None = None
    next_request_id: int = FIRST_TURN_REQUEST_ID


class CodexRunner(CLIAgentRunner):
    """Codex app-server JSON-RPC runner over subprocess stdio."""

    name = "codex"

    def __init__(
        self,
        command: str | tuple[str, ...] | list[str] = DEFAULT_CODEX_COMMAND,
        *,
        approval_policy: str | dict[str, Any] = DEFAULT_APPROVAL_POLICY,
        thread_sandbox: str | dict[str, Any] = DEFAULT_THREAD_SANDBOX,
        turn_sandbox_policy: dict[str, Any] | None = None,
        read_timeout_ms: int = DEFAULT_READ_TIMEOUT_MS,
        turn_timeout_ms: int = DEFAULT_TURN_TIMEOUT_MS,
        linear_client: LinearClient | None = None,
        tool_executor: DynamicToolExecutor | None = None,
        process_factory: SubprocessFactory | None = None,
    ) -> None:
        super().__init__(command)
        self.command_string = command if isinstance(command, str) else shlex.join(tuple(command))
        self.approval_policy = approval_policy
        self.thread_sandbox = thread_sandbox
        self.turn_sandbox_policy = turn_sandbox_policy or _default_turn_sandbox_policy()
        self.read_timeout_ms = _positive_timeout(read_timeout_ms, "read_timeout_ms")
        self.turn_timeout_ms = _positive_timeout(turn_timeout_ms, "turn_timeout_ms")
        self.linear_client = linear_client
        self.tool_executor = tool_executor
        self.process_factory = process_factory or asyncio.create_subprocess_exec

    async def start_session(
        self,
        workspace: Path,
        *,
        worker_host: str | None = None,
    ) -> AgentSession:
        if worker_host is not None:
            raise AgentRunnerError("remote_codex_worker_not_supported")

        workspace = Path(workspace).expanduser().resolve()
        if not workspace.exists() or not workspace.is_dir():
            raise AgentRunnerError("invalid_workspace_cwd")

        process = await self._start_process(workspace)
        stderr_task = _start_stderr_drain(process)

        try:
            await self._send_request(
                process,
                {
                    "id": INITIALIZE_REQUEST_ID,
                    "method": "initialize",
                    "params": {
                        "capabilities": {"experimentalApi": True},
                        "clientInfo": {
                            "name": "jazzband-orchestrator",
                            "title": "Jazzband Orchestrator",
                            "version": "0.1.0",
                        },
                    },
                },
            )
            await self._await_response(process, INITIALIZE_REQUEST_ID, timeout_ms=self.read_timeout_ms)
            await self._send_request(process, {"method": "initialized", "params": {}})

            await self._send_request(
                process,
                {
                    "id": THREAD_START_REQUEST_ID,
                    "method": "thread/start",
                    "params": {
                        "approvalPolicy": self.approval_policy,
                        "sandbox": self.thread_sandbox,
                        "cwd": str(workspace),
                        "dynamicTools": [_linear_graphql_tool_spec()],
                    },
                },
            )
            thread_response = await self._await_response(
                process,
                THREAD_START_REQUEST_ID,
                timeout_ms=self.read_timeout_ms,
            )
            thread_id = _extract_thread_id(thread_response)
        except Exception:
            await self._stop_process(process, stderr_task)
            raise

        state = _CodexSessionState(process=process, thread_id=thread_id, stderr_task=stderr_task)
        return AgentSession(
            id=thread_id,
            workspace=workspace,
            process_id=process.pid,
            metadata={"thread_id": thread_id, "codex_state": state},
        )

    async def run_turn(
        self,
        session: AgentSession,
        prompt: str,
        issue: Issue,
        on_event: AgentEventCallback,
    ) -> TurnResult:
        state = _session_state(session)
        process = state.process
        turn_request_id = state.next_request_id
        state.next_request_id += 1

        await self._send_request(
            process,
            {
                "id": turn_request_id,
                "method": "turn/start",
                "params": {
                    "threadId": state.thread_id,
                    "input": [{"type": "text", "text": prompt}],
                    "cwd": str(session.workspace),
                    "title": f"{issue.identifier}: {issue.title}",
                    "approvalPolicy": self.approval_policy,
                    "sandboxPolicy": self.turn_sandbox_policy,
                },
            },
        )

        try:
            turn_response = await self._await_response(process, turn_request_id, timeout_ms=self.read_timeout_ms)
            turn_id = _extract_turn_id(turn_response)
        except AgentRunnerError as exc:
            await _emit(
                on_event,
                AgentEventType.ERROR,
                issue,
                session.id,
                message=str(exc),
                data={"event": "startup_failed", "reason": str(exc)},
            )
            return TurnResult(success=False, exit_reason=str(exc))

        session_id = f"{state.thread_id}-{turn_id}"
        await _emit(
            on_event,
            AgentEventType.SESSION_STARTED,
            issue,
            session_id,
            message="Codex session started.",
            data={"event": "session_started", "thread_id": state.thread_id, "turn_id": turn_id},
        )

        return await self._await_turn_completion(process, issue, session_id, on_event)

    async def stop_session(self, session: AgentSession) -> None:
        state = _session_state(session)
        await self._stop_process(state.process, state.stderr_task)

    async def _start_process(self, workspace: Path) -> CodexProcess:
        try:
            return await self.process_factory(
                "bash",
                "-lc",
                self.command_string,
                cwd=str(workspace),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=4 * 1024 * 1024,  # 4 MB; default 64 KB triggers LimitOverrunError on large JSON lines
            )
        except FileNotFoundError as exc:
            raise AgentRunnerError("codex_not_found") from exc
        except OSError as exc:
            raise AgentRunnerError(f"codex_launch_failed:{exc}") from exc

    async def _send_request(self, process: CodexProcess, payload: dict[str, Any]) -> None:
        if process.stdin is None:
            raise AgentRunnerError("codex_stdin_unavailable")

        process.stdin.write(json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n")
        await process.stdin.drain()

    async def _await_response(
        self,
        process: CodexProcess,
        request_id: int,
        *,
        timeout_ms: int,
    ) -> dict[str, Any]:
        while True:
            payload, raw = await self._read_payload(process, timeout_ms=timeout_ms)
            if payload is None:
                continue
            if payload.get("id") != request_id:
                continue
            if "error" in payload:
                raise AgentRunnerError(f"response_error:{payload['error']}")
            if "result" not in payload or not isinstance(payload["result"], dict):
                raise AgentRunnerError(f"response_error:{payload}")
            return payload["result"]

    async def _await_turn_completion(
        self,
        process: CodexProcess,
        issue: Issue,
        session_id: str,
        on_event: AgentEventCallback,
    ) -> TurnResult:
        usage: TokenUsage | None = None

        while True:
            try:
                payload, raw = await self._read_payload(process, timeout_ms=self.turn_timeout_ms)
            except AgentRunnerError as exc:
                exit_reason = str(exc)
                await _emit(
                    on_event,
                    AgentEventType.TURN_FAILED,
                    issue,
                    session_id,
                    message=exit_reason,
                    data={"event": exit_reason},
                )
                return TurnResult(success=False, exit_reason=exit_reason, usage=usage)

            if payload is None:
                if _protocol_message_candidate(raw):
                    await _emit(
                        on_event,
                        AgentEventType.MALFORMED,
                        issue,
                        session_id,
                        message="Malformed Codex JSON-RPC frame.",
                        data={"event": "malformed", "raw": raw},
                    )
                continue

            usage = _merge_usage(usage, _usage_from_payload(payload))
            method = payload.get("method")

            if method == "turn/completed":
                await _emit_turn_payload(on_event, AgentEventType.TURN_COMPLETED, issue, session_id, payload, raw)
                return TurnResult(
                    success=True,
                    exit_reason="turn_completed",
                    usage=usage,
                    metadata={"session_id": session_id, "payload": payload},
                )

            if method == "turn/failed":
                await _emit_turn_payload(on_event, AgentEventType.TURN_FAILED, issue, session_id, payload, raw)
                return TurnResult(
                    success=False,
                    exit_reason="turn_failed",
                    usage=usage,
                    metadata={"session_id": session_id, "payload": payload},
                )

            if method == "turn/cancelled":
                await _emit_turn_payload(on_event, AgentEventType.TURN_FAILED, issue, session_id, payload, raw)
                return TurnResult(
                    success=False,
                    exit_reason="turn_cancelled",
                    usage=usage,
                    metadata={"session_id": session_id, "payload": payload},
                )

            handled = await self._handle_turn_method(process, payload, raw, issue, session_id, on_event)
            if handled == "continue":
                continue
            if handled is not None:
                return TurnResult(
                    success=False,
                    exit_reason=handled,
                    usage=usage,
                    metadata={"session_id": session_id, "payload": payload},
                )

            await _emit_turn_payload(on_event, AgentEventType.NOTIFICATION, issue, session_id, payload, raw)

    async def _handle_turn_method(
        self,
        process: CodexProcess,
        payload: dict[str, Any],
        raw: str,
        issue: Issue,
        session_id: str,
        on_event: AgentEventCallback,
    ) -> str | None:
        method = payload.get("method")

        if method == "item/tool/call":
            await self._handle_tool_call(process, payload, raw, issue, session_id, on_event)
            return "continue"

        if method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
            return await self._approve_or_fail(process, payload, raw, issue, session_id, on_event, "acceptForSession")

        if method == "execCommandApproval":
            return await self._approve_or_fail(process, payload, raw, issue, session_id, on_event, "approved_for_session")

        if method == "applyPatchApproval":
            return await self._approve_or_fail(process, payload, raw, issue, session_id, on_event, "approved_for_session")

        if method == "item/tool/requestUserInput":
            await self._handle_tool_input_request(process, payload, raw, issue, session_id, on_event)
            return "continue"

        if _needs_input(method, payload):
            await _emit_turn_payload(on_event, AgentEventType.TURN_FAILED, issue, session_id, payload, raw)
            return "turn_input_required"

        return None

    async def _handle_tool_call(
        self,
        process: CodexProcess,
        payload: dict[str, Any],
        raw: str,
        issue: Issue,
        session_id: str,
        on_event: AgentEventCallback,
    ) -> None:
        request_id = payload.get("id")
        params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
        tool_name = _tool_call_name(params)
        arguments = params.get("arguments", {})
        result = _normalize_dynamic_tool_result(self._execute_tool(tool_name, arguments))

        if request_id is not None:
            await self._send_request(process, {"id": request_id, "result": result})

        if result.get("success") is True:
            event_name = "tool_call_completed"
        elif tool_name is None or tool_name != LINEAR_GRAPHQL_TOOL_NAME:
            event_name = "unsupported_tool_call"
        else:
            event_name = "tool_call_failed"

        await _emit(
            on_event,
            AgentEventType.NOTIFICATION,
            issue,
            session_id,
            message=event_name,
            data={"event": event_name, "payload": payload, "raw": raw, "result": result},
        )

    def _execute_tool(self, tool_name: str | None, arguments: Any) -> dict[str, Any]:
        if self.tool_executor is not None:
            return self.tool_executor(tool_name, arguments)

        if tool_name == LINEAR_GRAPHQL_TOOL_NAME and self.linear_client is not None:
            return LinearGraphQLTool(self.linear_client).run(arguments)

        return {
            "success": False,
            "error": {
                "code": "unsupported_tool",
                "message": f"Unsupported dynamic tool: {tool_name!r}.",
                "supportedTools": [LINEAR_GRAPHQL_TOOL_NAME],
            },
        }

    async def _approve_or_fail(
        self,
        process: CodexProcess,
        payload: dict[str, Any],
        raw: str,
        issue: Issue,
        session_id: str,
        on_event: AgentEventCallback,
        decision: str,
    ) -> str:
        if self.approval_policy == "never":
            request_id = payload.get("id")
            if request_id is not None:
                await self._send_request(process, {"id": request_id, "result": {"decision": decision}})
            await _emit(
                on_event,
                AgentEventType.NOTIFICATION,
                issue,
                session_id,
                message="approval_auto_approved",
                data={"event": "approval_auto_approved", "payload": payload, "raw": raw, "decision": decision},
            )
            return "continue"

        await _emit(
            on_event,
            AgentEventType.TURN_FAILED,
            issue,
            session_id,
            message="approval_required",
            data={"event": "approval_required", "payload": payload, "raw": raw},
        )
        return "approval_required"

    async def _handle_tool_input_request(
        self,
        process: CodexProcess,
        payload: dict[str, Any],
        raw: str,
        issue: Issue,
        session_id: str,
        on_event: AgentEventCallback,
    ) -> None:
        request_id = payload.get("id")
        params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
        answers, event_name = _tool_input_answers(params, auto_approve=self.approval_policy == "never")
        if request_id is not None:
            await self._send_request(process, {"id": request_id, "result": {"answers": answers}})

        await _emit(
            on_event,
            AgentEventType.NOTIFICATION,
            issue,
            session_id,
            message=event_name,
            data={"event": event_name, "payload": payload, "raw": raw, "answers": answers},
        )

    async def _read_payload(
        self,
        process: CodexProcess,
        *,
        timeout_ms: int,
    ) -> tuple[dict[str, Any] | None, str]:
        if process.stdout is None:
            raise AgentRunnerError("codex_stdout_unavailable")

        try:
            raw_bytes = await asyncio.wait_for(process.stdout.readline(), timeout=timeout_ms / 1000)
        except TimeoutError as exc:
            reason = "turn_timeout" if timeout_ms == self.turn_timeout_ms else "response_timeout"
            raise AgentRunnerError(reason) from exc

        if raw_bytes == b"":
            status = process.returncode
            if status is None:
                try:
                    status = await asyncio.wait_for(process.wait(), timeout=0.01)
                except TimeoutError:
                    status = None
            raise AgentRunnerError(f"port_exit:{status}")

        raw = raw_bytes.decode("utf-8", errors="replace").strip()
        if not raw:
            return None, raw

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return None, raw

        if not isinstance(payload, dict):
            return None, raw
        return payload, raw

    async def _stop_process(
        self,
        process: CodexProcess,
        stderr_task: asyncio.Task[None] | None,
    ) -> None:
        if stderr_task is not None:
            stderr_task.cancel()
            try:
                await stderr_task
            except asyncio.CancelledError:
                pass

        if process.returncode is not None:
            return

        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            process.kill()
            await process.wait()


def _positive_timeout(value: int, name: str) -> int:
    if value <= 0:
        raise AgentRunnerError(f"{name}_must_be_positive")
    return value


def _default_turn_sandbox_policy() -> dict[str, Any]:
    return {
        "type": "workspaceWrite",
        "readOnlyAccess": {"type": "fullAccess"},
        "networkAccess": False,
        "excludeTmpdirEnvVar": False,
        "excludeSlashTmp": False,
    }


def _linear_graphql_tool_spec() -> dict[str, Any]:
    return {
        "name": LINEAR_GRAPHQL_TOOL_NAME,
        "description": "Execute a raw GraphQL query or mutation against Linear using Jazzband's configured auth.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["query"],
            "properties": {
                "query": {
                    "type": "string",
                    "description": "GraphQL query or mutation document to execute against Linear.",
                },
                "variables": {
                    "type": ["object", "null"],
                    "description": "Optional GraphQL variables object.",
                    "additionalProperties": True,
                },
            },
        },
    }


def _session_state(session: AgentSession) -> _CodexSessionState:
    state = session.metadata.get("codex_state")
    if not isinstance(state, _CodexSessionState):
        raise AgentRunnerError("invalid_codex_session")
    return state


def _extract_thread_id(result: dict[str, Any]) -> str:
    thread = result.get("thread")
    if not isinstance(thread, dict) or not isinstance(thread.get("id"), str) or not thread["id"]:
        raise AgentRunnerError(f"invalid_thread_payload:{result}")
    return thread["id"]


def _extract_turn_id(result: dict[str, Any]) -> str:
    turn = result.get("turn")
    if not isinstance(turn, dict) or not isinstance(turn.get("id"), str) or not turn["id"]:
        raise AgentRunnerError(f"invalid_turn_payload:{result}")
    return turn["id"]


def _usage_from_payload(payload: dict[str, Any]) -> TokenUsage | None:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        params = payload.get("params")
        if isinstance(params, dict):
            usage = params.get("usage")
    if not isinstance(usage, dict):
        return None

    input_tokens = _int_field(usage, "input_tokens", "inputTokens", "input")
    output_tokens = _int_field(usage, "output_tokens", "outputTokens", "output")
    total_tokens = _int_field(usage, "total_tokens", "totalTokens", "total")
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    if input_tokens is None and output_tokens is None and total_tokens is None:
        return None

    try:
        return TokenUsage(
            input_tokens=input_tokens or 0,
            output_tokens=output_tokens or 0,
            total_tokens=total_tokens or ((input_tokens or 0) + (output_tokens or 0)),
        )
    except ValueError:
        return None


def _merge_usage(current: TokenUsage | None, new_usage: TokenUsage | None) -> TokenUsage | None:
    if new_usage is None:
        return current
    if current is None:
        return new_usage
    return current.merge(new_usage)


def _int_field(payload: dict[str, Any], *names: str) -> int | None:
    for name in names:
        value = payload.get(name)
        if isinstance(value, int) and value >= 0:
            return value
    return None


def _normalize_dynamic_tool_result(result: dict[str, Any]) -> dict[str, Any]:
    success = result.get("success")
    if not isinstance(success, bool):
        success = False

    output = result.get("output")
    if not isinstance(output, str):
        output = json.dumps(result, indent=2, sort_keys=True)

    content_items = result.get("contentItems")
    if not isinstance(content_items, list):
        content_items = [{"type": "inputText", "text": output}]

    normalized = dict(result)
    normalized["success"] = success
    normalized["output"] = output
    normalized["contentItems"] = content_items
    return normalized


def _tool_call_name(params: dict[str, Any]) -> str | None:
    name = params.get("tool") or params.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return None


def _tool_input_answers(params: dict[str, Any], *, auto_approve: bool) -> tuple[dict[str, Any], str]:
    questions = params.get("questions")
    if not isinstance(questions, list):
        return {}, "tool_input_auto_answered"

    if auto_approve:
        approval_answers: dict[str, Any] = {}
        for question in questions:
            if not isinstance(question, dict):
                approval_answers = {}
                break
            question_id = question.get("id")
            answer_label = _approval_option_label(question.get("options"))
            if not isinstance(question_id, str) or answer_label is None:
                approval_answers = {}
                break
            approval_answers[question_id] = {"answers": [answer_label]}
        if approval_answers:
            return approval_answers, "approval_auto_approved"

    answers = {}
    for question in questions:
        if isinstance(question, dict) and isinstance(question.get("id"), str):
            answers[question["id"]] = {"answers": [NON_INTERACTIVE_TOOL_INPUT_ANSWER]}
    return answers, "tool_input_auto_answered"


def _approval_option_label(options: Any) -> str | None:
    if not isinstance(options, list):
        return None
    labels = [option.get("label") for option in options if isinstance(option, dict) and isinstance(option.get("label"), str)]
    return (
        next((label for label in labels if label == "Approve this Session"), None)
        or next((label for label in labels if label == "Approve Once"), None)
        or next((label for label in labels if label.strip().lower().startswith(("approve", "allow"))), None)
    )


def _needs_input(method: Any, payload: dict[str, Any]) -> bool:
    if not isinstance(method, str):
        return False
    input_methods = {
        "turn/input_required",
        "turn/needs_input",
        "turn/need_input",
        "turn/request_input",
        "turn/request_response",
        "turn/provide_input",
        "turn/approval_required",
    }
    return method in input_methods or _requires_input(payload) or _requires_input(payload.get("params"))


def _requires_input(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    return (
        payload.get("requiresInput") is True
        or payload.get("needsInput") is True
        or payload.get("input_required") is True
        or payload.get("inputRequired") is True
        or payload.get("type") in {"input_required", "needs_input"}
    )


def _protocol_message_candidate(raw: str) -> bool:
    return raw.lstrip().startswith("{")


async def _emit_turn_payload(
    on_event: AgentEventCallback,
    event_type: AgentEventType,
    issue: Issue,
    session_id: str,
    payload: dict[str, Any],
    raw: str,
) -> None:
    method = payload.get("method")
    await _emit(
        on_event,
        event_type,
        issue,
        session_id,
        message=str(method or event_type.value),
        data={"event": method or event_type.value, "payload": payload, "raw": raw},
    )


async def _emit(
    on_event: AgentEventCallback,
    event_type: AgentEventType,
    issue: Issue,
    session_id: str,
    *,
    message: str,
    data: dict[str, Any],
) -> None:
    await on_event(
        AgentEvent(
            type=event_type,
            message=message,
            issue_id=issue.id,
            issue_identifier=issue.identifier,
            session_id=session_id,
            data=data,
        )
    )


def _start_stderr_drain(process: CodexProcess) -> asyncio.Task[None] | None:
    if process.stderr is None:
        return None
    return asyncio.create_task(_drain_stderr(process.stderr))


async def _drain_stderr(stderr: CodexStreamReader) -> None:
    while True:
        line = await stderr.readline()
        if line == b"":
            return
