import unittest
from collections import OrderedDict

from symphony.auth import TokenStore
from symphony.config import TrackerConfig
from symphony.tracker.linear import (
    CANDIDATE_ISSUES_QUERY,
    ISSUES_BY_ID_QUERY,
    GraphQLResponse,
    LinearAPIStatusError,
    LinearClient,
    LinearGraphQLError,
    LinearIssueUpdateFailedError,
    LinearMissingEndCursorError,
    LinearWorkflowStateNotFoundError,
    normalize_issue,
)
from symphony.tools.linear_graphql import LinearGraphQLTool, linear_graphql_tool


def issue_payload(issue_id="issue-1", identifier="IN-1", state="Todo", priority=1):
    return {
        "id": issue_id,
        "identifier": identifier,
        "title": "Build the thing",
        "description": "Do useful work",
        "priority": priority,
        "state": {"name": state},
        "branchName": "feature/in-1",
        "url": "https://linear.app/example/issue/IN-1",
        "labels": {"nodes": [{"name": "Backend"}, {"name": "MVP"}]},
        "inverseRelations": {
            "nodes": [
                {
                    "type": "blocks",
                    "issue": {
                        "id": "blocker-1",
                        "identifier": "IN-0",
                        "state": {"name": "Done"},
                    },
                },
                {
                    "type": "relates",
                    "issue": {
                        "id": "related-1",
                        "identifier": "IN-9",
                        "state": {"name": "Todo"},
                    },
                },
            ]
        },
        "createdAt": "2026-05-07T01:02:03.000Z",
        "updatedAt": "2026-05-07T04:05:06.000Z",
    }


class RecordingTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, payload, headers, timeout):
        self.calls.append({"url": url, "payload": payload, "headers": headers, "timeout": timeout})
        if not self.responses:
            raise AssertionError("unexpected transport call")
        return self.responses.pop(0)


