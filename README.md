# Symphony

> Turn Linear issues into isolated agent implementation runs.

Symphony is an agent orchestration system for teams that want to manage project
work in Linear instead of supervising one-off coding-agent chats. You write a
clear issue, move it into an active state, and Symphony prepares an isolated
workspace, runs the configured agent, and hands the result back through a pull
request and Linear updates.

This repository contains the Python CLI implementation of the language-agnostic
[Symphony specification](SPEC.md). The current working slice is built for local
operator use with Linear, GitHub, and Claude Code; Codex app-server is also
supported.

## Key Features

- **Issue-driven dispatch** — poll Linear, pick eligible tickets, run the agent,
  open a PR, close the loop with Linear comments and state transitions. Issues
  that already have an open PR are skipped automatically — no duplicate
  dispatches.
- **Per-run workspace isolation** — each dispatch gets its own
  `<root>/<issue>/<run_id>` directory. Optional git mode maintains a bare clone
  with `git worktree` per run and force-cleans the branch on completion.
- **Pluggable agent runners** — Claude Code and Codex app-server today;
  AgentRunner ABCs are in place for Gemini, Hermes/OpenAI-compatible, and
  GPT-Image-1.
- **Multi-instance safety** — best-effort state-transition claim before
  dispatch, blocker eligibility gate, structured `claim_*` events tagged with
  `host:pid` so duplicate dispatch is visible in logs.
- **Fail-closed approval gate** — `symphony doctor` hard-fails when the runner
  can request approval but no resolution path is configured. Approval requests
  are routed to a configured `approval_state` instead of looping.
- **Failure-state transition** — non-recoverable runs move to a configured
  `failure_state` and clean up the workspace; no auto-retry into a dirty tree.
- **WORKFLOW.md hot reload** — change YAML front matter or the prompt body and
  the daemon picks it up without restart.
- **Linear OAuth + webhooks** — PKCE flow, secure credential storage, webhook
  receiver with HMAC-SHA256 verification, polling fallback.
- **`linear_graphql` agent tool** — agents can read issues, post comments, and
  move state through Symphony-managed auth.
- **Terminal dashboard** — alt-screen UI showing live issue state, current PR
  URL, and CI check status per issue. REST API at `/api/v1/state`,
  `/api/v1/<issue>`, `/api/v1/refresh`, `/api/v1/health`.
- **PR feedback loop** — polls GitHub PR review comments and Linear issue
  comments each tick. Change-request feedback is routed to the existing PR
  branch; approve or close signals trigger the matching state transition.
- **CI auto-fix** — monitors check-runs on tracked PR branches; when new
  failures appear and no human comment was posted that tick, the agent is
  dispatched with the failure details to fix and re-push. CI status resets to
  open automatically when checks recover.
- **LLM feedback classification** — approve / change-request / close signals in
  Linear comments are classified by the Claude CLI, not just regex, so natural
  language feedback is reliably detected.
- **Colored console logging** — timestamps, log levels, and per-issue activity
  lines are color-coded in TTY sessions; file handler always writes plain text.

## Install

Pick one channel.

**Homebrew (macOS, recommended):**

```bash
brew install codatta/symphony/symphony
```

**pipx / uv / pip:**

```bash
pipx install symphony
# or: uv tool install symphony
```

Both channels register two entry points: `symphony` and the shorthand `sy`.

**From source:**

```bash
git clone https://github.com/codatta/symphony.git
cd symphony && uv sync
uv run symphony --help
```

Authenticate the supporting tools once:

```bash
# Claude Code (default runner)
npm install -g @anthropic-ai/claude-code && claude

# GitHub CLI (used by the agent to clone and push)
brew install gh && gh auth login

# Linear API key — create at linear.app/settings/api → Personal API keys
```

### Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `LINEAR_API_KEY` | Yes | Linear personal API key (`lin_api_…`) |
| `GITHUB_TOKEN` | Fallback | GitHub token if `gh auth` is unavailable |
| `ANTHROPIC_API_KEY` | claude_code runner | API key for Claude Code |
| `OPENAI_API_KEY` | codex runner | API key for Codex |
| `SYMPHONY_NTFY_TOPIC` | Optional | ntfy topic for push notifications |
| `SYMPHONY_WEBHOOK_URL` | Optional | Generic webhook for event notifications |

Variables can also be stored in the local credentials file written by
`symphony onboard` — you do not need to export them in every shell session.

## Quick Start

Run onboarding from the repository where you want `WORKFLOW.md` to live:

```bash
symphony onboard --project-slug your-linear-project-slug
# sy is a short alias for symphony
sy onboard --project-slug your-linear-project-slug
```

`symphony onboard` scans the local environment first, reports detected Linear /
GitHub auth and runner availability, asks only for the gaps, and writes
`WORKFLOW.md` + stores credentials under the local Symphony config path.

Validate the setup:

```bash
symphony doctor WORKFLOW.md
```

Run one controlled poll tick against a real Linear ticket:

```bash
symphony run WORKFLOW.md --once --log-level INFO
```

A successful run prints `Tick OK: fetched=1 dispatched=1 completed=1 failed=0 …`
and the agent opens a PR + posts the URL back to Linear. For continuous
operation, drop `--once` and add `--port 7337 --logs-root ./log`.

For scripted setup (no prompts):

```bash
symphony onboard --mode automated \
  --project-slug your-linear-project-slug \
  --linear-api-key lin_api_... \
  --github-token ghp_... \
  --github-org your-org \
  --github-repo your-repo
```

Available presets: `codex-safe`, `codex-autonomous`, `review-only`.

## Development Roadmap

| Phase | Status | Highlights |
|-------|--------|-----------|
| **0 — Guardrails** | 🟡 open | AGENTS.md tracking, PR template, LFS docs |
| **1 — MVP (Linear + Codex)** | 🟢 shipped | Python skeleton, WORKFLOW.md parser, Linear read path, `linear_graphql` tool, orchestration state machine, workspace lifecycle, Codex runner, status API |
| **1 — SPEC compliance** | 🟢 shipped | Per-run workspace isolation, blocker gate, fail-closed approval, failure-state transition, claim race prevention |
| **2A — CLI onboarding** | 🟢 mostly shipped | `init` / `doctor` / `run` / `onboard`, bilingual tutorial, presets, Homebrew tap, colored logging |
| **2B — Linear productionization** | 🟡 partial | OAuth/PKCE, webhooks, PR feedback loop, CI auto-fix, terminal dashboard. Remaining: Tauri desktop shell |
| **3 — Operator visibility** | ⚪ planned | SSE stream, web dashboard/PWA, mobile push, approval gate UI |
| **4 — Multi-agent runners** | ⚪ planned | Gemini API, OpenAI-compatible / Hermes, GPT-Image-1 |
| **5 — IM & distribution** | ⚪ planned | Telegram bot, Slack bot, marketplace channels |
| **6 — Backlog** | ⚪ planned | GitHub Issues / Jira adapters, Docker sandboxing, persistent retry queue |

See [prd.md](prd.md) §7 for the full build queue with ticket links, and
[CHANGELOG.md](CHANGELOG.md) for user-facing changes.

## Operating Symphony

### Doctor checks

`symphony doctor WORKFLOW.md` validates: workflow parse, Linear auth source,
runner command, `gh auth`, GitHub token, workspace root writability, logs root,
status API port, claim guard (warn), approval gate (hard fail when
misconfigured), failure state (warn).

### Day-to-day

```bash
symphony run WORKFLOW.md --port 7337 --logs-root ./log --log-level INFO
```

Stop with `Ctrl-C`. Status API: `http://127.0.0.1:7337/api/v1/state`.

### Terminal dashboard

When `--port` is set, Symphony exposes a live alt-screen dashboard and a REST
API alongside the daemon:

```
http://127.0.0.1:7337/api/v1/state        # all tracked issues
http://127.0.0.1:7337/api/v1/<issue-id>   # single issue detail
http://127.0.0.1:7337/api/v1/refresh      # force a poll tick
http://127.0.0.1:7337/api/v1/health       # liveness check
```

The terminal UI shows each issue's current state, the open PR URL, and live
CI check status. Omit `--port` to run headless with log output only.

### PR feedback loop

Once the agent opens a PR, Symphony keeps polling both the GitHub PR review
comments and the Linear issue comments on every tick. You do not need to
restart the daemon after leaving review feedback.

- **Change-request** — post a review comment on the GitHub PR (or a comment
  on the Linear issue) describing what to fix. Symphony classifies the signal
  via the Claude CLI and re-dispatches the agent to the same branch.
- **Approved** — Symphony moves the issue to the configured handoff state.
- **Closed** — Symphony treats the PR close as a terminal signal and
  transitions the issue accordingly.

### CI auto-fix

Symphony monitors check-runs on each tracked PR branch. When new CI failures
appear and no human comment was posted in the same tick, Symphony dispatches
the agent with the failure details so it can push a fix automatically. Once
checks recover, the CI status resets and polling resumes normally.

Good fits: scoped implementation tickets, docs/cleanup tasks, review follow-up
where feedback lands on the Linear issue. Avoid: secret rotation, broad
refactors, high-risk production changes, repos where agent PRs are unsafe.

### Webhooks (optional)

Webhooks let Linear push state changes to Symphony instantly instead of
waiting for the next poll tick. Add these fields to `WORKFLOW.md` and expose
a public URL (or a local tunnel):

```yaml
tracker:
  webhook_secret: $LINEAR_WEBHOOK_SECRET   # set in Linear webhook settings
server:
  public_url: $SYMPHONY_PUBLIC_URL         # e.g. https://symphony.yourteam.com
  tunnel: none                             # none | cloudflared | ngrok
```

