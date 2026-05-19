from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from symphony.auth import MissingLinearTokenError, TokenStore, redact_secret
from symphony.config import TrackerConfig
from symphony.tracker.models import Blocker, Issue


ISSUE_PAGE_SIZE = 50
NETWORK_TIMEOUT_SECONDS = 30


CANDIDATE_ISSUES_QUERY = """
query SymphonyLinearPoll($projectSlug: String!, $stateNames: [String!]!, $first: Int!, $relationFirst: Int!, $after: String) {
  issues(filter: {project: {slugId: {eq: $projectSlug}}, state: {name: {in: $stateNames}}}, first: $first, after: $after) {
    nodes {
      id
      identifier
      title
      description
      priority
      state {
        name
      }
      branchName
      url
      team {
        id
      }
      labels {
        nodes {
          name
        }
      }
      inverseRelations(first: $relationFirst) {
        nodes {
          type
          issue {
            id
            identifier
            state {
              name
            }
          }
        }
      }
      createdAt
      updatedAt
    }
    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
""".strip()


WORKFLOW_STATES_QUERY = """
query SymphonyWorkflowStates($teamId: String!, $first: Int!) {
  team(id: $teamId) {
    states(first: $first) {
      nodes {
        id
        name
        type
      }
    }
  }
}
""".strip()


ISSUE_UPDATE_STATE_MUTATION = """
mutation SymphonyClaimIssue($issueId: String!, $stateId: String!) {
  issueUpdate(id: $issueId, input: {stateId: $stateId}) {
    success
    issue {
      id
      identifier
      title
      description
      priority
      state {
        name
      }
      branchName
      url
      team {
        id
      }
      labels {
        nodes {
          name
        }
      }
      createdAt
      updatedAt
    }
  }
}
""".strip()


ISSUE_COMMENTS_QUERY = """
query SymphonyLinearIssueComments($issueId: String!, $first: Int!) {
  issue(id: $issueId) {
    comments(first: $first) {
      nodes {
        body
        user {
          name
        }
      }
    }
  }
}
""".strip()


ISSUES_BY_ID_QUERY = """
query SymphonyLinearIssuesById($ids: [ID!]!, $first: Int!, $relationFirst: Int!) {
  issues(filter: {id: {in: $ids}}, first: $first) {
    nodes {
      id
      identifier
      title
      description
      priority
      state {
        name
      }
      branchName
      url
      team {
        id
      }
      labels {
        nodes {
          name
        }
      }
      inverseRelations(first: $relationFirst) {
        nodes {
          type
          issue {
            id
            identifier
            state {
              name
            }
          }
        }
      }
      createdAt
      updatedAt
    }
  }
}
""".strip()


@dataclass(frozen=True)
class GraphQLResponse:
    status: int
    body: dict[str, Any] | str


Transport = Callable[[str, dict[str, Any], dict[str, str], float], GraphQLResponse]


class LinearClientError(RuntimeError):
    code = "linear_error"


class LinearAPIRequestError(LinearClientError):
    code = "linear_api_request"


class LinearAPIStatusError(LinearClientError):
    code = "linear_api_status"


class LinearGraphQLError(LinearClientError):
    code = "linear_graphql_errors"


class LinearUnknownPayloadError(LinearClientError):
    code = "linear_unknown_payload"


class LinearMissingEndCursorError(LinearClientError):
    code = "linear_missing_end_cursor"


class MissingLinearProjectSlugError(LinearClientError):
    code = "missing_tracker_project_slug"


class LinearWorkflowStateNotFoundError(LinearClientError):
    code = "linear_workflow_state_not_found"


class LinearIssueUpdateFailedError(LinearClientError):
    code = "linear_issue_update_failed"