class LinearTrackerTests(unittest.TestCase):
    def client(self, transport):
        tracker = TrackerConfig.from_mapping(
            {
                "tracker": {
                    "kind": "linear",
                    "project_slug": "symphony-ai-agent-orchestration",
                    "active_states": ["Todo", "In Progress"],
                    "api_key": "$LINEAR_KEY",
                }
            }
        )
        return LinearClient(tracker, token_store=TokenStore(tracker, environ={"LINEAR_KEY": "lin_secret"}), transport=transport)

    def test_normalize_issue_payload(self):
        issue = normalize_issue(issue_payload(priority="not-an-int"))

        self.assertEqual("issue-1", issue.id)
        self.assertEqual("IN-1", issue.identifier)
        self.assertEqual("Todo", issue.state)
        self.assertIsNone(issue.priority)
        self.assertEqual(("backend", "mvp"), issue.labels)
        self.assertEqual(1, len(issue.blocked_by))
        self.assertEqual("IN-0", issue.blocked_by[0].identifier)
        self.assertEqual(2026, issue.created_at.year)

    def test_fetch_candidate_issues_paginates_and_preserves_order(self):
        transport = RecordingTransport(
            [
                GraphQLResponse(
                    200,
                    {
                        "data": {
                            "issues": {
                                "nodes": [issue_payload("issue-1", "IN-1")],
                                "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"},
                            }
                        }
                    },
                ),
                GraphQLResponse(
                    200,
                    {
                        "data": {
                            "issues": {
                                "nodes": [issue_payload("issue-2", "IN-2")],
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                            }
                        }
                    },
                ),
            ]
        )

        issues = self.client(transport).fetch_candidate_issues()

        self.assertEqual(["IN-1", "IN-2"], [issue.identifier for issue in issues])
        self.assertIn("slugId", transport.calls[0]["payload"]["query"])
        self.assertEqual(CANDIDATE_ISSUES_QUERY, transport.calls[0]["payload"]["query"])
        self.assertEqual("symphony-ai-agent-orchestration", transport.calls[0]["payload"]["variables"]["projectSlug"])
        self.assertEqual(["Todo", "In Progress"], transport.calls[0]["payload"]["variables"]["stateNames"])
        self.assertIsNone(transport.calls[0]["payload"]["variables"]["after"])
        self.assertEqual("cursor-1", transport.calls[1]["payload"]["variables"]["after"])
        self.assertEqual("lin_secret", transport.calls[0]["headers"]["Authorization"])

    def test_fetch_issues_by_empty_states_skips_api_call(self):
        transport = RecordingTransport([])

        issues = self.client(transport).fetch_issues_by_states([])

        self.assertEqual([], issues)
        self.assertEqual([], transport.calls)

    def test_missing_end_cursor_is_pagination_error(self):
        transport = RecordingTransport(
            [
                GraphQLResponse(
                    200,
                    {
                        "data": {
                            "issues": {
                                "nodes": [],
                                "pageInfo": {"hasNextPage": True, "endCursor": None},
                            }
                        }
                    },
                )
            ]
        )

        with self.assertRaisesRegex(LinearMissingEndCursorError, "linear_missing_end_cursor"):
            self.client(transport).fetch_candidate_issues()

    def test_fetch_issue_states_by_ids_uses_graphql_id_query_and_requested_order(self):
        transport = RecordingTransport(
            [
                GraphQLResponse(
                    200,
                    {
                        "data": {
                            "issues": {
                                "nodes": [
                                    issue_payload("issue-2", "IN-2", "Done"),
                                    issue_payload("issue-1", "IN-1", "In Progress"),
                                ]
                            }
                        }
                    },
                )
            ]
        )

        issues = self.client(transport).fetch_issue_states_by_ids(["issue-1", "issue-2", "issue-1"])

        self.assertEqual(["issue-1", "issue-2"], [issue.id for issue in issues])
        self.assertEqual(ISSUES_BY_ID_QUERY, transport.calls[0]["payload"]["query"])
        self.assertIn("$ids: [ID!]!", transport.calls[0]["payload"]["query"])
        self.assertEqual(["issue-1", "issue-2"], transport.calls[0]["payload"]["variables"]["ids"])

    def test_graphql_errors_are_redacted(self):
        transport = RecordingTransport([GraphQLResponse(200, {"errors": [{"message": "bad lin_secret"}]})])

        with self.assertRaises(LinearGraphQLError) as raised:
            self.client(transport).fetch_candidate_issues()

        self.assertIn("[REDACTED]", str(raised.exception))
        self.assertNotIn("lin_secret", str(raised.exception))

    def test_status_errors_are_redacted(self):
        transport = RecordingTransport([GraphQLResponse(401, "token lin_secret rejected")])

        with self.assertRaises(LinearAPIStatusError) as raised:
            self.client(transport).fetch_candidate_issues()

        self.assertIn("[REDACTED]", str(raised.exception))
        self.assertNotIn("lin_secret", str(raised.exception))

    def test_linear_graphql_tool_executes_object_input_with_orchestrator_auth(self):
        transport = RecordingTransport([GraphQLResponse(200, {"data": {"issue": {"id": "issue-1"}}})])

        result = linear_graphql_tool(
            self.client(transport),
            {
                "query": "query Issue($id: String!) { issue(id: $id) { id } }",
                "variables": {"id": "IN-1"},
            },
        )

        self.assertTrue(result["success"])
        self.assertEqual({"data": {"issue": {"id": "issue-1"}}}, result["response"])
        self.assertEqual({"id": "IN-1"}, transport.calls[0]["payload"]["variables"])
        self.assertEqual("lin_secret", transport.calls[0]["headers"]["Authorization"])

    def test_linear_graphql_tool_accepts_mapping_variables(self):
        transport = RecordingTransport([GraphQLResponse(200, {"data": {"issue": {"id": "issue-1"}}})])

        result = linear_graphql_tool(
            self.client(transport),
            {
                "query": "query Issue($id: String!) { issue(id: $id) { id } }",
                "variables": OrderedDict([("id", "IN-1")]),
            },
        )

        self.assertTrue(result["success"])
        self.assertEqual({"id": "IN-1"}, transport.calls[0]["payload"]["variables"])

    def test_linear_graphql_tool_accepts_raw_query_string(self):
        transport = RecordingTransport([GraphQLResponse(200, {"data": {"viewer": {"id": "viewer-1"}}})])

        result = linear_graphql_tool(self.client(transport), "{ viewer { id } }")

        self.assertTrue(result["success"])
        self.assertEqual({"data": {"viewer": {"id": "viewer-1"}}}, result["response"])

    def test_linear_graphql_tool_preserves_graphql_error_body_as_failure(self):
        transport = RecordingTransport([GraphQLResponse(200, {"errors": [{"message": "bad lin_secret"}]})])

        result = linear_graphql_tool(self.client(transport), "{ viewer { id } }")

        self.assertFalse(result["success"])
        self.assertEqual("linear_graphql_errors", result["error"]["code"])
        self.assertEqual({"errors": [{"message": "bad [REDACTED]"}]}, result["response"])
        self.assertNotIn("lin_secret", str(result))

    def test_linear_graphql_tool_rejects_invalid_input_without_api_call(self):
        transport = RecordingTransport([])

        result = linear_graphql_tool(self.client(transport), {"query": "{ viewer { id } }", "variables": []})

        self.assertFalse(result["success"])
        self.assertEqual("invalid_input", result["error"]["code"])
        self.assertEqual([], transport.calls)

    def test_linear_graphql_tool_rejects_multiple_operations(self):
        transport = RecordingTransport([])

        result = linear_graphql_tool(
            self.client(transport),
            "query First { viewer { id } } mutation Second { issueUpdate(id: \"1\", input: {}) { success } }",
        )

        self.assertFalse(result["success"])
        self.assertEqual("invalid_input", result["error"]["code"])
        self.assertIn("exactly_one_operation", result["error"]["message"])
        self.assertEqual([], transport.calls)

    def test_linear_graphql_tool_rejects_multiple_anonymous_shorthand_operations(self):
        transport = RecordingTransport([])

        result = linear_graphql_tool(self.client(transport), "{ viewer { id } } { team { id } }")

        self.assertFalse(result["success"])
        self.assertEqual("invalid_input", result["error"]["code"])
        self.assertIn("exactly_one_operation", result["error"]["message"])
        self.assertEqual([], transport.calls)

    def test_linear_graphql_tool_ignores_escaped_block_string_terminator(self):
        transport = RecordingTransport([GraphQLResponse(200, {"data": {"viewer": {"id": "viewer-1"}}})])

        result = linear_graphql_tool(
            self.client(transport),
            'query Search { viewer { id } search(text: """before \\""" { notAnOperation } after""") { id } }',
        )

        self.assertTrue(result["success"])
        self.assertEqual({"data": {"viewer": {"id": "viewer-1"}}}, result["response"])

    def test_linear_graphql_tool_returns_transport_failure_payload(self):
        class FailingTransport:
            def __call__(self, url, payload, headers, timeout):
                raise RuntimeError("token lin_secret rejected")

        result = LinearGraphQLTool(self.client(FailingTransport())).run("{ viewer { id } }")

        self.assertFalse(result["success"])
        self.assertEqual("linear_api_request", result["error"]["code"])
        self.assertIn("[REDACTED]", result["error"]["message"])
        self.assertNotIn("lin_secret", str(result))


