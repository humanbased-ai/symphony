from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping

import yaml


DEFAULT_PRESET = "codex-safe"
DEFAULT_WORKFLOW_PATH = "WORKFLOW.md"
DEFAULT_ACTIVE_STATES = ("Todo", "In Progress")
DEFAULT_TERMINAL_STATES = ("Done", "Canceled", "Duplicate")
DEFAULT_RUNNER = "claude_code"

# Monorepo signal files. Presence of any one of these at the repo root flips
# detection from "single" to "monorepo" so the generated prompt can include
# the self-scoping preamble (IN-284).
_MONOREPO_SIGNAL_FILES = (
    "pnpm-workspace.yaml",
    "pnpm-workspace.yml",
    "nx.json",
    "lerna.json",
    "rush.json",
    "go.work",
    "turbo.json",
)

RepoMode = Literal["new", "monorepo", "single"]
DEFAULT_REPO_MODE: RepoMode = "single"


@dataclass(frozen=True)
class WorkflowPreset:
    name: str
    max_concurrent_agents: int
    max_turns: int
    approval_policy: str
    thread_sandbox: str
    polling_interval_ms: int


PRESETS: Mapping[str, WorkflowPreset] = {
    "codex-safe": WorkflowPreset(
        name="codex-safe",
        max_concurrent_agents=3,
        max_turns=20,
        approval_policy="never",
        thread_sandbox="workspace-write",
        polling_interval_ms=30_000,
    ),
    "codex-autonomous": WorkflowPreset(
        name="codex-autonomous",
        max_concurrent_agents=3,
        max_turns=30,
        approval_policy="never",
        thread_sandbox="workspace-write",
        polling_interval_ms=15_000,
    ),
    "review-only": WorkflowPreset(
        name="review-only",
        max_concurrent_agents=1,
        max_turns=12,
        approval_policy="on-request",
        thread_sandbox="read-only",
        polling_interval_ms=60_000,
    ),
}


@dataclass(frozen=True)
class InitConfig:
    project_slug: str
    preset: str = DEFAULT_PRESET
    active_states: tuple[str, ...] = DEFAULT_ACTIVE_STATES
    terminal_states: tuple[str, ...] = DEFAULT_TERMINAL_STATES
    workspace_root: str = "~/.symphony/workspaces"
    codex_command: str = "codex app-server"
    runner: str = DEFAULT_RUNNER
    github_org: str = ""
    github_repo: str = ""
    repo_mode: RepoMode = DEFAULT_REPO_MODE


class OnboardingError(ValueError):
    """Raised when CLI onboarding input cannot produce a workflow."""


def generate_workflow(config: InitConfig) -> str:
    preset = PRESETS.get(config.preset)
    if preset is None:
        raise OnboardingError(f"unknown_preset:{config.preset}")

    project_slug = _required(config.project_slug, "missing_project_slug")
    active_states = _states(config.active_states, "missing_active_states")
    terminal_states = _states(config.terminal_states, "missing_terminal_states")
    workspace_root = _required(config.workspace_root, "missing_workspace_root")
    codex_command = _required(config.codex_command, "missing_codex_command")

    runner = config.runner or DEFAULT_RUNNER

    front_matter: dict[str, object] = {
        "tracker": {
            "kind": "linear",
            "project_slug": project_slug,
            "active_states": list(active_states),
            "terminal_states": list(terminal_states),
        },
        "polling": {"interval_ms": preset.polling_interval_ms},
        "workspace": {
            "root": workspace_root,
            **({"repo_url": f"https://github.com/{config.github_org}/{config.github_repo}"} if config.github_org and config.github_repo else {}),
        },
        "agent": {
            "runner": runner,
            "max_concurrent_agents": preset.max_concurrent_agents,
            "max_turns": preset.max_turns,
        },
    }

    if config.github_org and config.github_repo:
        front_matter["github"] = {
            "token": "$GITHUB_TOKEN",
            "owner": config.github_org,
            "repo": config.github_repo,
        }

    if runner == "claude_code":
        prompt = (
            _CLAUDE_PR_PROMPT
            .replace("__GITHUB_ORG__", config.github_org or "YOUR_ORG")
            .replace("__GITHUB_REPO__", config.github_repo or "YOUR_REPO")
        )
    else:
        front_matter["codex"] = {
            "command": codex_command,
            "approval_policy": preset.approval_policy,
            "thread_sandbox": preset.thread_sandbox,
        }
        prompt = _CODEX_PROMPT

    preamble = _repo_mode_preamble(config.repo_mode, config.github_org, config.github_repo)
    if preamble:
        prompt = f"{preamble}\n\n{prompt}"

    return f"---\n{yaml.safe_dump(front_matter, sort_keys=False)}---\n\n{prompt}"


_MONOREPO_PREAMBLE = """\
## Monorepo scope (IN-284)

This repository is a monorepo. Before implementing, identify the smallest
subpackage that owns the change requested by the issue and treat that
directory as your working scope. Avoid touching unrelated workspaces in
the same commit. Reference the relevant subpackage paths in the PR body.
"""

