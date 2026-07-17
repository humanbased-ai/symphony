import unittest
from collections import OrderedDict

from jazzband.auth import TokenStore
from jazzband.config import TrackerConfig
from jazzband.tracker.linear import (
    CANDIDATE_ISSUES_QUERY,
    ISSUES_BY_ID_QUERY,
    GraphQLResponse,
    LinearAPIStatusError,
    LinearClient,
    LinearGraphQLError,
    LinearMissingEndCursorError,
    normalize_issue,
)
from jazzband.tools.linear_graphql import LinearGraphQLTool, linear_graphql_tool


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
                    "project_slug": "jazzband-ai-agent-orchestration",
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
        self.assertEqual("jazzband-ai-agent-orchestration", transport.calls[0]["payload"]["variables"]["projectSlug"])
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


class FeedbackMethodTests(unittest.TestCase):
    def client(self, transport):
        tracker = TrackerConfig.from_mapping(
            {
                "tracker": {
                    "kind": "linear",
                    "project_slug": "jazzband-abc123",
                    "api_key": "$K",
                }
            }
        )
        return LinearClient(tracker, token_store=TokenStore(tracker, environ={"K": "lin_tok"}), transport=transport)

    def test_fetch_issue_comment_ids_returns_ids_in_order(self):
        transport = RecordingTransport(
            [
                GraphQLResponse(
                    200,
                    {
                        "data": {
                            "issue": {
                                "comments": {
                                    "nodes": [
                                        {"id": "cmt-1", "body": "hello", "createdAt": "2026-05-01T00:00:00Z", "user": {"name": "Alice"}},
                                        {"id": "cmt-2", "body": "world", "createdAt": "2026-05-02T00:00:00Z", "user": {"name": "Bob"}},
                                    ]
                                }
                            }
                        }
                    },
                )
            ]
        )
        client = self.client(transport)
        ids = client.fetch_issue_comment_ids("issue-x")
        self.assertEqual(["cmt-1", "cmt-2"], ids)

    def test_fetch_issue_comment_ids_returns_empty_on_missing_issue(self):
        transport = RecordingTransport([GraphQLResponse(200, {"data": {"issue": None}})])
        client = self.client(transport)
        ids = client.fetch_issue_comment_ids("no-such-issue")
        self.assertEqual([], ids)

    def test_create_comment_returns_true_on_success(self):
        transport = RecordingTransport(
            [GraphQLResponse(200, {"data": {"commentCreate": {"success": True, "comment": {"id": "c-new"}}}})]
        )
        client = self.client(transport)
        result = client.create_comment("issue-x", "LGTM!")
        self.assertTrue(result)
        self.assertEqual(1, len(transport.calls))
        payload = transport.calls[0]["payload"]
        self.assertIn("commentCreate", payload["query"])
        self.assertEqual("issue-x", payload["variables"]["issueId"])
        self.assertEqual("LGTM!", payload["variables"]["body"])

    def test_create_comment_returns_false_on_graphql_error(self):
        transport = RecordingTransport([GraphQLResponse(200, {"data": None, "errors": [{"message": "oops"}]})])
        client = self.client(transport)
        result = client.create_comment("issue-x", "hello")
        self.assertFalse(result)

    def test_update_issue_state_by_name_resolves_state_and_updates(self):
        team_resp = GraphQLResponse(
            200, {"data": {"projects": {"nodes": [{"teams": {"nodes": [{"id": "team-1"}]}}]}}}
        )
        states_resp = GraphQLResponse(
            200, {"data": {"workflowStates": {"nodes": [{"id": "state-done", "name": "Done"}, {"id": "state-todo", "name": "Todo"}]}}}
        )
        update_resp = GraphQLResponse(
            200, {"data": {"issueUpdate": {"success": True, "issue": {"id": "issue-x", "state": {"name": "Done"}}}}}
        )
        transport = RecordingTransport([team_resp, states_resp, update_resp])
        client = self.client(transport)
        result = client.update_issue_state_by_name("issue-x", "Done")
        self.assertTrue(result)
        self.assertEqual(3, len(transport.calls))
        update_vars = transport.calls[2]["payload"]["variables"]
        self.assertEqual("issue-x", update_vars["issueId"])
        self.assertEqual("state-done", update_vars["stateId"])

    def test_update_issue_state_by_name_returns_false_when_state_not_found(self):
        team_resp = GraphQLResponse(
            200, {"data": {"projects": {"nodes": [{"teams": {"nodes": [{"id": "team-1"}]}}]}}}
        )
        states_resp = GraphQLResponse(
            200, {"data": {"workflowStates": {"nodes": [{"id": "state-todo", "name": "Todo"}]}}}
        )
        transport = RecordingTransport([team_resp, states_resp])
        client = self.client(transport)
        result = client.update_issue_state_by_name("issue-x", "NonExistent")
        self.assertFalse(result)

    def test_update_issue_state_by_name_returns_false_when_team_not_found(self):
        team_resp = GraphQLResponse(200, {"data": {"projects": {"nodes": []}}})
        transport = RecordingTransport([team_resp])
        client = self.client(transport)
        result = client.update_issue_state_by_name("issue-x", "Done")
        self.assertFalse(result)