def _states_response() -> GraphQLResponse:
    return GraphQLResponse(
        200,
        {
            "data": {
                "team": {
                    "states": {
                        "nodes": [
                            {"id": "state-todo", "name": "Todo", "type": "unstarted"},
                            {"id": "state-progress", "name": "In Progress", "type": "started"},
                            {"id": "state-done", "name": "Done", "type": "completed"},
                        ]
                    }
                }
            }
        },
    )


def _claim_mutation_response(state_name: str = "In Progress") -> GraphQLResponse:
    return GraphQLResponse(
        200,
        {
            "data": {
                "issueUpdate": {
                    "success": True,
                    "issue": {
                        "id": "issue-1",
                        "identifier": "IN-1",
                        "title": "Build the thing",
                        "description": "Do useful work",
                        "priority": 1,
                        "state": {"name": state_name},
                        "branchName": "feature/in-1",
                        "url": "https://linear.app/example/issue/IN-1",
                        "team": {"id": "team-1"},
                        "labels": {"nodes": []},
                        "createdAt": "2026-05-07T01:02:03.000Z",
                        "updatedAt": "2026-05-07T04:05:06.000Z",
                    },
                }
            }
        },
    )


class MoveIssueToStateTests(unittest.TestCase):
    def _client(self, transport: RecordingTransport) -> LinearClient:
        tracker = TrackerConfig.from_mapping(
            {
                "tracker": {
                    "kind": "linear",
                    "project_slug": "symphony",
                    "active_states": ["Todo"],
                    "api_key": "$LINEAR_KEY",
                }
            }
        )
        return LinearClient(
            tracker,
            token_store=TokenStore(tracker, environ={"LINEAR_KEY": "lin_secret"}),
            transport=transport,
        )

    def test_move_resolves_state_name_and_invokes_mutation(self):
        transport = RecordingTransport([_states_response(), _claim_mutation_response()])
        client = self._client(transport)

        result = client.move_issue_to_state("issue-1", "team-1", "In Progress")

        self.assertEqual("issue-1", result.id)
        self.assertEqual("In Progress", result.state)
        self.assertEqual("team-1", result.team_id)
        self.assertEqual(2, len(transport.calls))
        states_call, mutation_call = transport.calls
        self.assertIn("team(id: $teamId)", states_call["payload"]["query"])
        self.assertEqual({"teamId": "team-1", "first": 100}, states_call["payload"]["variables"])
        self.assertIn("issueUpdate", mutation_call["payload"]["query"])
        self.assertEqual(
            {"issueId": "issue-1", "stateId": "state-progress"},
            mutation_call["payload"]["variables"],
        )

    def test_state_map_is_cached_per_team(self):
        transport = RecordingTransport(
            [
                _states_response(),
                _claim_mutation_response(),
                _claim_mutation_response(state_name="Done"),
            ]
        )
        client = self._client(transport)

        client.move_issue_to_state("issue-1", "team-1", "In Progress")
        client.move_issue_to_state("issue-1", "team-1", "Done")

        # Only one workflowStates query for the second call (cache hit).
        self.assertEqual(3, len(transport.calls))
        self.assertIn("issueUpdate", transport.calls[2]["payload"]["query"])

    def test_missing_state_raises(self):
        # First call returns the initial map; the refresh call also returns it
        # so the not-found path is exercised end-to-end.
        transport = RecordingTransport([_states_response(), _states_response()])
        client = self._client(transport)

        with self.assertRaises(LinearWorkflowStateNotFoundError):
            client.move_issue_to_state("issue-1", "team-1", "Nonexistent")

    def test_missing_team_id_raises(self):
        client = self._client(RecordingTransport([]))

        with self.assertRaises(LinearWorkflowStateNotFoundError):
            client.move_issue_to_state("issue-1", None, "In Progress")

    def test_unsuccessful_update_raises(self):
        bad_response = GraphQLResponse(
            200, {"data": {"issueUpdate": {"success": False, "issue": None}}}
        )
        transport = RecordingTransport([_states_response(), bad_response])
        client = self._client(transport)

        with self.assertRaises(LinearIssueUpdateFailedError):
            client.move_issue_to_state("issue-1", "team-1", "In Progress")


if __name__ == "__main__":
    unittest.main()