_NEW_PROJECT_PREAMBLE = """\
## New project scope (IN-284)

No git remote is configured for this workspace. Before the dispatch flow
below can run, an operator must create the GitHub repository:

  gh repo create __GITHUB_ORG__/__GITHUB_REPO__ --public --source=. --remote=origin

Or replace the clone step in the prompt with the equivalent push-to-new-repo
sequence. Until a remote exists, the dispatch will fail at the clone step.
"""


def _repo_mode_preamble(mode: RepoMode, github_org: str, github_repo: str) -> str:
    if mode == "monorepo":
        return _MONOREPO_PREAMBLE
    if mode == "new":
        return (
            _NEW_PROJECT_PREAMBLE
            .replace("__GITHUB_ORG__", github_org or "YOUR_ORG")
            .replace("__GITHUB_REPO__", github_repo or "YOUR_REPO")
        )
    return ""


_CODEX_PROMPT = """You are working on Linear issue {{ issue.identifier }}.

Title: {{ issue.title }}
State: {{ issue.state }}
URL: {{ issue.url }}

Description:
{{ issue.description }}

Work only inside the provided workspace. Keep changes scoped to the issue.
When finished, report changed files and validation evidence. If the
linear_graphql tool is available, post meaningful progress back to Linear.
"""

_CLAUDE_PR_PROMPT = """\
You are working on Linear issue {{ issue.identifier }}.

Title: {{ issue.title }}
State: {{ issue.state }}
URL: {{ issue.url }}

Description:
{{ issue.description }}

{% if issue.comments %}
Review feedback — address each point before submitting:
{% for comment in issue.comments %}
- {{ comment }}
{% endfor %}
{% endif %}

## Instructions

1. Clone the repository (gh handles authentication — no token in the URL):
   gh repo clone __GITHUB_ORG__/__GITHUB_REPO__ .

2. Create a working branch:
   git checkout -b fix/{{ issue.identifier | lower }}

3. Implement the changes. Keep the scope to what the issue describes.

4. Push and open a PR:
   git push -u origin HEAD
   gh pr create --title "{{ issue.title }}" --body "Resolves {{ issue.url }}"

5. Post the PR URL as a comment on the Linear issue using LINEAR_API_KEY.

6. Update the Linear issue state to "In Review" (query workflow states first to get the
   state ID, then call issueUpdate with the state ID).

Use $GITHUB_TOKEN for git authentication and $LINEAR_API_KEY for all Linear API calls.
"""


def write_workflow(path: str | Path, content: str, *, overwrite: bool = False) -> Path:
    workflow_path = Path(path).expanduser()
    if workflow_path.exists() and not overwrite:
        raise OnboardingError("workflow_file_exists")
    workflow_path.parent.mkdir(parents=True, exist_ok=True)
    workflow_path.write_text(content, encoding="utf-8")
    return workflow_path.resolve()


def parse_state_list(raw: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    if raw is None:
        return default
    states = tuple(item.strip() for item in raw.split(",") if item.strip())
    return states or default


def default_workspace_root(project_slug: str) -> str:
    suffix = re.sub(r"[^a-zA-Z0-9_.-]+", "-", project_slug.strip()).strip("-")
    return f"~/.symphony/workspaces/{suffix or 'linear'}"


def detect_repo_shape(cwd: str | Path | None = None) -> RepoMode:
    """Auto-detect the shape of the repository at ``cwd`` (IN-284).

    Returns one of:

    * ``"new"`` — no git directory or no remote configured. Operator likely
      wants ``gh repo create`` flow; the workflow's clone step is replaced
      with a project-creation hint.
    * ``"monorepo"`` — root contains a recognized workspace signal file
      (pnpm-workspace.yaml, nx.json, lerna.json, rush.json, go.work,
      turbo.json) OR a top-level ``packages/`` directory OR an npm
      ``package.json`` declaring ``workspaces``. The generated prompt
      includes a self-scoping preamble.
    * ``"single"`` — has a git remote and no monorepo signals. The default.
    """

    root = Path(cwd).expanduser().resolve() if cwd is not None else Path.cwd().resolve()

    git_dir = root / ".git"
    if not git_dir.exists():
        return "new"

    try:
        result = subprocess.run(
            ["git", "-C", str(root), "remote"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        result = None
    if result is None or result.returncode != 0 or not result.stdout.strip():
        return "new"

    for signal in _MONOREPO_SIGNAL_FILES:
        if (root / signal).exists():
            return "monorepo"

    packages_dir = root / "packages"
    if packages_dir.is_dir() and any(packages_dir.iterdir()):
        return "monorepo"

    package_json = root / "package.json"
    if package_json.is_file():
        try:
            payload = json.loads(package_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict) and payload.get("workspaces"):
            return "monorepo"

    return "single"


def _required(value: str, code: str) -> str:
    trimmed = value.strip()
    if not trimmed:
        raise OnboardingError(code)
    return trimmed


def _states(values: tuple[str, ...], code: str) -> tuple[str, ...]:
    states = tuple(item.strip() for item in values if item.strip())
    if not states:
        raise OnboardingError(code)
    return states