class FeedbackSignalTests(unittest.TestCase):
    """Unit tests for the classify_feedback function."""

    def _make_response(self, label: str) -> bytes:
        import json
        return json.dumps({"content": [{"text": label}]}).encode()

    def test_empty_list_returns_none_without_http_call(self):
        from jazzband.feedback import classify_feedback
        from unittest.mock import patch

        with patch("urllib.request.urlopen") as mock_open:
            result = classify_feedback([], api_key="test-key")
        self.assertIsNone(result)
        mock_open.assert_not_called()

    def test_approve_label_parsed(self):
        from jazzband.feedback import FeedbackSignal, classify_feedback
        from unittest.mock import MagicMock, patch

        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = self._make_response("APPROVE")
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = classify_feedback(["Alice: LGTM"], api_key="test-key")
        self.assertEqual(FeedbackSignal.APPROVE, result)

    def test_change_request_label_parsed(self):
        from jazzband.feedback import FeedbackSignal, classify_feedback
        from unittest.mock import MagicMock, patch

        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = self._make_response("CHANGE_REQUEST")
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = classify_feedback(["Bob: please fix this"], api_key="test-key")
        self.assertEqual(FeedbackSignal.CHANGE_REQUEST, result)

    def test_close_label_parsed(self):
        from jazzband.feedback import FeedbackSignal, classify_feedback
        from unittest.mock import MagicMock, patch

        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = self._make_response("CLOSE")
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = classify_feedback(["Carol: not needed"], api_key="test-key")
        self.assertEqual(FeedbackSignal.CLOSE, result)

    def test_none_label_returns_none(self):
        from jazzband.feedback import classify_feedback
        from unittest.mock import MagicMock, patch

        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = self._make_response("NONE")
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = classify_feedback(["Dave: interesting"], api_key="test-key")
        self.assertIsNone(result)

    def test_http_error_raises_classify_error(self):
        import urllib.error
        from jazzband.feedback import ClassifyError, classify_feedback
        from unittest.mock import patch

        with self.assertRaises(ClassifyError):
            with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("timeout")):
                classify_feedback(["Alice: LGTM"], api_key="test-key")

    def test_label_is_case_insensitive(self):
        from jazzband.feedback import FeedbackSignal, classify_feedback
        from unittest.mock import MagicMock, patch

        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = self._make_response("approve")
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = classify_feedback(["Alice: LGTM"], api_key="test-key")
        self.assertEqual(FeedbackSignal.APPROVE, result)


if __name__ == "__main__":
    unittest.main()