class LinearClient:
    def __init__(
        self,
        tracker: TrackerConfig,
        token_store: TokenStore | None = None,
        transport: Transport | None = None,
    ) -> None:
        self.tracker = tracker
        self.token_store = token_store or TokenStore(tracker)
        self.transport = transport or _urllib_transport
        # Per-process cache of workflow state name → ID, keyed by team_id. Linear
        # state IDs are stable, so caching once per daemon lifetime is safe; if
        # the cache becomes stale (a state is renamed/recreated), the next
        # ``updateIssue`` will fail with a clear error and the operator can
        # restart Symphony to pick up the new map.
        self._workflow_state_cache: dict[str, dict[str, str]] = {}

    def fetch_candidate_issues(self) -> list[Issue]:
        return self.fetch_issues_by_states(list(self.tracker.active_states))

    def fetch_issues_by_states(self, state_names: list[str]) -> list[Issue]:
        states = tuple(dict.fromkeys(str(state) for state in state_names if str(state).strip()))
        if not states:
            return []
        if not self.tracker.project_slug:
            raise MissingLinearProjectSlugError("missing_tracker_project_slug")

        issues: list[Issue] = []
        after: str | None = None

        while True:
            body = self.graphql(
                CANDIDATE_ISSUES_QUERY,
                {
                    "projectSlug": self.tracker.project_slug,
                    "stateNames": list(states),
                    "first": ISSUE_PAGE_SIZE,
                    "relationFirst": ISSUE_PAGE_SIZE,
                    "after": after,
                },
            )
            nodes, page_info = _decode_issue_page(body)
            issues.extend(normalize_issue(node) for node in nodes)

            if page_info.get("hasNextPage") is True:
                cursor = page_info.get("endCursor")
                if not isinstance(cursor, str) or not cursor:
                    raise LinearMissingEndCursorError("linear_missing_end_cursor")
                after = cursor
                continue

            return issues

    def fetch_issue_states_by_ids(self, issue_ids: list[str]) -> list[Issue]:
        ids = list(dict.fromkeys(issue_ids))
        if not ids:
            return []

        issues: list[Issue] = []
        for offset in range(0, len(ids), ISSUE_PAGE_SIZE):
            batch = ids[offset : offset + ISSUE_PAGE_SIZE]
            body = self.graphql(
                ISSUES_BY_ID_QUERY,
                {
                    "ids": batch,
                    "first": len(batch),
                    "relationFirst": ISSUE_PAGE_SIZE,
                },
            )
            nodes = _decode_issue_nodes(body)
            issues.extend(normalize_issue(node) for node in nodes)

        requested_order = {issue_id: index for index, issue_id in enumerate(ids)}
        return sorted(issues, key=lambda issue: requested_order.get(issue.id, len(requested_order)))

    def fetch_issue_comments(self, issue_id: str) -> list[str]:
        body = self.graphql(ISSUE_COMMENTS_QUERY, {"issueId": issue_id, "first": 50})
        nodes = _nested(body, "data", "issue", "comments", "nodes")
        if not isinstance(nodes, list):
            return []
        comments: list[str] = []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            text = node.get("body")
            author = _nested(node, "user", "name") or "Unknown"
            if isinstance(text, str) and text.strip():
                comments.append(f"{author}: {text.strip()}")
        return comments

    def move_issue_to_state(self, issue_id: str, team_id: str | None, state_name: str) -> Issue:
        """Transition ``issue_id`` to ``state_name`` and return the refreshed Issue.

        Used as the best-effort claim guard (PRD §8.5). The state name is
        resolved to its team-scoped Linear state ID and cached per team_id; the
        first claim for a given team pays the resolution cost, subsequent
        claims hit cache.

        Caller MUST re-fetch the issue afterwards to verify ownership — Linear
        ``updateIssue`` is not a compare-and-set (two concurrent callers can
        both succeed), so the returned issue is the post-update state as
        reported by Linear, not a verified claim.
        """

        if not team_id:
            raise LinearWorkflowStateNotFoundError("issue_missing_team_id")
        state_id = self._resolve_workflow_state_id(team_id, state_name)
        body = self.graphql(
            ISSUE_UPDATE_STATE_MUTATION,
            {"issueId": issue_id, "stateId": state_id},
        )
        result = _nested(body, "data", "issueUpdate")
        if not isinstance(result, dict) or result.get("success") is not True:
            raise LinearIssueUpdateFailedError(
                f"issue_update_unsuccessful:{state_name}"
            )
        issue_node = result.get("issue")
        if not isinstance(issue_node, dict):
            raise LinearUnknownPayloadError("linear_unknown_payload")
        return normalize_issue(issue_node)

    def _resolve_workflow_state_id(self, team_id: str, state_name: str) -> str:
        states = self._workflow_state_cache.get(team_id)
        if states is None:
            states = self._fetch_workflow_state_map(team_id)
            self._workflow_state_cache[team_id] = states
        state_id = states.get(state_name) or states.get(state_name.strip())
        if not state_id:
            # Refresh once in case the state was created after the cache filled.
            states = self._fetch_workflow_state_map(team_id)
            self._workflow_state_cache[team_id] = states
            state_id = states.get(state_name) or states.get(state_name.strip())
        if not state_id:
            raise LinearWorkflowStateNotFoundError(
                f"workflow_state_not_found:{state_name}"
            )
        return state_id

    def _fetch_workflow_state_map(self, team_id: str) -> dict[str, str]:
        body = self.graphql(WORKFLOW_STATES_QUERY, {"teamId": team_id, "first": 100})
        nodes = _nested(body, "data", "team", "states", "nodes")
        if not isinstance(nodes, list):
            raise LinearUnknownPayloadError("linear_unknown_payload")
        mapping: dict[str, str] = {}
        for node in nodes:
            if not isinstance(node, dict):
                continue
            name = node.get("name")
            state_id = node.get("id")
            if isinstance(name, str) and isinstance(state_id, str):
                mapping[name] = state_id
        return mapping

    def graphql_raw(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        variables = variables or {}
        payload = {"query": query, "variables": variables}

        try:
            token = self.token_store.resolve_linear_token()
        except MissingLinearTokenError as exc:
            raise

        headers = {
            "Authorization": token,
            "Content-Type": "application/json",
        }

        try:
            response = self.transport(self.tracker.endpoint, payload, headers, NETWORK_TIMEOUT_SECONDS)
        except Exception as exc:  # noqa: BLE001 - transports may raise urllib/httpx/custom errors.
            message = redact_secret(exc, [token])
            raise LinearAPIRequestError(message) from exc

        if response.status != 200:
            body = redact_secret(response.body, [token])
            raise LinearAPIStatusError(f"status={response.status} body={body}")

        if not isinstance(response.body, dict):
            raise LinearUnknownPayloadError("linear_unknown_payload")

        if "errors" in response.body:
            return _redact_payload(response.body, [token])

        return response.body

    def graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        body = self.graphql_raw(query, variables)

        if "errors" in body:
            raise LinearGraphQLError(body["errors"])

        return body


def normalize_issue(raw: dict[str, Any]) -> Issue:
    return Issue(
        id=_required_string(raw.get("id"), "id"),
        identifier=_required_string(raw.get("identifier"), "identifier"),
        title=_required_string(raw.get("title"), "title"),
        description=_optional_string(raw.get("description")),
        priority=_priority(raw.get("priority")),
        state=_required_string(_nested(raw, "state", "name"), "state.name"),
        branch_name=_optional_string(raw.get("branchName")),
        url=_optional_string(raw.get("url")),
        labels=_labels(raw),
        blocked_by=_blockers(raw),
        created_at=_parse_datetime(raw.get("createdAt")),
        updated_at=_parse_datetime(raw.get("updatedAt")),
        team_id=_optional_string(_nested(raw, "team", "id")),
    )


def _decode_issue_page(body: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    issues = _nested(body, "data", "issues")
    if not isinstance(issues, dict):
        raise LinearUnknownPayloadError("linear_unknown_payload")

    nodes = issues.get("nodes")
    page_info = issues.get("pageInfo")
    if not isinstance(nodes, list) or not isinstance(page_info, dict):
        raise LinearUnknownPayloadError("linear_unknown_payload")

    return nodes, page_info


def _decode_issue_nodes(body: dict[str, Any]) -> list[dict[str, Any]]:
    issues = _nested(body, "data", "issues")
    if not isinstance(issues, dict):
        raise LinearUnknownPayloadError("linear_unknown_payload")

    nodes = issues.get("nodes")
    if not isinstance(nodes, list):
        raise LinearUnknownPayloadError("linear_unknown_payload")

    return nodes


def _urllib_transport(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: float) -> GraphQLResponse:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw_body = response.read().decode("utf-8")
            return GraphQLResponse(status=response.status, body=json.loads(raw_body))
    except urllib.error.HTTPError as exc:
        raw_body = exc.read().decode("utf-8", errors="replace")
        try:
            body: dict[str, Any] | str = json.loads(raw_body)
        except json.JSONDecodeError:
            body = raw_body
        return GraphQLResponse(status=exc.code, body=body)
    except urllib.error.URLError as exc:
        raise LinearAPIRequestError(str(exc)) from exc


def _required_string(value: Any, field: str) -> str:
    if isinstance(value, str) and value:
        return value
    raise LinearUnknownPayloadError(f"missing_issue_field:{field}")


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _nested(mapping: dict[str, Any], *keys: str) -> Any:
    current: Any = mapping
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _labels(raw: dict[str, Any]) -> tuple[str, ...]:
    nodes = _nested(raw, "labels", "nodes")
    if not isinstance(nodes, list):
        return ()

    labels = []
    for node in nodes:
        name = node.get("name") if isinstance(node, dict) else None
        if isinstance(name, str):
            labels.append(name.lower())
    return tuple(labels)


def _blockers(raw: dict[str, Any]) -> tuple[Blocker, ...]:
    nodes = _nested(raw, "inverseRelations", "nodes")
    if not isinstance(nodes, list):
        return ()

    blockers: list[Blocker] = []
    for relation in nodes:
        if not isinstance(relation, dict):
            continue
        relation_type = relation.get("type")
        blocker_issue = relation.get("issue")
        if not isinstance(relation_type, str) or not isinstance(blocker_issue, dict):
            continue
        if relation_type.strip().lower() != "blocks":
            continue
        blockers.append(
            Blocker(
                id=_optional_string(blocker_issue.get("id")),
                identifier=_optional_string(blocker_issue.get("identifier")),
                state=_optional_string(_nested(blocker_issue, "state", "name")),
            )
        )
    return tuple(blockers)


def _priority(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _redact_payload(value: Any, secrets: list[str | None]) -> Any:
    if isinstance(value, dict):
        return {key: _redact_payload(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_payload(item, secrets) for item in value]
    if isinstance(value, str):
        return redact_secret(value, secrets)
    return value
