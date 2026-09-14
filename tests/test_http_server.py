from __future__ import annotations

import asyncio
import unittest
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from jazzband.http_server import StatusAPI, build_issue_detail, build_state_snapshot, create_fastapi_app
from jazzband.tracker.models import Issue


@dataclass
class RunningEntry:
    issue: Issue
    started_at_ms: int
    retry_attempt: int | None = None
    last_event_at_ms: int | None = None
    session_id: str | None = None
    last_event: str | None = None
    last_message: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    turn_count: int = 0
    workspace_path: Path | None = None
    recent_events: list[dict[str, str]] = field(default_factory=list)
    log_paths: tuple[Path, ...] = ()


@dataclass(frozen=True)
class RetryEntry:
    issue_id: str
    identifier: str
    attempt: int
    due_at_ms: int
    error: str | None = None


@dataclass
class OrchestratorState:
    running: dict[str, RunningEntry] = field(default_factory=dict)
    retry_attempts: dict[str, RetryEntry] = field(default_factory=dict)
    completed: set[str] = field(default_factory=set)
    claimed: set[str] = field(default_factory=set)


def issue(issue_id: str = "issue-1", identifier: str = "IN-172", state: str = "In Progress") -> Issue:
    return Issue(
        id=issue_id,
        identifier=identifier,
        title="Build minimal status API",
        description="Expose HTTP status endpoints",
        priority=1,
        state=state,
        branch_name="feat/in-172-status-api",
        url="https://linear.app/example/issue/IN-172",
        labels=("backend",),
    )


def sample_state() -> OrchestratorState:
    state = OrchestratorState()
    state.running["issue-1"] = RunningEntry(
        issue=issue(),
        started_at_ms=1_700_000_000_000,
        last_event_at_ms=1_700_000_010_000,
        session_id="thread-1-turn-1",
        last_event="turn_completed",
        last_message="Tests passed",
        input_tokens=1200,
        output_tokens=800,
        turn_count=7,
        workspace_path=Path("/tmp/jazzband_workspaces/IN-172"),
        recent_events=[{"event": "turn_completed", "message": "Tests passed"}],
        log_paths=(Path("/tmp/jazzband/logs/IN-172/latest.log"),),
    )
    state.retry_attempts["issue-2"] = RetryEntry(
        issue_id="issue-2",
        identifier="IN-175",
        attempt=3,
        due_at_ms=1_700_000_030_000,
        error="no available orchestrator slots",
    )
    state.claimed.update({"issue-1", "issue-2"})
    state.completed.add("issue-0")
    return state


class StatusSnapshotTests(unittest.TestCase):
    def test_state_snapshot_serializes_running_retrying_and_totals(self):
        snapshot = build_state_snapshot(
            sample_state(),
            now=datetime(2023, 11, 14, 22, 13, 30, tzinfo=timezone.utc),
        )

        self.assertEqual("2023-11-14T22:13:30Z", snapshot["generated_at"])
        self.assertEqual({"running": 1, "retrying": 1, "completed": 1, "claimed": 2}, snapshot["counts"])
        self.assertEqual("IN-172", snapshot["running"][0]["issue_identifier"])
        self.assertEqual("2023-11-14T22:13:20Z", snapshot["running"][0]["started_at"])
        self.assertEqual({"input_tokens": 1200, "output_tokens": 800, "total_tokens": 2000}, snapshot["running"][0]["tokens"])
        self.assertEqual("IN-175", snapshot["retrying"][0]["issue_identifier"])
        self.assertEqual("2023-11-14T22:13:50Z", snapshot["retrying"][0]["due_at"])
        self.assertEqual(2000, snapshot["codex_totals"]["total_tokens"])
        self.assertEqual(10.0, snapshot["codex_totals"]["seconds_running"])

    def test_issue_detail_finds_running_issue_by_identifier(self):
        detail = build_issue_detail(
            sample_state(),
            "in-172",
            now=datetime(2023, 11, 14, 22, 13, 30, tzinfo=timezone.utc),
        )

        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual("running", detail["status"])
        self.assertEqual("issue-1", detail["issue_id"])
        self.assertEqual({"path": "/tmp/jazzband_workspaces/IN-172"}, detail["workspace"])
        self.assertEqual(7, detail["running"]["turn_count"])
        self.assertEqual("Build minimal status API", detail["tracked"]["title"])
        self.assertEqual(
            [{"label": "latest", "path": "/tmp/jazzband/logs/IN-172/latest.log", "url": None}],
            detail["logs"]["codex_session_logs"],
        )

    def test_issue_detail_finds_retrying_issue(self):
        detail = build_issue_detail(sample_state(), "IN-175")

        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual("retrying", detail["status"])
        self.assertIsNone(detail["running"])
        self.assertEqual(3, detail["retry"]["attempt"])
        self.assertEqual("no available orchestrator slots", detail["last_error"])


