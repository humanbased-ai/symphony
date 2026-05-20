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
  open a PR, close the loop with Linear comments and state transitions.
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
- **Status API + dashboard** — `/api/v1/state`, `/api/v1/<issue>`,
  `/api/v1/refresh`, `/api/v1/health`.

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

## Quick Start

Run onboarding from the repository where you want `WORKFLOW.md` to live:

```bash
symphony onboard --project-slug your-linear-project-slug
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
| **1 — MVP (Linear + Codex)** | 🟢 shipped | Python skeleton, WORKFLOW.md parser, Linear read path, `linear_graphql` tool, orchestration state machine, workspace lifecycle, agent runner ABCs, Codex runner, status API |
| **1 — SPEC compliance follow-ups** | 🟢 in review | Per-run workspace isolation (IN-286), blocker gate (IN-287), fail-closed approval (IN-288), failure-state transition (IN-289), claim race prevention (IN-290) — PRs #34–#38 |
| **2A — CLI onboarding & packaging** | 🟢 mostly shipped | `init` / `doctor` / `run` / `onboard`, bilingual tutorial, presets, Homebrew tap. Remaining: env-first onboard redesign (IN-283), repo-shape auto-detect (IN-284), runner picker + cross-vendor review (IN-285) |
| **2B — Standalone app & Linear productionization** | 🟡 partial | Shipped: Linear OAuth/PKCE, webhooks, credential storage, Homebrew. Remaining: Tauri desktop shell, setup flow, app status view |
| **3 — Operator visibility & approval** | ⚪ planned | SSE event stream, web dashboard/PWA, mobile push, approval gate UI |
| **4 — Multi-agent runners** | ⚪ planned | Claude Code (shipped as MVP), Gemini API, OpenAI-compatible / Hermes, GPT-Image-1 |
| **5 — IM integrations & distribution** | ⚪ planned | Telegram bot, Slack bot, marketplace channels |
| **6 — Backlog / expansion** | ⚪ planned | GitHub Issues / Jira adapters, SSH worker (SPEC Appendix A), Docker/cgroup sandboxing, persistent retry queue, multimodal vision input |

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

Good fits: scoped implementation tickets, docs/cleanup tasks, review follow-up
where feedback lands on the Linear issue. Avoid: secret rotation, broad
refactors, high-risk production changes, repos where agent PRs are unsafe.

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
inspect the workspace and PR. For the review loop, post a revision request as a
Linear comment, move the issue back to an active state, run another tick.

Current limitation: GitHub PR review comments are not auto-read. Copy revision
instructions to the Linear issue and re-activate manually.

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
