"""GitHub REST API client — post PR comments and fetch diffs."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


GITHUB_API_BASE = "https://api.github.com"


class GitHubClientError(RuntimeError):
    pass


@dataclass(frozen=True)
class GitHubClient:
    token: str
    owner: str
    repo: str

    def post_pr_comment(self, pr_number: int, body: str) -> bool:
        """Post a comment on a PR's issue thread. Returns True on success."""
        try:
            self._request(
                "POST",
                f"/repos/{self.owner}/{self.repo}/issues/{pr_number}/comments",
                {"body": body},
            )
            return True
        except GitHubClientError:
            return False

    def get_pr_diff(self, pr_number: int) -> str:
        """Return the unified diff for a PR. Returns empty string on failure."""
        try:
            return self._request_text(
                "GET",
                f"/repos/{self.owner}/{self.repo}/pulls/{pr_number}",
                accept="application/vnd.github.diff",
            )
        except GitHubClientError:
            return ""

    # ------------------------------------------------------------------

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"{GITHUB_API_BASE}{path}",
            data=data,
            headers=self._headers(),
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            raise GitHubClientError(f"github_http_error:{exc.code}") from exc
        except urllib.error.URLError as exc:
            raise GitHubClientError(f"github_url_error:{exc}") from exc

    def _request_text(self, method: str, path: str, *, accept: str) -> str:
        headers = {**self._headers(), "Accept": accept}
        req = urllib.request.Request(f"{GITHUB_API_BASE}{path}", headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raise GitHubClientError(f"github_http_error:{exc.code}") from exc
        except urllib.error.URLError as exc:
            raise GitHubClientError(f"github_url_error:{exc}") from exc

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