class StatusAPIHandlerTests(unittest.TestCase):
    def test_health_endpoint_reports_running_count(self):
        api = StatusAPI(lambda: sample_state(), monotonic_started_at=0)

        response = api.handle_request("GET", "/api/v1/health")

        self.assertEqual(200, response.status_code)
        self.assertEqual("ok", response.body["status"])
        self.assertEqual(1, response.body["running"])
        self.assertGreaterEqual(response.body["uptime_s"], 0)

    def test_state_endpoint_returns_snapshot(self):
        api = StatusAPI(lambda: sample_state())

        response = api.handle_request("GET", "/api/v1/state?unused=1")

        self.assertEqual(200, response.status_code)
        self.assertEqual(1, response.body["counts"]["running"])
        self.assertEqual("application/json; charset=utf-8", response.headers["content-type"])
        self.assertIn(b'"running":1', response.json_bytes())

    def test_issue_endpoint_returns_404_for_unknown_issue(self):
        api = StatusAPI(lambda: sample_state())

        response = api.handle_request("GET", "/api/v1/IN-999")

        self.assertEqual(404, response.status_code)
        self.assertEqual("issue_not_found", response.body["error"]["code"])

    def test_refresh_endpoint_invokes_callback_and_returns_accepted(self):
        calls = []
        api = StatusAPI(
            lambda: sample_state(),
            refresh_callback=lambda: calls.append("refresh") or {"coalesced": True},
        )

        response = api.handle_request("POST", "/api/v1/refresh", b"{}")

        self.assertEqual(["refresh"], calls)
        self.assertEqual(202, response.status_code)
        self.assertTrue(response.body["queued"])
        self.assertTrue(response.body["coalesced"])
        self.assertEqual(["poll", "reconcile"], response.body["operations"])

    def test_refresh_endpoint_can_await_async_callback(self):
        calls = []

        async def refresh():
            calls.append("refresh")
            return {"queued": True, "operations": ["poll"]}

        api = StatusAPI(lambda: sample_state(), refresh_callback=refresh)

        response = asyncio.run(api.async_handle_request("POST", "/api/v1/refresh"))

        self.assertEqual(["refresh"], calls)
        self.assertEqual(202, response.status_code)
        self.assertEqual(["poll"], response.body["operations"])

    def test_refresh_without_callback_returns_service_unavailable(self):
        api = StatusAPI(lambda: sample_state())

        response = api.handle_request("POST", "/api/v1/refresh")

        self.assertEqual(503, response.status_code)
        self.assertEqual("refresh_unavailable", response.body["error"]["code"])

    def test_unsupported_methods_return_405(self):
        api = StatusAPI(lambda: sample_state())

        response = api.handle_request("POST", "/api/v1/state")

        self.assertEqual(405, response.status_code)
        self.assertEqual("method_not_allowed", response.body["error"]["code"])

    def test_invalid_refresh_body_returns_400(self):
        api = StatusAPI(lambda: sample_state(), refresh_callback=lambda: None)

        response = api.handle_request("POST", "/api/v1/refresh", b"{")

        self.assertEqual(400, response.status_code)
        self.assertEqual("invalid_json", response.body["error"]["code"])

    def test_unknown_route_returns_404(self):
        api = StatusAPI(lambda: sample_state())

        response = api.handle_request("GET", "/api/v1/state/extra")

        self.assertEqual(404, response.status_code)
        self.assertEqual("route_not_found", response.body["error"]["code"])

    def test_fastapi_factory_reports_missing_dependency(self):
        api = StatusAPI(lambda: sample_state())

        with self.assertRaisesRegex(RuntimeError, "fastapi_unavailable"):
            create_fastapi_app(api)


if __name__ == "__main__":
    unittest.main()
