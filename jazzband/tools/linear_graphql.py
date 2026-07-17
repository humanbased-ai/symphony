from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from jazzband.auth import MissingLinearTokenError
from jazzband.tracker.linear import LinearClient, LinearClientError


LINEAR_GRAPHQL_TOOL_NAME = "linear_graphql"


@dataclass(frozen=True)
class LinearGraphQLTool:
    client: LinearClient

    @property
    def name(self) -> str:
        return LINEAR_GRAPHQL_TOOL_NAME

    def run(self, tool_input: Any) -> dict[str, Any]:
        try:
            query, variables = _parse_input(tool_input)
            _validate_single_operation(query)
        except LinearGraphQLToolInputError as exc:
            return _failure("invalid_input", str(exc))

        try:
            response = self.client.graphql_raw(query, variables)
        except MissingLinearTokenError as exc:
            return _failure("missing_tracker_api_key", str(exc))
        except LinearClientError as exc:
            return _failure(exc.code, str(exc))

        if "errors" in response:
            return {
                "success": False,
                "error": {
                    "code": "linear_graphql_errors",
                    "message": "Linear GraphQL returned errors.",
                },
                "response": response,
            }

        return {"success": True, "response": response}


def linear_graphql_tool(client: LinearClient, tool_input: Any) -> dict[str, Any]:
    return LinearGraphQLTool(client).run(tool_input)


class LinearGraphQLToolInputError(ValueError):
    pass


def _parse_input(tool_input: Any) -> tuple[str, dict[str, Any]]:
    if isinstance(tool_input, str):
        query = tool_input.strip()
        if not query:
            raise LinearGraphQLToolInputError("query_must_be_non_empty_string")
        return query, {}

    if not isinstance(tool_input, Mapping):
        raise LinearGraphQLToolInputError("input_must_be_object_or_query_string")

    raw_query = tool_input.get("query")
    if not isinstance(raw_query, str) or not raw_query.strip():
        raise LinearGraphQLToolInputError("query_must_be_non_empty_string")

    raw_variables = tool_input.get("variables", {})
    if raw_variables is None:
        raw_variables = {}
    if not isinstance(raw_variables, Mapping):
        raise LinearGraphQLToolInputError("variables_must_be_object")

    return raw_query.strip(), dict(raw_variables)


def _validate_single_operation(query: str) -> None:
    operation_count = _count_graphql_operations(query)
    if operation_count != 1:
        raise LinearGraphQLToolInputError("query_must_contain_exactly_one_operation")


def _count_graphql_operations(query: str) -> int:
    tokens = list(_graphql_tokens(query))
    if not tokens:
        return 0

    operation_count = 0
    depth = 0
    seen_significant_token = False

    for token in tokens:
        if token == "{":
            if not seen_significant_token:
                operation_count += 1
            seen_significant_token = True
            depth += 1
            continue

        if token == "}":
            seen_significant_token = True
            depth = max(depth - 1, 0)
            if depth == 0:
                seen_significant_token = False
            continue

        seen_significant_token = True
        if depth == 0 and token in {"query", "mutation", "subscription"}:
            operation_count += 1

    return operation_count


def _graphql_tokens(query: str):
    index = 0
    length = len(query)

    while index < length:
        char = query[index]

        if char.isspace() or char == ",":
            index += 1
            continue

        if char == "#":
            index = _skip_line_comment(query, index + 1)
            continue

        if query.startswith('"""', index):
            index = _skip_block_string(query, index + 3)
            continue

        if char == '"':
            index = _skip_string(query, index + 1)
            continue

        if char in "{}":
            yield char
            index += 1
            continue

        if char.isalpha() or char == "_":
            start = index
            index += 1
            while index < length and (query[index].isalnum() or query[index] == "_"):
                index += 1
            yield query[start:index]
            continue

        index += 1


def _skip_line_comment(query: str, index: int) -> int:
    while index < len(query) and query[index] not in "\r\n":
        index += 1
    return index


def _skip_string(query: str, index: int) -> int:
    escaped = False
    while index < len(query):
        char = query[index]
        index += 1
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"':
            break
    return index


def _skip_block_string(query: str, index: int) -> int:
    while index < len(query):
        if query.startswith('\\"""', index):
            index += 4
            continue
        if query.startswith('"""', index):
            return index + 3
        index += 1
    return len(query)


def _failure(code: str, message: str) -> dict[str, Any]:
    return {
        "success": False,
        "error": {
            "code": code,
            "message": message,
        },
    }
