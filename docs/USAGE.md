# Jazzband Usage Guide

Jazzband turns Linear issues into isolated AI agent implementation runs. You
write a clear issue, move it to an active state, and Jazzband dispatches an
agent to implement it — opening a PR and posting the URL back to Linear when
done.

## Contents

- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [First-time setup](#first-time-setup)
- [Validating your setup](#validating-your-setup)
- [Running Jazzband](#running-jazzband)
- [Day-to-day workflow](#day-to-day-workflow)
- [PR feedback loop](#pr-feedback-loop)
- [CI auto-fix](#ci-auto-fix)
- [Terminal dashboard](#terminal-dashboard)
- [Webhooks](#webhooks-optional)
- [Multiple projects](#multiple-projects)
- [WORKFLOW.md reference](#workflowmd-reference)
- [Environment variables](#environment-variables)
- [Good and bad fits](#good-and-bad-fits)
- [Your first test issue](#your-first-test-issue)
- [Updating Jazzband](#updating-jazzband)
- [Troubleshooting](#troubleshooting)
- [Command reference](#command-reference)

---

## Prerequisites

Install and authenticate these tools before running Jazzband.

```bash
# Claude Code — default AI runner
npm install -g @anthropic-ai/claude-code
claude                   # completes the login flow

# GitHub CLI — used by the agent to clone, branch, and push
brew install gh
gh auth login

# Linear API key
# Create one at: linear.app/settings/api → Personal API keys
```

---

## Installation

**pipx** (recommended for most users):

```bash
brew install pipx        # if pipx is not installed
pipx install jazzband
```

**uv**:

```bash
brew install uv          # if uv is not installed
uv tool install jazzband
```

**From source**:

```bash
git clone https://github.com/humanbased-ai/jazzband.git
cd jazzband && uv sync
uv run jazzband --help
```

Both `jazzband` and the shorthand `sy` are registered as entry points.

---

## First-time setup

Run onboarding from the root of the repository where you want `WORKFLOW.md`
to live:

```bash
jazzband onboard --project-slug your-linear-project-slug
```

`jazzband onboard` scans your environment first — it detects existing Linear,
GitHub, and runner authentication and only asks for what is missing. When it
finishes it writes `WORKFLOW.md` and stores credentials in the local Jazzband
config directory.

**Scripted / non-interactive setup:**

```bash
jazzband onboard --mode automated \
  --project-slug your-linear-project-slug \
  --linear-api-key lin_api_... \
  --github-token ghp_... \
  --github-org your-org \
  --github-repo your-repo
```

Available workflow presets: `codex-safe`, `codex-autonomous`, `review-only`.

---

## Validating your setup

```bash
jazzband doctor WORKFLOW.md
```

Doctor checks: Linear auth source, runner command availability, `gh auth`
status, GitHub repository access, workspace root writability, logs root,
status API port, claim guard configuration, and approval gate configuration.

All checks must pass (green) before running a live dispatch. Red items include
the exact command needed to fix them.

---

## Running Jazzband

**Single tick** — run once and exit. Use this to verify everything works before
running continuously:

```bash
jazzband run WORKFLOW.md --once --log-level INFO
```

A successful run prints:

```
Tick OK: fetched=1 dispatched=1 completed=1 failed=0 ...
```

The agent opens a PR and posts the URL back to the Linear issue.

**Continuous daemon** — runs until stopped:

```bash
jazzband run WORKFLOW.md --port 7337 --logs-root ./log --log-level INFO
```

Stop with `Ctrl-C`.

---

## Day-to-day workflow

```
1. Write a clear Linear issue with a concrete description and acceptance criteria
2. Move the issue to Todo (or any state listed in active_states)
3. Jazzband polls Linear, creates an isolated workspace, and dispatches the agent
4. The agent implements the issue, opens a PR, and posts the PR URL to Linear
5. You review the PR on GitHub
6. Leave review feedback if changes are needed (see PR feedback loop below)
```

Jazzband skips issues that already have an open PR — no duplicate dispatches.

---

## PR feedback loop

After the agent opens a PR, Jazzband continues polling both the GitHub PR
review comments and the Linear issue comments on every tick.

| Signal | Where to post | What Jazzband does |
|--------|--------------|-------------------|
| Change request | GitHub PR review comment or Linear issue comment | Re-dispatches the agent to the same branch to address the feedback |
| Approved | GitHub PR approval | Moves the issue to the configured handoff state |
| Closed | GitHub PR closed without merge | Moves the issue to the configured terminal state |

Jazzband uses the Claude CLI to classify feedback — natural language comments
are reliably detected, not just structured commands.

---

## CI auto-fix

Jazzband monitors CI check-runs on each tracked PR branch. When new failures
appear and no human comment was posted in the same poll tick, Jazzband
automatically dispatches the agent with the failure details. The agent pushes a
fix and CI re-runs. Once checks recover, the CI status resets and polling
resumes normally.

---

## Terminal dashboard

When `--port` is set, Jazzband exposes a live REST API alongside the daemon:

```
GET /api/v1/state          — all tracked issues and their current state
GET /api/v1/<issue-id>     — single issue detail, PR URL, CI status
POST /api/v1/refresh       — force an immediate poll tick
GET /api/v1/health         — liveness check
```

Example:

```bash
curl http://127.0.0.1:7337/api/v1/state | jq
```

Omit `--port` to run headless with log output only.

---

## Webhooks (optional)

Webhooks let Linear push state changes to Jazzband instantly instead of waiting
for the next poll tick. Add these fields to `WORKFLOW.md` and expose a public
URL or configure a local tunnel:

```yaml
tracker:
  webhook_secret: $LINEAR_WEBHOOK_SECRET   # set this in Linear's webhook settings

server:
  public_url: $JAZZBAND_PUBLIC_URL         # e.g. https://jazzband.yourteam.com
  tunnel: none                             # none | cloudflared | ngrok
```

When webhooks are active you can safely raise `polling.interval_ms` to
`120000` (2 minutes) — webhooks handle the fast path and polling is a safety
net. Without a public URL, polling-only mode works fine for local use.

---

## Multiple projects

Each `WORKFLOW.md` targets one Linear project. Run one daemon per project on
different ports:

```bash
jazzband run project-a/WORKFLOW.md --port 7337 --logs-root ./log/a
jazzband run project-b/WORKFLOW.md --port 7338 --logs-root ./log/b
```

Stop all daemons: `pkill -f "jazzband run"`

Hot-switch the active project by updating `tracker.project_slug` in a watched
`WORKFLOW.md` — the daemon reloads without restart.

---

## WORKFLOW.md reference

`WORKFLOW.md` is the team-owned runtime contract: YAML front matter for
configuration, Markdown body rendered as the per-issue prompt.

```yaml
---
tracker:
  kind: linear
  project_slug: "your-project-slug"
  active_states: [Todo, In Progress]     # states that trigger dispatch
  terminal_states: [Done, Canceled]      # states that end tracking

workspace:
  root: ~/.jazzband/workspaces/your-project
  # Git isolation mode (recommended for production):
  # repo_url: https://github.com/your-org/your-repo
  # default_branch: main

agent:
  runner: claude_code          # or: codex
  max_concurrent_agents: 1     # max issues dispatched simultaneously
  max_turns: 20

claude_code:
  model: claude-sonnet-4-6
---

You are working on Linear issue {{ issue.identifier }}.

Title: {{ issue.title }}
URL: {{ issue.url }}

Description:
{{ issue.description }}

Implement only what the issue asks for. Open a pull request and post the PR URL
back to Linear when finished.
```

`WORKFLOW.md` changes are hot-reloaded — no daemon restart needed.

See [SPEC.md](../SPEC.md) for the full configuration schema.

---

## Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `LINEAR_API_KEY` | Yes | Linear personal API key (`lin_api_…`) |
| `GITHUB_TOKEN` | Fallback | GitHub token if `gh auth` is unavailable |
| `ANTHROPIC_API_KEY` | claude_code runner | API key for Claude Code |
| `OPENAI_API_KEY` | codex runner | API key for Codex |
| `LINEAR_WEBHOOK_SECRET` | Webhooks only | HMAC secret from Linear webhook settings |
| `JAZZBAND_PUBLIC_URL` | Webhooks only | Publicly reachable URL for the Jazzband server |
| `JAZZBAND_NTFY_TOPIC` | Optional | ntfy topic for push notifications |
| `JAZZBAND_WEBHOOK_URL` | Optional | Generic webhook URL for event notifications |

Variables can also be stored in the credentials file written by
`jazzband onboard` — you do not need to export them in every shell session.

---

## Good and bad fits

| Good fits | Avoid |
|-----------|-------|
| Scoped feature implementation | Secret rotation or permission changes |
| Docs and comment cleanup | Large-scale refactors |
| Review follow-up (feedback on Linear issue) | High-risk production changes |
| CI failure fixes | Repositories where agent-opened PRs are unsafe |

---

## Your first test issue

Before running Jazzband against real work, create a low-stakes Linear issue to
verify the full loop:

```
Title: Add a smoke-test note to README

Description:
  Repository: https://github.com/your-org/your-repo
  Add a short paragraph to README.md about the smoke test. Docs-only change.
  Open a PR and post the URL back to this issue.

Acceptance criteria:
  - README.md contains the new paragraph
  - A GitHub PR is opened
  - The PR URL is commented on this Linear issue
```

Move it to `Todo`, then run:

```bash
jazzband run WORKFLOW.md --once --log-level INFO
```

Inspect the workspace under `workspace.root` and the PR opened by the agent.
To test the review loop, post a change-request comment on the GitHub PR or the
Linear issue — Jazzband will classify it and re-dispatch automatically.

---

## Updating Jazzband

| Install method | Update command |
|---------------|---------------|
| pipx | `pipx upgrade jazzband` |
| uv | `uv tool upgrade jazzband` |
| Homebrew | `brew upgrade jazzband` |
| From source | `git pull && uv sync` |

---

## Troubleshooting

**Jazzband starts but never dispatches an issue**
- Run `jazzband doctor WORKFLOW.md` — the most common cause is a missing or
  invalid `LINEAR_API_KEY`.
- Confirm the issue is in one of the states listed in `active_states`.
- If an open PR already exists for the issue, Jazzband skips it by design.
  Close or merge the existing PR first.

**Agent runs but does not open a PR**
- Verify `gh auth status` shows the correct account with repository write
  access.
- Check the per-issue log file under `--logs-root` — it contains the exact
  output from the agent.

**Workspace is dirty after a failed run**
- Add `keep_on_failure: true` to `WORKFLOW.md` to preserve the workspace for
  inspection, then clean it up manually.
- Stale worktrees from crashed runs are swept automatically on the next daemon
  start.

**`jazzband doctor` reports a fatal approval-gate error**
- `approval_policy: on-request` is set but no `approval_state` is configured.
  Either add `approval_state` or change the policy to `never`.

**Hot reload is not picking up WORKFLOW.md changes**
- Only YAML front matter and the prompt body are reloaded. Changing the runner
  binary or workspace root requires a daemon restart.

---

## Command reference

| Command | Description |
|---------|-------------|
| `sy onboard --project-slug <slug>` | First-time setup wizard |
| `sy onboard --mode automated ...` | Non-interactive setup for CI or scripting |
| `sy doctor WORKFLOW.md` | Validate configuration and auth before running |
| `sy run WORKFLOW.md --once` | Run a single poll tick and exit |
| `sy run WORKFLOW.md --port 7337` | Start the continuous daemon with status API |
| `sy init` | Generate a WORKFLOW.md from a preset |
| `sy --version` | Print the installed version |
| `sy --help` | Show all commands and flags |
