# Symphony

> Turn Linear issues into isolated agent implementation runs.

Symphony is an agent orchestration system for teams that want to manage project
work in Linear instead of supervising one-off coding-agent chats. You write a
clear issue, move it into an active state, and Symphony prepares an isolated
workspace, runs the configured agent, and hands the result back through a pull
request and Linear updates.

This repository contains the Python CLI implementation of the language-agnostic
[Symphony specification](SPEC.md). The current working slice is built for local
operator use with Linear, GitHub, and Claude Code. Codex app-server support is
also available.

## Why Use It

- Keep agent work scoped to reviewable Linear tickets.
- Run implementation attempts in deterministic per-issue workspaces.
- Version orchestration policy in each repository with `WORKFLOW.md`.
- Use one repeatable loop for dispatch, review feedback, retries, and handoff.
- Train teammates on the workflow with a disposable ticket before using it on
  real project work.

Symphony is useful when a task is clear enough to become an issue and important
enough to deserve a pull request review.

## Current Status

The CLI MVP can:

- generate a starter `WORKFLOW.md` with `symphony init`;
- store local Linear and GitHub credentials;
- validate setup with `symphony doctor`;
- poll Linear for active issues;
- create isolated per-issue workspaces;
- run Claude Code or Codex;
- let agents open PRs and report progress back to Linear.

It is not production-ready yet. Desktop packaging, Linear OAuth, webhooks, the
full dashboard, and IM approval flows are planned follow-on work. See
[prd.md](prd.md) for the product plan and [ARCHITECTURE.md](ARCHITECTURE.md) for
the deeper system design.

## Requirements

- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/) for the recommended install path
- A Linear API key with access to the target project
- [`gh`](https://cli.github.com/) authenticated for the target GitHub repo
- Claude Code installed and authenticated for the default runner

Install and authenticate Claude Code:

```bash
npm install -g @anthropic-ai/claude-code
claude
```

Install and authenticate GitHub CLI:

```bash
brew install gh
gh auth login
```

Create a Linear API key at `linear.app/settings/api` under Personal API keys.

## Install

Install the CLI from GitHub:

```bash
uv tool install git+https://github.com/codatta/symphony.git
```

Install a specific branch while testing a PR:

```bash
uv tool install git+https://github.com/codatta/symphony.git@branch-name
```

Install from a local checkout:

```bash
git clone https://github.com/codatta/symphony.git
cd symphony
uv tool install .
```

Verify the command:

```bash
symphony --help
symphony --version
symphony onboard --help
symphony init --help
symphony doctor --help
symphony run --help
```

Upgrade an existing `uv tool` install:

```bash
uv tool install --force git+https://github.com/codatta/symphony.git
```

Uninstall:

```bash
uv tool uninstall symphony
```

## Get Started

### Local Development

Use this path when you are changing Symphony itself rather than operating it:

```bash
git clone https://github.com/codatta/symphony.git
cd symphony
uv sync
uv run symphony --help
```

In the rest of this README, `symphony ...` means the installed CLI. From a
development checkout, replace it with `uv run symphony ...`.

Build release artifacts before tagging or attaching a release:

```bash
uv build
ls dist/
```

This release-prep command should be verified in a network-enabled environment
before cutting a release tag, because build isolation may need to download the
configured build backend. Expected artifacts are a source distribution and wheel
under `dist/`, for example `symphony-<version>.tar.gz` and
`symphony-<version>-py3-none-any.whl`. Smoke test the wheel in an isolated CLI
install from a clean `dist/` directory:

```bash
uv tool install --force ./dist/symphony-*.whl
symphony --help
```

Native single-file binaries and Homebrew formulae are not part of this packaging
slice. They remain follow-on distribution channels once the CLI command surface
has stabilized.

Release automation is defined in `.github/workflows/release.yml`. Use the
manual `workflow_dispatch` path with `channel=dry-run` or `channel=staging` to
build artifacts and smoke-test the installed wheel before cutting a stable
`vX.Y.Z` tag for the main release channel. User-facing CLI changes should be
recorded in `CHANGELOG.md` before a release tag is created.

### First Run Setup

Run onboarding from the repository where you want to keep the generated
`WORKFLOW.md`:

```bash
symphony onboard --project-slug your-linear-project-slug
```

`symphony onboard` is the recommended first-run command. It scans the local
environment first, reports detected Linear/GitHub auth and Claude/Codex tooling,
and skips the init step when an existing `WORKFLOW.md` and local prerequisites
already validate. Use `--overwrite` when you intentionally want to regenerate an
existing workflow.

In an interactive terminal, onboarding starts with a short paginated orientation
when it has not been shown for the current tutorial version, then guides you
through:

- the Linear project slug;
- active and terminal Linear state names;
- the workspace root for per-issue checkouts;
- the GitHub org or user and repo name;
- a Linear API key;
- a GitHub token if you want Symphony to store one locally.

The command writes `WORKFLOW.md` and stores credentials under the local Symphony
config path. Keep raw tokens out of committed workflow files.

For scripted setup, use automated mode:

```bash
symphony onboard \
  --mode automated \
  --project-slug your-linear-project-slug \
  --linear-api-key lin_api_... \
  --github-token ghp_... \
  --github-org your-org \
  --github-repo your-repo
```

`--yes` remains available as an alias for `--mode automated`. Automated setup
never prompts; if required input or local auth is missing, it exits with
remediation steps before writing `WORKFLOW.md`.

`symphony init` remains available as the lower-level workflow generation command
for scripted setups that do not need skip/resume behavior.

Available presets are `codex-safe`, `codex-autonomous`, and `review-only`.

## Validate Setup

Run:

```bash
symphony doctor WORKFLOW.md
```

Expected checks:

- `WORKFLOW.md` parses successfully;
- Linear auth resolves;
- the configured workspace root is writable;
- `gh auth` resolves;
- a GitHub token resolves;
- the selected agent command is available;
- logs and status API paths are printable.

Fix any failed checks before dispatching a real issue.

## Quick Training Tickets

Before running Symphony on important work, create one disposable Linear issue in
the configured project. Use Linear's UI or your team's interactive Linear
command session; Symphony only needs the finished ticket to exist in an active
state. This lets a new operator finish onboarding and practice the
dispatch/review loop in a controlled session.

Start with a small docs-only issue:

```text
Title:
Add a CLI smoke-test note to README

Description:
Repository: https://github.com/your-org/your-repo

Please add one short paragraph to README.md explaining how to run the CLI smoke
test. Keep the change docs-only. Open a PR when done and post the PR URL back on
this Linear issue.

Acceptance criteria:
- README.md contains the new note.
- A GitHub PR is opened.
- The PR URL is commented on this Linear issue.
```

Move the ticket into one of the active states from `WORKFLOW.md`, usually `Todo`
or `In Progress`.

Run one controlled poll tick:

```bash
symphony run WORKFLOW.md --once --log-level INFO
```

A successful smoke run prints a summary like:

```text
Tick OK: fetched=1 dispatched=1 completed=1 failed=0 released=0
```

The exact counts can vary, but the training issue should be fetched and
dispatched. Inspect the created workspace, GitHub PR, and Linear comments.

To practice the feedback loop, leave one requested change as a Linear comment,
move the issue back to an active state, and run another controlled tick:

```bash
symphony run WORKFLOW.md --once --log-level INFO
```

When the result is acceptable, merge the PR and move the Linear issue to a
terminal state such as `Done`.

For team onboarding, repeat the same pattern with a second small ticket owned by
the trainee. Keep the acceptance criteria narrow enough that the trainee can
review the whole PR and close the Linear issue without needing production
context.

Current limitation: GitHub PR review comments are not read automatically. Put
revision instructions on the Linear issue and manually move the issue back to an
active state for another agent pass.

## Day-To-Day Operation

For continuous local operation:

```bash
symphony run WORKFLOW.md --port 7337 --logs-root ./log --log-level INFO
```

Stop the process with `Ctrl-C`.

Good first use cases:

- scoped implementation tickets with clear acceptance criteria;
- documentation and cleanup tasks;
- review follow-up where feedback is copied to the Linear issue;
- live-dispatch smoke tests with `--once`.

Avoid using the current CLI loop for secret rotation, broad refactors,
high-risk production changes, or repositories where an agent-created PR is not
safe to review.

### Running Multiple Projects

Each `WORKFLOW.md` targets one Linear project. To run Symphony across multiple
projects, start one process per project, each pointing at its own file:

```bash
symphony run project-a/WORKFLOW.md --port 7337 --logs-root ./log/a
symphony run project-b/WORKFLOW.md --port 7338 --logs-root ./log/b
```

To stop a specific process, find its PID and send SIGTERM:

```bash
# list running symphony processes
ps aux | grep "symphony run"

# stop a specific process
kill <PID>
```

Or stop all symphony processes at once:

```bash
pkill -f "symphony run"
```

To switch a running process to a different project, update `tracker.project_slug`
in the watched `WORKFLOW.md`; the daemon hot-reloads it automatically. To point
at a completely different `WORKFLOW.md` file, stop the process and restart with
the new path.

## WORKFLOW.md Basics

`WORKFLOW.md` is the team-owned runtime contract. It contains YAML front matter
for configuration and a Markdown prompt body rendered for each issue.

Minimal Claude Code example:

```yaml
---
tracker:
  kind: linear
  project_slug: "your-linear-project-slug"
  active_states:
    - Todo
    - In Progress
  terminal_states:
    - Done
    - Canceled
    - Duplicate

workspace:
  root: ~/.symphony/workspaces/your-project

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

Switch to Codex by changing the runner:

```yaml
agent:
  runner: codex
```

Use hooks when every issue workspace needs setup or verification:

```yaml
hooks:
  before_run: |
    uv sync
  after_run: |
    git status --short
```

For the full workflow schema and service contract, read [SPEC.md](SPEC.md).

## Local Development

Use this path when changing Symphony itself:

```bash
git clone https://github.com/codatta/symphony.git
cd symphony
uv sync
uv run symphony --help
```

Run tests:

```bash
uv run python -m unittest discover -s tests -p 'test_*.py'
git diff --check
```

Build release artifacts:

```bash
uv build
ls dist/
```

Smoke test a built wheel:

```bash
uv tool install --force ./dist/symphony-*.whl
symphony --help
```

## Learn More

- [SPEC.md](SPEC.md): language-agnostic Symphony service specification.
- [ARCHITECTURE.md](ARCHITECTURE.md): Python architecture, runtime model, and
  planned desktop/IM surfaces.
- [prd.md](prd.md): product requirements, build phases, and roadmap.
- [test-plan-epic-2.md](test-plan-epic-2.md): live-dispatch validation plan.

## Attribution

Symphony and its specification were created by OpenAI and are licensed under the
[Apache License 2.0](LICENSE). This repository is an independent implementation
of that specification. The original project is at
[github.com/openai/symphony](https://github.com/openai/symphony).