When webhooks are active you can raise `polling.interval_ms` to `120000`
(2 min) — webhooks handle the fast path and polling acts as a safety net.
Without a public URL, polling-only mode works fine for local use.

### Multiple projects

Each `WORKFLOW.md` targets one Linear project. Run one process per project on
different ports:

```bash
symphony run project-a/WORKFLOW.md --port 7337 --logs-root ./log/a
symphony run project-b/WORKFLOW.md --port 7338 --logs-root ./log/b
```

Stop with `pkill -f "symphony run"` or `kill <PID>`. Update
`tracker.project_slug` in a watched `WORKFLOW.md` to hot-switch the running
daemon.

### Quick training ticket

Before running against real work, create one disposable Linear ticket like:

```text
Title: Add a CLI smoke-test note to README
Description:
  Repository: https://github.com/your-org/your-repo
  Add a short paragraph to README.md about the smoke test. Keep it docs-only.
  Open a PR and post the URL back here.
Acceptance criteria:
  - README.md contains the new note.
  - A GitHub PR is opened.
  - The PR URL is commented on this Linear issue.
```

Move it to `Todo`, run `symphony run WORKFLOW.md --once --log-level INFO`,
inspect the workspace and PR. For the review loop, post a revision request as
a GitHub PR review comment or Linear issue comment — Symphony polls both each
tick and automatically dispatches the agent to address the feedback.

## Troubleshooting

**Symphony starts but never dispatches an issue**
- Check `symphony doctor WORKFLOW.md` — the most common cause is a missing or
  invalid `LINEAR_API_KEY`.
- Confirm the issue is in one of the states listed in `active_states`.
- If an open PR already exists for the issue, Symphony skips it by design.
  Close or merge the PR first.

**Agent runs but does not open a PR**
- Verify `gh auth status` shows the correct account with repo write access.
- Check `--logs-root` for the per-issue log file — it usually contains the
  exact error from the agent.

**Workspace is dirty after a failed run**
- Set `keep_on_failure: true` in WORKFLOW.md to preserve the workspace for
  inspection, then clean it up manually.
- Stale worktrees from crashed runs are swept automatically on the next daemon
  start.

**`symphony doctor` reports a fatal approval-gate error**
- This means `approval_policy: on-request` is set but no `approval_state` is
  configured. Either add `approval_state` or change the policy to `never`.

**Hot reload is not picking up WORKFLOW.md changes**
- Only YAML front matter and the prompt body are reloaded. Changing the runner
  binary or workspace root requires a daemon restart.

## WORKFLOW.md Basics

`WORKFLOW.md` is the team-owned runtime contract: YAML front matter for
configuration, Markdown body rendered as the per-issue prompt.

Minimal Claude Code example:

```yaml
---
tracker:
  kind: linear
  project_slug: "your-linear-project-slug"
  active_states: [Todo, In Progress]
  terminal_states: [Done, Canceled, Duplicate]

workspace:
  root: ~/.symphony/workspaces/your-project
  # Optional git mode — Symphony manages bare clone + worktree per run:
  # repo_url: https://github.com/your-org/your-repo
  # default_branch: main

agent:
  runner: claude_code
  max_concurrent_agents: 1
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

Switch runners by changing `agent.runner` to `codex`. Add `hooks.before_run`,
`hooks.after_run`, `hooks.before_remove` for per-workspace setup. See
[SPEC.md](SPEC.md) for the full schema.

## Local Development

```bash
git clone https://github.com/codatta/symphony.git
cd symphony && uv sync

uv run symphony --help
uv run python -m unittest discover -s tests -p 'test_*.py'
```

Build a wheel and smoke-test the installed CLI:

```bash
uv build
uv tool install --force ./dist/symphony-*.whl
symphony --help
```

Release automation lives in `.github/workflows/release.yml`; the
`workflow_dispatch` path supports `channel=dry-run` / `staging` / `main`. The
Homebrew tap (`codatta/symphony`) auto-updates on tagged releases via
`.github/workflows/homebrew-tap.yml`.

Native single-file binaries are out of scope for the current packaging slice.

## Learn More

- [SPEC.md](SPEC.md) — language-agnostic Symphony service specification.
- [ARCHITECTURE.md](ARCHITECTURE.md) — Python architecture, runtime model, and
  planned desktop / IM surfaces.
- [prd.md](prd.md) — product requirements and full build queue.
- [CHANGELOG.md](CHANGELOG.md) — user-facing changes per release.
- [test-plan-epic-2.md](test-plan-epic-2.md) — live-dispatch validation plan.

## Attribution

Symphony and its specification were created by OpenAI and are licensed under the
[Apache License 2.0](LICENSE). This repository is an independent implementation
of that specification. The original project is at
[github.com/openai/symphony](https://github.com/openai/symphony).
