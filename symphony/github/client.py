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

    def get_pr(self, pr_number: int) -> dict[str, Any] | None:
        """Return PR data (state, merged, etc.) or None on failure."""
        try:
            return self._request("GET", f"/repos/{self.owner}/{self.repo}/pulls/{pr_number}")
        except GitHubClientError:
            return None

    def find_open_pr_for_branch(self, branch: str) -> int | None:
        """Return the PR number of the first open PR with the given head branch, or None."""
        try:
            result = self._request(
                "GET",
                f"/repos/{self.owner}/{self.repo}/pulls"
                f"?head={self.owner}:{branch}&state=open&per_page=1",
            )
            if isinstance(result, list) and result:
                return int(result[0]["number"])
            return None
        except (GitHubClientError, KeyError, TypeError, ValueError):
            return None

    def list_pr_review_comments(self, pr_number: int) -> list[dict]:
        """List inline review comments (pull_request_review_comment) on a PR."""
        try:
            result = self._request(
                "GET",
                f"/repos/{self.owner}/{self.repo}/pulls/{pr_number}/comments?per_page=100",
            )
            return result if isinstance(result, list) else []
        except GitHubClientError:
            return []

    def list_pr_issue_comments(self, pr_number: int) -> list[dict]:
        """List general PR comments (issue-level) on a PR."""
        try:
            result = self._request(
                "GET",
                f"/repos/{self.owner}/{self.repo}/issues/{pr_number}/comments?per_page=100",
            )
            return result if isinstance(result, list) else []
        except GitHubClientError:
            return []

    def list_pr_reviews(self, pr_number: int) -> list[dict]:
        """List review submissions on a PR."""
        try:
            result = self._request(
                "GET",
                f"/repos/{self.owner}/{self.repo}/pulls/{pr_number}/reviews?per_page=100",
            )
            return result if isinstance(result, list) else []
        except GitHubClientError:
            return []

    def get_authenticated_login(self) -> str | None:
        """Return the GitHub login for the current token, or None on failure."""
        try:
            result = self._request("GET", "/user")
            login = result.get("login")
            return str(login) if login else None
        except GitHubClientError:
            return None

    def list_webhooks(self) -> list[dict]:
        """List all webhooks registered for the repository."""
        result = self._request("GET", f"/repos/{self.owner}/{self.repo}/hooks")
        if isinstance(result, list):
            return result
        return []

    def delete_webhook(self, webhook_id: int) -> None:
        """Delete a webhook by its ID."""
        try:
            self._request("DELETE", f"/repos/{self.owner}/{self.repo}/hooks/{webhook_id}")
        except GitHubClientError as exc:
            # 404 means it's already gone — treat as success
            if "github_http_error:404" in str(exc):
                return
            raise

    def register_webhook(
        self,
        url: str,
        secret: str,
        events: list[str] | None = None,
    ) -> str:
        """Register a webhook, returning the webhook id as a string.

        If a webhook with the same URL already exists it is deleted first so
        the secret is guaranteed to be up-to-date.
        """
        if events is None:
            events = ["pull_request", "pull_request_review", "pull_request_review_comment"]

        # Remove any existing webhook pointing at the same URL
        try:
            existing = self.list_webhooks()
            for hook in existing:
                config_block = hook.get("config") or {}
                if config_block.get("url") == url:
                    self.delete_webhook(int(hook["id"]))
        except GitHubClientError:
            pass

        payload = {
            "name": "web",
            "active": True,
            "events": events,
            "config": {
                "url": url,
                "content_type": "json",
                "secret": secret,
                "insecure_ssl": "0",
            },
        }
        result = self._request("POST", f"/repos/{self.owner}/{self.repo}/hooks", payload)
        return str(result["id"])

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
