# Symphony — Product Requirements Document

## Status: Draft v0.5 — CLI-first MVP: Linear + Codex first

> See [ARCHITECTURE.md](ARCHITECTURE.md) for the full system design, component diagram, data flows, and module layout.

---

## 1. Overview

Symphony is an orchestration service that polls a work tracker (Linear), creates isolated workspaces per issue, and runs AI agent sessions against those workspaces. The existing Elixir reference implementation is tightly coupled to the Codex app-server JSON-RPC protocol. This PRD covers the design and build queue for a new implementation that supports multiple agent backends.

---

## 2. Tech Stack Decision

### Candidates considered

| Language | Pros | Cons |
|---|---|---|
| **Elixir (extend current)** | Already spec-compliant; OTP supervision is excellent; hot reload | Smaller contributor pool; harder to integrate Python/TS AI SDKs; Codex coupling runs deep |
| **Python 3.12+** | Best subprocess orchestration; all major AI SDKs (Anthropic, OpenAI, Google); `asyncio` handles concurrent agents cleanly; `pydantic` for config schema | Slower startup; not a single binary out-of-the-box |
| **TypeScript / Bun** | Claude Code SDK is TypeScript-native; strong typing; Bun compiles to single binary | Node.js subprocess management is more complex; weaker daemon patterns |
| **Go** | Single binary; excellent concurrency; fast | Smaller AI SDK ecosystem; verbose for rapid experimentation |

### Recommendation: **Python 3.12+ with asyncio**

**Rationale:**

1. **Subprocess orchestration is the core runtime primitive.** Managing N concurrent agent CLI processes (Codex, Claude Code, Gemini CLI) with streaming stdout/stderr, timeouts, stall detection, and cancellation maps cleanly to `asyncio.create_subprocess_exec`. Python's subprocess model is the most ergonomic for this pattern.
2. **All agent SDKs have first-class Python support.** Anthropic SDK, OpenAI SDK, Google Generative AI SDK — all available and actively maintained.
3. **Pydantic** gives Ecto-equivalent typed config validation with `$VAR` resolution, defaults, and schema errors at startup.
4. **`asyncio` + `anyio`** handles the orchestrator's event loop (tick, retry timers, reconciliation) more directly than OTP GenServer while remaining approachable.
5. **FastAPI + SSE** covers the optional HTTP observability server cleanly.
6. **`watchfiles`** provides WORKFLOW.md hot reload.

### Core dependencies

```
python          3.12+           runtime
pydantic        2.x             config schema, WORKFLOW.md validation
httpx           async           Linear GraphQL, image generation APIs
jinja2          2.x             prompt template rendering (strict mode)
typer           CLI             CLI entrypoint with --port, --logs-root
fastapi         HTTP server     /api/v1/* and optional LiveView-style dashboard
uvicorn         ASGI            HTTP server
watchfiles      fs watch        WORKFLOW.md hot reload
anthropic       0.x             Claude API integration
openai          1.x             Codex / GPT-Image-1 API integration
google-genai    1.x             Gemini API integration
pyyaml          6.x             WORKFLOW.md front matter
anyio           task groups     structured concurrency for agent sessions
```

### Project layout

```
symphony/
  symphony/
    cli.py                  # typer CLI entrypoint
    orchestrator.py         # poll loop, claims, retries, reconciliation
    config.py               # pydantic schema + $VAR resolution
    workflow.py             # WORKFLOW.md loader + jinja2 renderer
    workspace.py            # per-issue directories + hook execution
    tracker/
      base.py               # IssueTrackerAdapter ABC
      linear.py             # Linear GraphQL implementation
    agents/
      base.py               # AgentRunner ABC — the key new abstraction
      codex.py              # Codex app-server JSON-RPC adapter
      claude_code.py        # Claude Code CLI adapter
      gemini_cli.py         # Gemini CLI adapter
      openai_api.py         # OpenAI API adapter (includes image generation)
      hermes.py             # OpenAI-compatible API adapter (Ollama/vLLM)
    http_server.py          # FastAPI observability server
    log_file.py             # structured logging
  tests/
  WORKFLOW.md
  pyproject.toml
```

---

## 3. Core Architecture: Multi-Agent Adapter Pattern

The central change relative to the Elixir implementation is extracting a clean **AgentRunner** abstraction. All four new agent types map onto one of two adapter base classes:

### 3.1 CLIAgentRunner (subprocess-based)

Used by: Codex app-server, Claude Code CLI, Gemini CLI

```python
class CLIAgentRunner(ABC):
    async def start_session(self, workspace: Path) -> AgentSession: ...
    async def run_turn(self, session: AgentSession, prompt: str, issue: Issue) -> TurnResult: ...
    async def stop_session(self, session: AgentSession) -> None: ...
```

The runner launches a subprocess in the workspace directory, manages its lifecycle, enforces timeouts and stall detection, and streams events back to the orchestrator.

### 3.2 APIAgentRunner (API-based)

Used by: GPT-Image-1, Claude API (headless), Gemini API

```python
class APIAgentRunner(ABC):
    async def run_task(self, workspace: Path, prompt: str, issue: Issue) -> TaskResult: ...
```

API-based runners do not have persistent sessions or multi-turn streaming. They make a single API call, save the output to the workspace, and return.

### 3.3 Session contract (unchanged from SPEC.md)

Orchestrator → AgentRunner is a fire-and-forget task. Events are pushed back via a callback:

```python
async def on_event(event: AgentEvent) -> None
```

Event types mirror SPEC.md §10.4: `session_started`, `turn_completed`, `turn_failed`, `notification`, `malformed`, etc.

---

## 4. Feature Evaluation

### 4.1 Claude Code CLI

**Fit assessment: ✅ Strong fit**

Claude Code CLI (`claude`) supports a `--print` flag for non-interactive, single-turn output. Multi-turn agentic operation is done by piping new input into the session, or by calling the Anthropic API directly using the Claude Agent SDK for programmatic session management.

**Integration approach:**

Two sub-modes:

| Sub-mode | When to use | How |
|---|---|---|
| `cli-print` | Simple single-turn tasks | `claude --print "<prompt>"` in workspace dir; capture stdout |
| `api-agent` | Multi-turn agentic workflows (equivalent to Codex app-server) | Anthropic Python SDK, `anthropic.beta.messages.stream()`, tool_use for `linear_graphql` |

For full parity with the Codex integration, use `api-agent` mode. The `CLIAgentRunner` for Claude Code wraps the Python SDK's streaming API rather than a subprocess, since the SDK gives programmatic turn management.

**Configuration in WORKFLOW.md:**

```yaml
agent:
  runner: claude_code
  model: claude-sonnet-4-6
  max_tokens: 32768
  tool_use: true
```

**MCP tool integration:** Claude Code supports MCP natively. The `linear_graphql` dynamic tool can be served as an MCP server exposed to the session, consistent with the existing SPEC.md §10.5 extension contract.

**Verdict:** Implement as a first-class runner. Should be the second runner after Codex in the Build Queue.

---

### 4.2 Hermes Agent (OpenAI-Compatible Local Models)

**Fit assessment: ✅ Good fit**

NousResearch Hermes series (Hermes-2, Hermes-3) are fine-tuned LLMs with strong tool-use capability, typically served locally via Ollama or vLLM with an OpenAI-compatible API endpoint. They are _models_, not agents — they require a host process to manage the agentic loop.

Integration approach: An `OpenAICompatRunner` wraps the OpenAI SDK pointed at a local Ollama/vLLM endpoint (`base_url: http://localhost:11434/v1`). The orchestrator drives the tool-use loop, calling tools client-side (including `linear_graphql`). This is a generic "OpenAI-compatible API runner" that can target any model — Hermes, Mistral, DeepSeek, LLaMA, etc.

**Configuration:**

```yaml
agent:
  runner: openai_compatible
  base_url: http://localhost:11434/v1
  model: nous-hermes-3
  api_key: $HERMES_API_KEY
```

**Verdict:** Implement as `openai_compatible` runner — it is effectively free once the Claude API runner exists, since both use the same streaming tool-use loop pattern. Good for teams running local models or private inference endpoints.

---

### 4.3 Gemini CLI

**Fit assessment: ✅ Good fit**

Google's `gemini` CLI tool (released 2025) supports non-interactive invocation and can work on codebases. Its interface is similar to Claude Code CLI.

**Integration approach:**

`GeminiCLIRunner` as a `CLIAgentRunner` subclass:

```bash
# single turn
gemini --yolo "<prompt>"

# or with stdin piping for longer prompts
echo "<prompt>" | gemini --yolo
```

For multi-turn agentic operation with full tool use, the `google-genai` Python SDK (v1.x) is preferable — it provides streaming, function calling, and code execution tool support.

**Configuration:**

```yaml
agent:
  runner: gemini_cli
  model: gemini-2.5-pro
```

OR for API-based multi-turn:

```yaml
agent:
  runner: gemini_api
  model: gemini-2.5-pro
  api_key: $GOOGLE_API_KEY
```

**Key limitation:** Gemini CLI's tool-use protocol (for `linear_graphql` equivalent) is less standardized than Codex app-server or Anthropic SDK tool_use. The first implementation should use the `google-genai` SDK for full tool-use support rather than the raw CLI.

**Verdict:** Implement as a `gemini_api` runner using the Python SDK. The CLI wrapper is a nice-to-have but the SDK gives better control over multi-turn sessions and tool calls.

---

### 4.4 GPT-Image-1 (Image Generation Tasks)

**Fit assessment: ⚠️ Partial fit — requires design extension, represents meaningful scope expansion**

GPT-Image-1 (OpenAI's image generation model, formerly DALL-E 3) is a **generative API**, not a coding agent. There is no multi-turn loop, no tool use, no workspace file manipulation — it takes a text prompt and returns an image.

**Where it fits in the Symphony model:**

Symphony's core is: _issue → workspace → agent turns → output_. Image generation maps to this as:

- **Issue:** A Linear task like "Generate hero image for landing page v2"
- **Workspace:** Directory where the generated images are saved
- **"Turn":** A single API call to GPT-Image-1 with the rendered prompt
- **Output:** PNG files committed to the workspace; PR attached to Linear issue

This works, but requires a new concept: **task type**. Coding agents do multiple turns of file editing. Image generators do a single (or few) API calls and save files.

**Integration approach — `ImageGenerationRunner`:**

```python
class ImageGenerationRunner(APIAgentRunner):
    async def run_task(self, workspace: Path, prompt: str, issue: Issue) -> TaskResult:
        # 1. Call openai.images.generate(model="gpt-image-1", prompt=prompt, ...)
        # 2. Save PNG to workspace/<issue_identifier>/<timestamp>.png
        # 3. Commit to workspace branch
        # 4. Return TaskResult with file paths
```

**Design implications:**

1. **No stall detection needed** — API calls have fixed timeouts.
2. **No multi-turn continuation** — normal worker exit immediately after one call.
3. **Prompt template** still applies — WORKFLOW.md body describes what to generate, using `{{ issue.title }}`, `{{ issue.description }}`, etc.
4. **Workspace hooks still apply** — `after_create` can set up a git repo; `after_run` can auto-commit.
5. **`linear_graphql` tool** is not applicable — image generator doesn't call tools.

**What this enables for teams:**

- Design tasks in Linear → auto-generate assets → PR with images for review
- UI component sketch generation
- Marketing copy + image generation pipelines
- Multimodal workflows where a coding agent (Claude/Codex) consumes the generated images in a follow-on issue

**Verdict:** Valid fit with clear boundaries. Recommend implementing after the CLI agent runners are stable. Requires adding `task_type: generative | agentic` to the runner config (default: `agentic`). The architectural surface area is small — it's essentially an `APIAgentRunner` with file output and no turn loop.

**Caveat:** If the intent is for GPT-Image-1 to be _called by a coding agent_ (e.g., Claude Code calls the image API as a tool), that is already handled through the agent's built-in tool use or MCP — Symphony doesn't need a dedicated runner for that case.

---

### 4.5 Mac Desktop App

**Fit assessment: ✅ Strong fit — distribution layer, not a core change**

Distributing Symphony as a native macOS application removes the requirement for users to install Python, manage a terminal daemon, or understand CLI conventions. The orchestrator logic is unchanged — the desktop app is a shell that manages the process lifecycle and surfaces the existing web dashboard in a native window.

**What "desktop app" means here:**

| Layer | What it does |
|---|---|
| macOS app bundle (`.app` / `.dmg`) | Installlable via drag-and-drop; no Python/pip required |
| Menubar / system tray icon | Start/stop daemon, quick status, open dashboard |
| Native notifications | Desktop alerts when agents finish, get blocked, or need review |
| Embedded web dashboard | The existing FastAPI + React frontend, rendered in a WebView |
| Python sidecar | The orchestrator daemon, bundled and managed by the app shell |

**Recommended approach: Tauri v2 + PyInstaller sidecar**

Tauri v2 is a Rust-based desktop app framework that wraps a web frontend in a lightweight native shell (no Chromium bundle — uses the OS WebView, which is WKWebView on macOS). The Python orchestrator is bundled as a standalone binary via PyInstaller and registered as a Tauri sidecar. The web frontend (React or Svelte) is shared with the browser dashboard.

```
symphony-desktop/
  src-tauri/         # Rust Tauri shell
    sidecar/         # PyInstaller-built symphony binary
    tauri.conf.json  # window config, sidecar, permissions
  src/               # React/Svelte frontend (reused from web dashboard)
```

**Why Tauri over alternatives:**

| Option | Why not |
|---|---|
| Electron | ~150 MB Chromium bundle per install; heavy for a daemon wrapper |
| Swift/SwiftUI | Separate codebase from the web dashboard; more maintenance |
| `rumps` + `pywebview` | No `.dmg` distribution without significant packaging work; less native |
| Web only (no desktop app) | Requires users to manage a terminal process manually |

**Key Tauri plugins used:**

- `tauri-plugin-shell` — manage Python sidecar lifecycle (start on launch, kill on quit)
- `tauri-plugin-notification` — native macOS notifications for agent events
- `tauri-plugin-updater` — auto-update from GitHub Releases
- `tauri-plugin-single-instance` — prevent multiple Symphony processes

**Distribution path:**

1. `tauri build` produces a signed `.dmg` for direct download
2. Auto-update checks a GitHub Releases feed
3. Mac App Store distribution is possible but requires sandboxing review — treat as a later milestone

**Architecture impact on the Python backend:**

Minimal. The daemon gains one new startup flag `--headless` (suppresses terminal output when launched by the desktop app) and exposes a health endpoint at `/api/v1/health` for the Tauri shell to poll. The sidecar communicates with the frontend over the existing FastAPI HTTP server on localhost.

**Verdict:** Implement after the core Python implementation is stable. The desktop app is a packaging and distribution milestone — it does not require changes to the orchestration logic.

---

### 4.6 Remote Phone Coordination

**Fit assessment: ✅ Strong fit — extends the operator loop to mobile**

Symphony's core value is autonomous agent execution. But agents regularly reach points where human judgment is needed: an issue moves to `Human Review`, an agent is blocked by a missing credential, or an approval gate fires. Today, operators must be at a desk watching the dashboard. Remote phone coordination closes this gap — operators get notified on their phone and can respond without opening a laptop.

**Two sub-features:**

#### A. Mobile monitoring (read-only)

Operators can view active sessions, queue depth, token consumption, and per-issue status from any device. This is largely free if the web dashboard is built as a **Progressive Web App (PWA)**: responsive layout + Web App Manifest + service worker. Operators install it on their home screen; it behaves like a native app.

PWA Web Push now works on iOS 16.4+ (Safari finally shipped it in 2023), which covers the majority of phone-based operators.

#### B. Push notifications + action gates (the valuable part)

Symphony sends a push notification to the operator's phone when an agent needs attention. The notification includes the issue identifier, state, and reason, plus one-tap action buttons.

**Notification triggers:**

| Event | Notification content | Actions |
|---|---|---|
| Issue → `Human Review` | "MT-42: PR ready for review" | Open PR, Open issue |
| Agent blocked (missing auth) | "MT-55 blocked: missing GITHUB_TOKEN" | Open dashboard |
| Agent stalled (stall timeout) | "MT-60 stalled after 5m of inactivity" | Retry, Cancel |
| Worker failure (after N retries) | "MT-71 failed: 3 retries exhausted" | View logs |
| Agent requests approval (non-auto-approve policy) | "MT-80 awaiting command approval" | Approve, Reject |

**Notification backend options:**

| Backend | Pros | Cons |
|---|---|---|
| **ntfy** (recommended) | Self-hosted or ntfy.sh cloud; zero mobile app needed (app exists on iOS/Android); HTTP `POST` to push; free | Requires ntfy app installed |
| **Pushover** | Reliable; one-time $5 per platform; great iOS/Android apps | Paid; third-party dependency |
| **Web Push (APNs/FCM)** | Native to PWA; no extra app | Requires VAPID key setup; complex on iOS |
| **Webhook** | Generic; operators wire to Slack, Teams, Discord, etc. | No action buttons; read-only |
| **Telegram Bot** | Free; global; action buttons via inline keyboard | Requires Telegram account |

**Recommendation: ntfy as primary, webhook as generic fallback.** ntfy's HTTP API is trivially simple (`POST https://ntfy.sh/my-topic`), its apps are on iOS and Android, and it can be self-hosted for air-gapped deployments. The webhook backend means operators who prefer Slack or Teams can wire it up themselves.

**Approval workflow architecture:**

When an agent hits an approval gate that is not auto-resolved:

1. Orchestrator emits `approval_requested` event
2. `NotificationService` sends push notification with `approve_url` and `reject_url` deep links
3. Operator taps Approve → browser (or PWA) opens, calls `POST /api/v1/sessions/<session_id>/approve`
4. Orchestrator receives approval, unblocks the agent turn
5. If no response within `approval_timeout_ms`, treat as rejection per configured policy

**WORKFLOW.md config additions:**

```yaml
notifications:
  backend: ntfy                        # ntfy | pushover | webhook | telegram
  ntfy_topic: $SYMPHONY_NTFY_TOPIC    # ntfy topic URL or topic name
  webhook_url: $SYMPHONY_WEBHOOK_URL  # generic webhook fallback
  approval_timeout_ms: 300000          # 5 minutes; treat as reject after this
  events:                              # which events trigger notifications
    - human_review
    - agent_blocked
    - agent_stalled
    - worker_failed
    - approval_requested
```

**New API endpoints:**

```
POST /api/v1/sessions/<session_id>/approve    # operator approves a gate
POST /api/v1/sessions/<session_id>/reject     # operator rejects
GET  /api/v1/health                           # liveness check for desktop sidecar
```

**Architecture impact:** A new `NotificationService` module that subscribes to orchestrator events and dispatches push messages. It is observability-only for monitoring events; for approval gates it becomes load-bearing (the orchestrator waits on the approval channel with a timeout). The approval gate integrates with the existing `codex.approval_policy` model — it activates only when policy is `on-request` rather than `never` or `untrusted`.

**Verdict:** Strong fit. Start with ntfy + webhook (low effort, high operator value). The approval gate is the premium feature — build it after the notification plumbing is proven. The PWA mobile dashboard is effectively free once the web dashboard is responsive.

---

### 4.7 Linear Integration & Authentication

**Fit assessment: ✅ Core requirement — Linear is the primary UX surface**

Symphony's orchestration loop is entirely driven by Linear state. Every goal, objective, and work item enters the system as a Linear issue. This is not just an integration — it is the interface. The system needs:

1. **Reliable authentication** that works for CLI, desktop app, and CI without different code paths
2. **Real-time coordination via webhooks**, not just polling — when an operator moves an issue to a different state in Linear, agents must react within seconds
3. **A setup wizard** so non-CLI users can connect Linear and generate a WORKFLOW.md without touching config files
4. **Token security** appropriate to each deployment context (env var for CI, Keychain for desktop)

**Authentication design:**

Two modes are supported and coexist:

| Mode | How | Storage |
|---|---|---|
| **Personal API key** | `LINEAR_API_KEY` env var or `tracker.api_key` in WORKFLOW.md | Env / WORKFLOW.md |
| **OAuth 2.0** | Full consent flow via Linear Application; bearer token stored securely | macOS Keychain (desktop) or `~/.symphony/credentials.json` (CLI) |

Token resolution order (first non-empty wins): env var → WORKFLOW.md → Keychain → credentials file. The `LinearClient` never reads credentials directly — it receives a resolved token from `TokenStore`.

**OAuth scopes required:** `read`, `write` (issue state + comments), optionally `app:assignIssues`.

**Webhook vs polling:**

| | Polling only | Polling + Webhooks |
|---|---|---|
| Reaction time | 5–30 s | < 1 s |
| Linear API calls | O(N × ticks) | O(1) per change + periodic reconcile |
| Requires public URL | No | Yes (or a tunnel) |

When webhooks are active, `polling.interval_ms` can be raised to 120 000 ms (2 min) as a safety net. Webhooks handle the fast path; polling catches missed events.

**Setup wizard** (desktop app first-run, also accessible via `/setup` in the web UI):

Step 1 → Connect Linear (OAuth) → Step 2 → Select team + project → Step 3 → Configure active/terminal states → Step 4 → Choose AI agent + enter API key → Step 5 → Preview + save generated WORKFLOW.md → Step 6 → Launch

**New config fields** (`tracker` block in WORKFLOW.md):
```yaml
tracker:
  team_id: "..."
  oauth_client_id: $LINEAR_CLIENT_ID
  oauth_client_secret: $LINEAR_CLIENT_SECRET
  webhook_secret: $LINEAR_WEBHOOK_SECRET
server:
  public_url: $SYMPHONY_PUBLIC_URL
  tunnel: none                          # none | cloudflared | ngrok
```

**Verdict:** Implement Linear auth + webhook support before shipping any agent runner to production. Without webhooks, Symphony is too slow for real team use. Without OAuth, the desktop app has no clean first-run flow.

---

## 5. Summary: Feature Fit Matrix

| Feature | Fit | Layer | Effort | Key Dependency |
|---|---|---|---|---|
| **Linear Auth + OAuth** | ✅ Core requirement | Auth / tracker | Medium | `keyring`, `httpx` |
| **Linear Webhooks** | ✅ Core requirement | Tracker / HTTP | Medium | `cryptography` (HMAC) |
| **Setup Wizard** | ✅ Strong | Web UI + HTTP | Medium | Linear OAuth |
| Claude Code (API/SDK) | ✅ Strong | Agent runner | Medium | `anthropic` Python SDK |
| Gemini CLI / API | ✅ Good | Agent runner | Medium | `google-genai` SDK |
| Hermes / OpenAI-compatible | ✅ Good | Agent runner | Low | `openai` SDK (reuse) |
| GPT-Image-1 | ⚠️ Scope expansion | Agent runner (generative) | Medium | `openai` SDK |
| Mac Desktop App | ✅ Strong | Distribution shell | Medium-High | Tauri v2, PyInstaller |
| Remote Phone / IM | ✅ Strong | Notifications + mobile UI | Medium | `aiogram` / `slack_bolt` |

---

## 6. Implementation Plan

The build is divided into product phases. Phase 1 is the MVP: a CLI-first
Symphony daemon that can read Linear work, create isolated workspaces, run Codex,
and hand results back through Linear. This is the fastest path to real use and
feedback. The standalone app follows once the core loop is proven.

Each phase must end with a working slice, updated `prd.md` context if decisions
change, Linear ticket updates, a PR, and review evidence.

### 6.1 Execution Rules

1. **No direct work on `main`.** Every implementation change starts on a feature
   branch named after the Linear issue or milestone, for example
   `feat/linear-auth-token-store`. Agents must never commit directly to `main`
   unless the user explicitly asks for a direct `main` commit.
2. **One PR per independently reviewable outcome.** Keep PRs small enough that a
   reviewer can validate behavior, tests, and product intent without reading the
   entire system at once.
3. **Read and sync the PRD.** Agents must read `prd.md` before implementation,
   update it with the intended solution when behavior, architecture, workflow,
   configuration, or user-facing expectations change, and sync it after
   implementation so the shipped behavior, validation, and follow-up work are
   accurate.
4. **Linear is the implementation ledger.** Every phase and substantial task must
   have a corresponding Linear issue. The issue must record the selected
   solution, decision context, rejected alternatives, validation plan, and PR
   links.
5. **PR creation is part of implementation.** Completed changes must be pushed
   and opened as a pull request using the repository template unless the user
   explicitly asks to stop before PR creation.
6. **Review loop is mandatory for code changes.** After opening a PR, request
   review from another agent instance where available. Iterate through comments,
   fixes, and follow-up reviews until there are no blocking comments.
7. **UI-impact evidence is required.** For UI-impacted PRs, run the app locally
   and attach `.png` captures of changed screens. Committed screenshot
   artifacts must use Git LFS or another configured storage-saving large-file
   mechanism.
8. **Validation must be written down.** Each PR must list targeted checks, full
   gates that were run, skipped checks with reasons, and any manual verification.

### 6.2 Phase 0: Repository And Delivery Guardrails

**Goal:** Make the implementation workflow safe before product code begins.

**Scope:**

- Root `AGENTS.md` with branch, PR, Linear, review, and UI screenshot policy.
- Root `CLAUDE.md` mirroring contribution-agent behavior for Claude Code
  sessions.
- PR template updates if the current template does not request Linear links,
  validation evidence, and UI screenshots.
- Git LFS configuration for recurring binary review artifacts such as committed
  `.png` screenshots.
- Initial Linear milestone or project that contains the phase tickets below.

**Exit criteria:**

- `AGENTS.md` is tracked.
- `CLAUDE.md` is tracked for Claude Code contributors.
- PR template captures solution, decision context, validation, and screenshots.
- Git LFS or an equivalent storage-saving artifact policy is documented.
- Linear contains implementation tickets for the MVP phase at minimum.

### 6.3 Phase 1: MVP — CLI Linear + Codex

**Goal:** Deliver the smallest useful Symphony implementation: configure a repo,
start the daemon from the terminal, dispatch Linear issues to Codex, and observe
the result.

**Scope:**

- Python project skeleton, package layout, CLI entrypoint, and logging baseline.
- `WORKFLOW.md` parser with YAML front matter, prompt template rendering, `$VAR`
  resolution with named missing-variable errors, strict config validation,
  defaults, `~` expansion, and hot reload.
- Core domain models for issues, workflow config, sessions, events, workspaces,
  and run state.
- Linear authentication for MVP operation. Personal API key support is required;
  OAuth can ship later and must not block the polling-based MVP.
- Linear tracker read path: candidate issue fetch, state refresh, pagination,
  and normalized issue model.
- `linear_graphql` client-side tool so Codex can comment, update issue state,
  and attach PR links using Symphony-managed Linear auth.
- Orchestrator poll loop, dispatch, claims, bounded concurrency, retry/backoff,
  reconciliation, and cleanup.
- Per-issue workspace lifecycle manager with sanitized paths, lifecycle hooks,
  and root containment safety checks.
  - **IN-171 implementation reference:** create and reuse deterministic
    workspaces under `workspace.root`, sanitize issue identifiers to
    `[A-Za-z0-9._-]`, enforce root containment before agent launch or cleanup,
    run configured lifecycle hooks with timeouts, and support terminal cleanup
    with `keep_on_failure` for debugging failed runs.
- `AgentRunner` abstraction, `CLIAgentRunner` base, and Codex app-server
  JSON-RPC adapter.
- Minimal HTTP/status surface: `/api/v1/state`, `/api/v1/<identifier>`,
  `/api/v1/refresh`, `/api/v1/health`, and recent log access.

**Alternatives considered:**

- Start with a standalone app first. This improves onboarding, but delays the
  core proof that Symphony can execute Linear work through Codex.
- Require OAuth in the MVP. This improves onboarding, but API-key setup is enough
  for the first operational loop and keeps the first authentication surface
  smaller.
- Implement all runner abstractions before Codex. This is architecturally tidy,
  but expands the first delivery slice before the Linear/Codex loop is proven.
- Require webhooks in the MVP. Webhooks improve responsiveness, but polling is
  enough to prove the product loop and avoids public URL/tunnel complexity.

**Exit criteria:**

- `symphony --help` works.
- A user can start Symphony from the terminal with a repository-owned
  `WORKFLOW.md`.
- Config, tracker, workspace, orchestrator, and Codex runner tests pass.
- A Linear issue in an active state can dispatch one Codex session in a
  per-issue workspace.
- Codex can use `linear_graphql` to post progress and move the issue to the
  workflow-defined handoff state.
- Terminal-state reconciliation stops or releases active work.
- Logs and the minimal status API are sufficient to debug a session from issue
  id to agent result.

**Closeout status — 2026-05-09:** Phase 1 is closed for planning purposes with
one explicit caveat. The local implementation covers the CLI, workflow/config,
Linear read path, `linear_graphql`, orchestration state, workspace lifecycle,
runner contracts, Codex app-server runner, status API handler, and runtime
single-tick glue. Validation passed with 100 Python tests, `symphony --help`,
`git diff --check`, config preflight against `elixir/WORKFLOW.md`, and a live
Linear polling tick that returned zero candidates. A live dispatch of one Linear
issue to Codex remains unproven because there were no active candidate issues
during the smoke test. That proof should be treated as a Phase 2 entry gate, not
as a reason to keep expanding Phase 1 scope.

**Review follow-up — 2026-05-11:** PR #7 review found three Phase 1 integration
gaps. The CLI daemon now starts the loopback status API listener in normal daemon
mode, reloads changed `WORKFLOW.md` config/prompt before poll ticks so future
dispatch uses the latest workflow contract, and cleans terminal workspaces when a
successful continuation retry disappears from active candidate polling after the
agent moves the tracker issue to a terminal state. This keeps successful handoff
cleanup aligned with the workspace lifecycle goal while preserving immediate
workspace reuse during active runs.

**MVP usage flow:**

1. Install the Python package in a local environment.
2. Create or edit `WORKFLOW.md` in the target repository.
3. Set `LINEAR_API_KEY` and any required Codex environment/auth state.
4. Run `symphony /path/to/repo/WORKFLOW.md --port 7337 --logs-root ./log`.
5. Move a Linear issue into an active state.
6. Symphony polls Linear, creates an isolated workspace, renders the prompt, and
   starts Codex in that workspace.
7. Codex uses `linear_graphql` to post progress, attach PR links, and move the
   issue to the configured handoff state.
8. The operator checks logs and the status API to verify the run.

### 6.4 Phase 2A: Standalone CLI Onboarding And Packaging

**Linear issue:** IN-205 — Package Symphony as an easy-install standalone CLI
with guided onboarding.

**Goal:** Make the proven CLI MVP useful without requiring users to understand
the internal daemon contract, hand-author `WORKFLOW.md`, or keep Linear secrets
inside repository files.

**Selected solution:**

- Keep the runtime daemon architecture intact and add a productized CLI surface:
  `symphony init`, `symphony doctor`, and `symphony run`.
- `symphony init` generates a repository-owned `WORKFLOW.md` from presets. The
  initial presets are `codex-safe`, `codex-autonomous`, and `review-only`.
- Interactive `symphony init` starts with a language picker for English or
  Simplified Chinese, then shows a brief versioned terminal orientation as
  paginated Q&A cards instead of one long text block. Each page should answer
  one setup question: what Symphony is, what init will deliver, why the
  issue-driven workflow matters, what productivity shift OpenAI reported, and
  what the user should expect next. Tutorial read history is stored in the local
  Symphony config directory so the same tutorial version is shown only once per
  installation; bumping the tutorial version shows it again.
- Setup supports two execution modes. Interactive mode guides the user through
  missing auth, project/repo selection, and workflow generation. Automated mode
  validates existing CLI/env/MCP auth and explicit config inputs without
  prompting, returning deterministic pass/fail output with exact remediation
  commands for headless or scripted installs.
- `symphony init` should guide all required auth before writing the final
  workflow: Linear, GitHub, Codex, and Claude Code. It should prefer existing
  authenticated CLIs or MCP sessions when available, validate them immediately,
  and only ask for raw tokens when no usable local auth is present.
- CLI Linear auth starts by detecting a usable Linear CLI / MCP authentication
  context. If available, init should use it to validate identity, list accessible
  teams/projects/states, and select the target project without asking the user to
  paste a token. Personal API keys stored outside the repo remain the fallback.
  Resolution order remains env var → WORKFLOW.md indirection/literal → Linear
  CLI/MCP auth context → local credentials file.
- GitHub auth should be delegated to `gh`. `symphony init` should run or guide
  `gh auth login`, validate `gh auth status`, and check the authenticated
  account can access the configured owner/repository with enough scope for
  branch push and pull-request creation. Manual GitHub token entry remains a
  fallback for environments where `gh` is unavailable.
- Agent auth should be checked during onboarding. For Codex, init/doctor should
  verify the `codex` command is installed and can start the configured app-server
  mode or report the exact login/setup command needed. For Claude Code,
  init/doctor should verify the `claude` command is installed and authenticated
  before generating a Claude-default workflow.
- `symphony doctor` validates the generated workflow, resolved Linear auth,
  GitHub repository access, Codex or Claude Code runner auth, workspace
  writability, logs root, and status API address before users run a live poll.
- `symphony run` is the clear long-term command for the daemon while the legacy
  `symphony WORKFLOW.md --once/--check` invocation remains compatible.
- Package first through normal Python CLI channels (`uv tool install`, `pipx`,
  and release artifacts). Native single-file binaries and Homebrew are follow-on
  distribution channels once the command surface stabilizes.

**Decision context:**

- A desktop app remains valuable, but it is too large for the immediate usability
  problem. A packaged CLI with guided onboarding removes most setup friction
  while keeping the delivery slice small and testable.
- Linear OAuth is still the preferred end-state, but the CLI should first reuse
  existing developer auth surfaces. Linear CLI/MCP and `gh` already solve the
  browser-login and scope-discovery problem for many operators; Symphony should
  validate and reuse those sessions instead of making users paste credentials
  into another tool.
- Crosscheck provides a useful setup pattern to adapt: environment checks happen
  before configuration, guided onboarding owns interactive choices, tool-native
  GitHub auth is derived from `gh`, status output summarizes auth/config/CLI
  state, and failures include exact fix commands. Symphony should use the same
  product pattern while keeping repository-owned `WORKFLOW.md` and
  `symphony doctor` as first-class contracts.
- Personal API key onboarding remains necessary as a fallback for Linear
  environments without CLI/MCP auth, headless CI, and early bootstrapping.
- Presets are intentionally conservative. They encode safe concurrency,
  sandbox, and polling defaults without hiding the generated `WORKFLOW.md` from
  teams that want to review or version runtime policy.

**Alternatives considered:**

- Start directly with the Tauri desktop app. This improves non-terminal UX, but
  delays packaging and onboarding improvements that are useful immediately.
- Require OAuth before improving the CLI. This is more secure, but adds Linear
  app registration, redirect handling, token refresh, and revocation before the
  basic command surface is proven.
- Require raw token entry for every integration. This is simple to implement,
  but creates unnecessary friction and hides whether the user's existing Linear,
  GitHub, Codex, or Claude local auth is already valid.
- Make every setup run interactive. This is friendlier for first-time users, but
  blocks scripted installs, CI validation, and repeatable team bootstrap flows.
- Hide `WORKFLOW.md` entirely behind CLI preferences. This reduces visible
  configuration, but conflicts with Symphony's repository-owned workflow
  contract and makes review harder.

**Scope:**

- CLI subcommands and backwards-compatible legacy invocation.
- Friendly bilingual terminal orientation for interactive init that lets users
  choose English or Simplified Chinese before asking for project, repository,
  and auth details. The tutorial is owned by a self-contained module with a
  version, persisted read history, and paginated Q&A renderer so future
  onboarding entrypoints can trigger it without depending on CLI internals.
- Explicit setup mode contract for `interactive` and `automated` runs, including
  non-TTY behavior, skip/continue controls for tutorial pages, and stable exit
  codes for missing auth or invalid configuration.
- Guided auth preflight inside `symphony init` for Linear CLI/MCP, GitHub via
  `gh`, Codex CLI, and Claude Code CLI.
- Local credentials-file fallback for Linear API keys and GitHub tokens with
  private file mode when the preferred CLI/MCP auth path is unavailable.
- Workflow generation from smart presets and explicit project/state/workspace
  inputs.
- Doctor/preflight checks that produce actionable pass/fail output, including
  the active identity, token source, repository access, CLI versions, and exact
  next command to run when a dependency is missing.
- README and PR documentation for install and first-run usage.
- Packaging metadata sufficient for `uv tool install` / `pipx` style installs.

**Exit criteria:**

- A new user can install Symphony as a CLI, run `symphony init`, store or provide
  Linear auth without editing secrets into `WORKFLOW.md`, validate GitHub repo
  access through `gh`, confirm Codex or Claude Code runner auth, run
  `symphony doctor`, and then run one poll tick with `symphony run --once`.
- A first-time interactive user reads the orientation one page at a time, can
  continue or skip without losing setup progress, and does not see the same
  tutorial version again after completion.
- Automated setup never blocks on prompts, uses existing CLI/env/MCP auth only,
  and exits with stable failure reasons plus exact fix commands such as
  `gh auth login`, `codex login --device-auth`, Claude Code login, or Linear
  auth setup.
- Generated workflows parse through the same production loader as hand-written
  workflows.
- Credential lookup works from env vars, explicit WORKFLOW references, and the
  local credentials file, with Linear CLI/MCP and `gh` auth used when available.
- Tests cover workflow generation, credential storage, CLI/MCP auth detection,
  `gh` access checks, runner auth checks, doctor checks, and backwards-compatible
  CLI startup. New onboarding tests cover tutorial pagination, language
  selection, versioned read history, non-TTY behavior, interactive setup,
  automated setup, and mocked auth adapters for Linear, GitHub, Codex, and
  Claude Code.

**Packaging closeout — 2026-05-14:** Python packaging is complete for the
stabilized CLI surface. The README now documents `uv tool install`, `pipx`,
local checkout installs, wheel/sdist generation with `uv build`, and isolated
wheel smoke testing. Native single-file binaries and Homebrew remain explicit
follow-on distribution channels instead of Phase 2A blockers.

### 6.4.1 Phase 2A Follow-Up: Production CLI Release And Onboarding UX

**Linear issue:** IN-280 — Productionize Symphony CLI release and onboarding UX.

**Goal:** Turn the packaged CLI slice into an operator-ready product surface:
versioned commands, release automation, staging/main release channels, and an
onboarding flow that detects existing local tools and auth before asking users
for secrets or configuration.

**Selected solution:**

- Keep `symphony init` as the low-level workflow generator and add
  `symphony onboard` as the recommended first-run entrypoint. `onboard` runs the
  same init/tutorial flow when setup is missing, but first checks whether a
  valid `WORKFLOW.md` and local prerequisites already exist. If setup is already
  valid, it reports the completed checks and skips regeneration by default.
- Add `symphony --version` using the Python package version as the single source
  of truth. The value must match the installable artifact version and should not
  be copied into multiple runtime constants.
- Show an environment readiness summary before interactive setup writes any
  files. The summary covers detected Linear auth source, GitHub auth source,
  runner command availability, configured repository inputs, existing workflow
  state, and exact next commands for missing prerequisites.
- Prefer existing authenticated developer tooling over token prompts: `gh` for
  GitHub, environment variables or the local Symphony credentials file for
  Linear/GitHub tokens, and installed `claude` / `codex` commands for runners.
  Linear CLI/MCP auth remains part of the longer-term target, but the CLI must
  keep personal API keys as a headless fallback.
- Establish release discipline around `CHANGELOG.md`, tagged versions, and a
  GitHub Actions release workflow. The workflow builds wheel/sdist artifacts,
  installs the wheel in a clean environment, and runs CLI smoke checks before
  allowing a publish job to target the `staging` or `main` environment.
- Keep publishing guarded. Staging release validation may run from manual
  dispatch or prerelease tags; main publishing should run only for stable tags
  and protected GitHub environments.

**Decision context:**

- IN-205 proved the basic packaged CLI and guided setup. IN-280 is not a
  replacement for that work; it closes the productization gap between an
  installable command and a releaseable operator tool.
- `init` should remain scriptable and deterministic. A separate `onboard`
  command lets first-time users ask Symphony to decide whether setup can be
  skipped, resumed, or rerun without weakening `init` as a config-generation
  primitive.
- A release workflow without an immediate public PyPI publish is still useful:
  it proves artifact integrity, exercises installed CLI behavior, and gives the
  team a staging gate before enabling production credentials.
- Detecting local auth must not become a fragile network dependency. Setup
  should run fast local probes first and reserve network-backed identity or repo
  access checks for tools that already expose a stable CLI status command.

**Scope:**

- `symphony --version` and stable help for production command discovery.
- `symphony onboard` as the first-run wrapper around tutorial, environment
  scan, init generation, and skip/resume behavior.
- Shared setup preflight checks for init/onboard/doctor covering workflow
  presence, Linear token source, GitHub auth source, runner command presence,
  workspace writability, logs/status settings where a workflow exists, and
  remediation commands.
- Release notes in `CHANGELOG.md` and README updates for version, onboarding,
  and release workflow usage.
- GitHub Actions release workflow with dry-run/staging/main controls, artifact
  build, wheel install smoke test, and protected environment hooks.

**Exit criteria:**

- `symphony --version` works from both `uv run symphony` and an installed wheel.
- `symphony onboard --mode automated` never prompts; if setup is already valid
  it skips init and reports the checks, otherwise it fails before writing files
  with exact missing inputs.
- `symphony onboard` in an interactive terminal shows local readiness before
  prompting and can reuse the existing init flow when setup must be generated.
- Existing valid setup is not overwritten unless the user explicitly passes
  `--overwrite`.
- `symphony doctor` and onboarding reports use consistent labels for auth/tool
  sources.
- Release automation can build artifacts, install the wheel, and verify
  `symphony --help`, `symphony --version`, `symphony init --help`,
  `symphony onboard --help`, `symphony doctor --help`, and
  `symphony run --help`.
- PR validation includes focused CLI tests and any release workflow syntax checks
  that can run locally.

### 6.5 Phase 2B: Standalone App And Linear Productionization

**Goal:** Make Symphony approachable and secure after the CLI MVP loop works.

**Scope:**

- Tauri macOS app shell with bundled Python sidecar.
- First-run setup flow for repository selection, Linear auth, state selection,
  Codex availability, workspace root, and `WORKFLOW.md` generation.
- Basic app status view for configuration status, active issues, run state, and
  recent logs.
- OAuth 2.0 / PKCE flow, token refresh, Keychain or credentials-file storage,
  revoke/status commands, and `/api/v1/linear/auth/*` endpoints.
- Linear webhook registration, HMAC verification, async event processing, and
  graceful fallback to polling.
- Optional tunnel support for local development.
- Signed and notarized `.dmg` packaging.
- Auto-update and app preference hardening.

**Exit criteria:**

- The app can be launched from Finder on macOS.
- The app can start and stop the bundled Python sidecar.
- First-run setup can generate or update `WORKFLOW.md` for Linear + Codex.
- State changes in Linear trigger reconcile/dispatch without waiting for the next
  poll tick.
- Invalid webhook signatures are rejected.
- App credentials are stored securely and can be revoked.
- Signed app can be installed through the normal macOS drag-to-Applications flow.
- UI PRs include local test run notes and `.png` captures of changed screens.

### 6.6 Phase 3: Operator Visibility And Approval

**Goal:** Give operators a usable day-to-day control surface before adding more
agent backends.

**Scope:**

- FastAPI SSE event stream if not already complete in MVP.
- Web dashboard with issue state, active sessions, logs, retry counts, and
  approval UI.
- Notification service with ntfy and webhook backends.
- Approval gate endpoints and notification deep links.
- PWA/mobile layout after the dashboard workflow is stable.

**Exit criteria:**

- Operators can inspect running work without reading raw logs.
- Approval/rejection can unblock or stop an agent turn without blocking the main
  orchestrator loop.
- Notification failures are isolated from orchestrator execution.
- UI PRs include screenshot evidence for changed flows.

### 6.7 Phase 4: Multi-Agent Runner Expansion

**Goal:** Add non-Codex runners after the MVP session, tracker, and operator
contracts are stable.

**Order:**

1. Claude Code / Anthropic SDK runner.
2. OpenAI-compatible runner for Hermes, Ollama, vLLM, and hosted compatible
   endpoints.
3. Gemini API runner.
4. GPT-Image-1 generative runner after `task_type` semantics are finalized.

**Exit criteria:**

- Each runner maps provider-specific streaming, tool calls, token usage, and
  failures into the common Symphony event schema.
- `linear_graphql` tool behavior is tested for every agentic runner.
- Provider-specific rate limits and safety blocks surface in observability.

### 6.8 Phase 5: IM Integrations And Distribution Expansion

**Goal:** Extend operator controls into team communication tools and broaden
distribution after the standalone app is stable.

**Scope:**

- Telegram bot.
- Slack bot.
- Additional distribution channels beyond direct `.dmg` download.

**Exit criteria:**

- IM integrations can send key events and process approval/cancel actions.
- UI and integration PRs include screenshot evidence for changed flows.

### 6.9 Phase 6: Backlog And Expansion

**Goal:** Add optional tracker, sandboxing, persistence, and multimodal features
after the primary product is usable.

**Scope:**

- GitHub Issues and Jira tracker adapters.
- Docker/cgroup workspace sandboxing.
- Persistent retry queue.
- Multi-runner dispatch by label/state.
- Vision inputs and other multimodal extensions.

### 6.9 Validation Gates By Layer

| Layer | Required validation |
|---|---|
| Config/workflow | Schema tests, `$VAR` tests, invalid YAML tests, hot reload tests |
| Workspace | Path traversal rejection, root containment, hook execution, cleanup |
| Tracker | Mock Linear GraphQL tests, pagination, auth failure, webhook HMAC |
| Orchestrator | Dispatch priority, bounded concurrency, retry/backoff, reconciliation |
| Agent runners | Event normalization, tool dispatch, timeout/stall handling, token usage |
| HTTP/API | Endpoint contract tests, auth errors, SSE or polling behavior |
| UI | Local test run plus `.png` captures of impacted screens |
| Desktop | Sidecar lifecycle, health polling, preferences persistence, packaging smoke test |

### 6.10 Progress Review Routine

Maintain a lightweight daily progress log when reviewing project status or
choosing the next build items. Each review should inspect local docs, local git
state, remote GitHub PRs, and corresponding Linear tickets, then write the
summary to `daily/dev-log-YYYY-MM-DD-HHMMSS.md` using Hong Kong time.

Each log should record:

- remote PRs in review and recently merged PRs,
- local uncommitted work and branch state,
- relevant Linear project, milestone, and ticket statuses,
- product-doc or tracker mismatches that need cleanup,
- validation run during the review, and
- the next five recommended build items.

When the review exposes a process change or product-contract change, update this
PRD in the same branch so the routine remains discoverable for future agents.

---

## 7. Build Queue

> Ordered work packages. Linear ticket priorities should mirror this order:
> urgent/high for MVP blockers, medium for post-MVP productionization, and low
> for optional expansion.

### 7.1 Phase 0: Guardrails

- [ ] **[Delivery: Repo guardrails]** — Track `AGENTS.md`, update the PR template
  for Linear links and validation evidence, and configure or document Git LFS for
  recurring `.png` review artifacts.

### 7.2 Phase 1: MVP — CLI Linear + Codex

- [x] **[Core: Python skeleton]** — package layout, CLI, logging, test harness,
  core domain models, and startup preflight.
- [x] **[Core: WORKFLOW.md parser]** — YAML front matter, Jinja2 prompt rendering,
  `$VAR` resolution with named missing-variable errors, strict config validation,
  defaults, `~` expansion, and hot reload.
- [x] **[Linear: MVP auth + tracker read path]** — personal API key support,
  token redaction, candidate issue fetch, state refresh, pagination, and
  normalized issue model.
- [x] **[Linear: `linear_graphql` tool]** — scoped GraphQL tool for agent comments,
  state transitions, and PR links using Symphony-managed auth.
- [x] **[Core: Orchestration state machine] (Linear: IN-169)** — dispatch
  ordering, eligibility, claims, bounded global/per-state concurrency,
  retry/backoff entries, continuation retries, stall detection, and
  reconciliation cleanup decisions. Runtime glue attaches polling and worker
  execution to this state core.
- [x] **[Core: Workspace lifecycle] (Linear: IN-171)** — per-issue directories,
  sanitized paths, lifecycle hooks, root containment checks, and terminal
  cleanup controls.
- [x] **[Agent: Runner base classes] (Linear: IN-174)** — runner-neutral
  session, event, token usage, turn result, and task result models plus
  `AgentRunner`, `CLIAgentRunner`, and `APIAgentRunner` abstract contracts.
- [x] **[Agent: Codex runner] (Linear: IN-175)** — Codex app-server JSON-RPC
  adapter with event normalization, timeout/stall handling, approval handling,
  malformed-frame handling, subprocess cleanup, and `linear_graphql` tool
  routing through an injectable tool executor.
- [x] **[HTTP: Minimal status API] (Linear: IN-172)** — framework-independent
  status handler for `/api/v1/state`, `/api/v1/<identifier>`,
  `/api/v1/refresh`, and `/api/v1/health`, plus a FastAPI factory for
  environments where FastAPI is installed.

**Milestone status:** Closed with caveat. Live Linear auth and polling are
verified; live Codex dispatch awaits a disposable active Linear issue and should
be the first Phase 2 gate before desktop or productionization work expands.

### 7.2.1 Phase 1 SPEC Compliance Follow-Up

> These items correct gaps found when comparing the implementation against the
> original OpenAI Symphony SPEC (decided 2026-05-18, isolation-first). They are
> **post-MVP follow-up work**, not Phase 1 blockers — Phase 1 closure stands.
> Full design rationale in §8.1–§8.5.

- [x] **[Core: Per-run workspace isolation] (Linear: IN-286)** — Symphony owns
  workspace setup per the isolation matrix in §8.1. Bare clone + `git worktree`
  per dispatch (working tree and branch isolated; object store shared for monorepo
  efficiency). Per-session env var credential injection. Per-issue log file.
  Application-level crash sweep at startup. See §8.1 for the full isolation matrix.
  - Shipped on `feat/in-286-workspace-isolation`. `symphony/workspace.py` —
    per-run paths `<root>/<workspace_key>/<run_id>`, optional bare-clone +
    `git worktree` mode via `workspace.repo_url`, force-cleanup via
    `git worktree remove --force` + `git branch -D`, `sweep_stale_worktrees()`
    called from the poll-loop startup. `WorkspaceManager.logs_root` plumbs
    per-run log paths through the runtime. PRD §8.1 documents the deliberate
    narrowing of SPEC §9.1–§9.2.
- [ ] **[Core: Blocker eligibility gate] (Linear: IN-287)** — Before dispatching
  any issue, check Linear for unresolved blocking relationships. Skip (log
  `blocker_skip`) without modifying tracker state. Reconsider on the next tick.
- [ ] **[Core: Fail-closed approval gate] (Linear: IN-288)** — When
  `approval_policy: on-request` is set but no approval resolution path exists,
  treat it as a fatal misconfiguration at startup (`symphony doctor` reports it).
  At runtime, an unresolvable approval request aborts the run and moves the issue
  to the failure state.
- [ ] **[Core: Failure-state transition, no auto-retry] (Linear: IN-289)** —
  On non-recoverable failure, move the issue to the configured `failure_state`,
  log the structured failure event, and clean up the workspace (unless
  `keep_on_failure: true`). No Symphony-initiated auto-retry; operator re-queues
  via Linear. Any future retry (SPEC §18.2 persistent queue, Phase 6 backlog)
  must use a fresh worktree. **Prerequisite:** SPEC.md §8.4/§10.7 and
  ARCHITECTURE.md retry flows must be updated before this is implemented — see §8.4.
- [ ] **[Core: Claim race prevention — best-effort state-transition claim] (Linear: IN-290)** —
  Move ticket to `in_progress_state` in Linear before launching the agent as
  a best-effort claim. Linear `updateIssue` is not a compare-and-swap — two
  concurrent instances can both succeed — so this is fully protective only for
  single-instance deployments. Multi-instance strict safety requires the
  Phase 2B claim-comment tie-breaker (see §8.5). Log `claim_succeeded` with
  instance identity. `symphony doctor` warns if `states.in_progress` is not
  configured. See §8.5 for the full design.

### 7.3 Phase 2A: Standalone CLI Onboarding And Packaging

- [x] **[CLI: Onboarding commands] (Linear: IN-205)** — add `symphony init`,
  `symphony doctor`, and `symphony run` while preserving legacy CLI startup.
- [x] **[Auth: Local CLI credentials] (Linear: IN-205)** — resolve Linear API
  keys from env vars, WORKFLOW references/literals, and a private local
  credentials file.
- [x] **[Config: Preset workflow generation] (Linear: IN-205)** — generate
  parseable Linear + Codex `WORKFLOW.md` files from `codex-safe`,
  `codex-autonomous`, and `review-only` presets.
- [x] **[Packaging: Easy install channels] (Linear: IN-205)** — publish
  installation instructions and release artifact workflow for `uv tool install`,
  `pipx`, wheel, and sdist packaging. Native binary builds and Homebrew remain
  follow-on distribution channels once the command surface stabilizes.
- [x] **[CLI: Init orientation] (Linear: IN-257)** — add a short interactive
  terminal tutorial with an English / Simplified Chinese picker, versioned read
  history, reusable tutorial module, expected setup deliverables, productivity
  context, and the next steps in the init flow.
- [x] **[CLI: Paginated init Q&A] (Linear: IN-268)** — break the orientation
  into versioned, bilingual Q&A pages with continue/skip controls and persisted
  completion only after the tutorial is actually shown.
  - Shipped in PR #18. `onboarding_tutorial.py` — versioned pages, continue/skip, bilingual language picker.
- [ ] **[CLI: Onboard UX redesign] (Linear: IN-283)** — redesign `sy onboard`
  with an env-first flow: run environment scan first (derive GitHub org/repo
  from `git remote`, runner presence from PATH, Linear auth from env), show a
  compact status table with color-coded pass/warn/fail icons, ask only for
  the gaps, and move the tutorial to an optional post-setup step. Use
  consistent color semantics (green=ok, yellow=warn, red=missing, cyan=commands)
  throughout scan, config, and tutorial phases.
- [ ] **[CLI: Auto-detect repo shape in onboard] (Linear: IN-284)** — detect
  repo shape automatically (no remote → new project; monorepo signals such as
  `pnpm-workspace.yaml`, `nx.json`, `go.work`, npm `workspaces`, `packages/`
  dir → monorepo; otherwise single repo), then show the result with a
  `github.com/org/______` fill-in for confirmation or correction. No manual
  picker. Monorepo mode adds a self-scoping preamble to the agent prompt;
  new-project mode runs `gh repo create`. Adds `repo_mode` to `InitConfig`.
- [ ] **[CLI: Primary runner picker + cross-vendor CR] (Linear: IN-285)** — when
  both `claude` and `codex` are installed, show an interactive runner picker
  instead of silently auto-selecting. Follow with a code review strategy
  question: cross-vendor (primary implements, other reviews via crosscheck
  pipeline), single-vendor, or skip. Cross-vendor selection writes a `review`
  block to WORKFLOW.md and wires up the crosscheck project. Automated mode
  defaults to claude_code + no review.
- [x] **[Auth: Interactive/automated setup modes] (Linear: IN-268)** — make
  `symphony init` support guided interactive setup and non-prompting automated
  setup while detecting and validating Linear CLI/MCP auth, GitHub access
  through `gh`, Codex CLI auth, and Claude Code CLI auth before writing the
  final workflow.
  - Shipped in PR #18. `cli.py` — `--mode interactive/automated`, TTY auto-detection, `_automated_setup_failures`.
- [x] **[CLI: Setup status preflight] (Linear: IN-268)** — expand
  `symphony doctor` or add a status-style view that reports auth source,
  account identity, repo access, CLI versions, config paths, and exact fix
  commands for each missing dependency.
  - Shipped in PR #23. `doctor_checks` in `cli.py` — Linear auth source, command availability, GitHub auth, exact fix commands.

### 7.4 Phase 2B: Standalone App And Linear Productionization

- [ ] **[Desktop: App shell]** — Tauri shell, embedded web UI, Python sidecar
  start/stop, health polling, and local app preferences.
- [ ] **[Desktop: Setup flow]** — repository picker, Linear auth setup,
  team/project/state selection, Codex availability check, workspace root,
  concurrency, and `WORKFLOW.md` generation.
- [ ] **[UI: App status view]** — setup status, idle/running/completed/failed
  states, issue list, and recent logs inside the app.
- [ ] **[Linear: Credential storage adapters] (Linear: IN-201)** — credential
  store interface; credentials-file adapter with owner-only permissions; macOS
  Keychain adapter via `keyring`; OAuth token fields (access, refresh, expiry);
  redaction utilities; status metadata without exposing token material.
- [ ] **[Linear: OAuth 2.0 / PKCE] (Linear: IN-165)** — PKCE authorization code
  flow, token exchange and refresh, `symphony auth login/status/revoke` commands,
  HTTP endpoints (`/api/v1/linear/auth/start|callback|status|revoke`), and
  personal API-key fallback for headless/CI use.
- [ ] **[Linear: Webhooks] (Linear: IN-166)** — webhook registration, HMAC-SHA256
  verification, async event routing as immediate orchestrator trigger, idempotent
  re-registration on startup, and polling fallback when webhooks are disabled.
- [ ] **[Distribution: Homebrew tap]** — `brew install codatta/symphony/symphony`
  via a `codatta/homebrew-symphony` tap; release workflow job computes SHA256 of
  the published wheel/sdist and bumps the formula on each tagged release.

### 7.5 Phase 3: Operator Visibility And Approval

- [x] **[HTTP: SSE event stream] (Linear: IN-158)** — typed runtime event stream
  for dashboards and future desktop shell. `symphony/event_bus.py` — asyncio
  pub/sub `EventBus` with bounded per-subscriber queues, `subscribe()` async
  context manager, and `event_to_sse_data()` serializer. FastAPI
  `GET /api/v1/events` endpoint with 15 s heartbeat ticks added to
  `create_fastapi_app()`. Events include session lifecycle, turn results, and
  approval requests. Shipped in IN-158 PR.
- [x] **[Core: Approval gate] (Linear: IN-158)** — asyncio.Event-based gate with
  configurable timeout (fail-closed). `symphony/approvals/service.py` —
  `ApprovalGate` dataclass with `asyncio.Event result`, `approved: bool | None`,
  and `to_dict()`. `ApprovalService.wait_for_approval()` uses
  `asyncio.wait_for` with `approval_timeout_ms / 1000`; timeout sets
  `approved = False` (fail-closed). `resolve()` is idempotent (returns False for
  unknown or already-resolved gates). `CodexRunner` gains an optional
  `approval_handler` callback wired through `run_daemon()`. Approval HTTP
  endpoints `POST /api/v1/approvals/{id}/approve|reject` added to both
  `StatusAPI.handle_request()` (thread-safe via
  `asyncio.run_coroutine_threadsafe`) and `create_fastapi_app()`.
  `codex.approval_timeout_ms` config field added (default 300 000 ms). Shipped
  in IN-158 PR.
- [ ] **[UI: Web dashboard / PWA]** — active issues, status badges, logs,
  retry counts, approval UI, and mobile-responsive layout.
- [ ] **[Mobile: Push notifications]** — ntfy and generic webhook backends for
  human review, blocked, stalled, and failed sessions.

### 7.6 Phase 4: Multi-Agent Runners

- [ ] **[Agent: Claude Code]** — Anthropic/Claude Code runner with streaming,
  tool routing, token accounting, and normalized Symphony events.
- [ ] **[Agent: OpenAI-compatible / Hermes]** — OpenAI protocol runner for
  Ollama, vLLM, LM Studio, Hermes, and hosted compatible endpoints.
- [ ] **[Agent: Gemini API]** — Gemini runner with function calling,
  streaming, token usage, and safety-block handling.
- [ ] **[Agent: GPT-Image-1]** — generative image runner after `task_type`
  semantics are finalized.

### 7.7 Phase 5: IM Integrations And Distribution Expansion

- [ ] **[IM: Telegram bot]** — push notifications and inline approval/cancel
  actions.
- [ ] **[IM: Slack bot]** — Slack socket-mode notifications and Block Kit
  approval/cancel actions.
- [ ] **[Distribution: Additional channels]** — packaging beyond direct `.dmg`
  download, such as managed enterprise installation or marketplace submission.

### 7.7 Backlog

- [ ] **[Tracker: GitHub Issues adapter]** — support GitHub Issues as an
  alternative to Linear.
- [ ] **[Tracker: Jira adapter]** — support Jira projects.
- [ ] **[SSH Worker Extension]** — port Appendix A SSH worker extension from the
  Elixir implementation.
- [ ] **[Security: Workspace sandboxing]** — Docker/cgroup-based execution
  isolation per workspace.
- [ ] **[Config: Multi-runner per workflow]** — dispatch different labels or
  states to different agent runners.
- [ ] **[Retry: Persistent queue]** — survive process restarts without losing
  retry state. Each retry entry must use a fresh workspace clone; reusing a
  failed workspace is explicitly prohibited (see §8.4).
- [ ] **[Multimodal: Vision input]** — pass screenshots/images from the workspace
  into agent prompts.

---

## 8. SPEC Alignment Decisions

> Resolved design decisions from comparing this PRD against the original OpenAI Symphony SPEC.
> Guiding principle: **isolation over efficiency** — no code contamination between concurrent agents.

### 8.1 Workspace population and multi-agent isolation (SPEC §9.2–9.3)

**Decision: Symphony owns workspace setup. Each dispatch gets an isolated working tree and a unique branch before the agent is launched.**

**Cross-doc narrowing.** SPEC §9.1 specifies a per-issue workspace path
(`<workspace.root>/<sanitized_issue_identifier>`) and §9.2 states that
"workspaces are reused across runs for the same issue." This PRD deliberately
narrows that contract to per-run isolation: workspace path becomes
`<workspace.root>/<workspace_key>/<run_id>` and every dispatch materializes a
fresh worktree. SPEC.md is the read-only reference design and is not modified
to match this narrowing; ARCHITECTURE.md and module docstrings document the
behavior actually shipped. The motivation is the multi-agent isolation matrix
below — reusing a worktree across runs reintroduces the working-tree, branch,
and log collision risks that the matrix is designed to eliminate.

#### Isolation matrix

| Dimension | Threat | Codex | Claude Code | Owner |
|-----------|--------|-------|-------------|-------|
| **Working tree (local)** | Agent A overwrites Agent B's files mid-run | OS container per session — walls prevent cross-agent filesystem access | Separate `git worktree` directory per dispatch | Symphony (`workspace.py`) |
| **Branch (remote)** | Two agents push to `main`; second push fails or force-clobbers | Branch-per-issue checkout (container walls end at network; remote collision risk is identical) | Branch-per-issue checkout | Symphony — derives name from Linear `gitBranchName` |
| **Process** | Agent kills or starves another agent's subprocess | Container process namespace isolation | OS process group — Symphony tracks PIDs and kills only its own subprocess | Symphony (`orchestrator.py`) |
| **Credentials** | Agent reads another agent's Linear or GitHub token | Container env isolation | Symphony passes resolved tokens as subprocess env vars; no shared credential files between sessions | Symphony (`workspace.py` + runner env injection) |
| **Logs** | Output from concurrent agents interleaved in one file | Container stdout isolation | Per-issue log file at `<logs_root>/<issue-id>/<run-id>.log` | Symphony (`log_file.py`) |
| **Concurrency** | Too many agents starve host resources | `max_workers` cap in WORKFLOW.md | `max_workers` cap in WORKFLOW.md | Symphony (`orchestrator.py`) |
| **Network sandbox** | Agent makes unauthorized outbound calls | Codex container enforces network policy (configurable) | **Not sandboxed** — Claude Code has full host network access | Operator responsibility; Docker/cgroup sandboxing is a Phase 6 backlog item |

Key observations:
- **Remote branch collision** is the same risk for both runners — container walls end at the network boundary. Branch-per-issue is load-bearing for both, not just Claude Code.
- **Network sandbox** is the one dimension where Codex is stronger than Claude Code by default. Teams running Claude Code against sensitive infrastructure should plan for the Phase 6 Docker/cgroup sandboxing item.
- **Credentials** are injected per-session as env vars and never written to shared files, so a compromised agent session cannot read another session's tokens from disk.

#### Workspace strategy: `git worktree` from a shared bare clone

A fresh `git clone` per dispatch is prohibitively expensive for monorepos — gigabytes of files multiplied by concurrent agents. The correct approach shares the object store via a bare clone and materializes only the working tree per dispatch.

```bash
# Once — on first dispatch or when absent:
git clone --bare <repo_url> <workspace_root>/.repo.git

# Before each dispatch — keep the object store current (one fetch per tick):
git -C <workspace_root>/.repo.git fetch --prune origin

# Per dispatch — fast, disk-efficient, branch-isolated:
git -C <workspace_root>/.repo.git worktree add \
  <workspace_root>/<issue-id>/<run-id> -b <branch-name>

# After dispatch — cleanup (force-remove worktree, then force-delete branch):
# --force is required: agents may leave uncommitted/untracked files,
# and git worktree remove refuses dirty worktrees without it.
git -C <workspace_root>/.repo.git worktree remove --force \
  <workspace_root>/<issue-id>/<run-id>
git -C <workspace_root>/.repo.git branch -D <branch-name>
```

Branch name: `<gitBranchName>-<run-id>` (e.g., `feat/in-42-add-login-a1b2c3d`), falling back to `issue/<identifier>-<run-id>`. A per-run suffix is required because `git worktree remove` leaves the local branch behind in the bare repo; a subsequent dispatch on the same issue would fail `worktree add -b` if a same-named branch already exists. `branch -D` (force delete) is required because the feature branch contains unmerged commits relative to the bare repo's HEAD — `branch -d` would always refuse.

| Approach | Network cost | Disk cost per agent | Monorepo safe |
|----------|-------------|---------------------|---------------|
| `git clone --depth 1` per dispatch | Full working tree × N agents | Full working tree × N | No |
| Bare clone + `git worktree` | One fetch per tick, shared | Working tree only; object store shared | Yes |

Additional constraints:
- The object store is shared — no per-dispatch network transfer.
- Worktree add/remove failure aborts the dispatch. Because the §8.5 dispatch sequence claims the issue (moves it to `in_progress_state`) **before** workspace setup, a workspace failure at this point occurs after Linear state has already changed — the rollback and abandon rules in §8.5 step 4 apply. Only if workspace setup is attempted before any claim step does the failure leave issue state unchanged and the dispatch retryable on the next tick.
- Stale worktrees from crashed dispatches must be cleaned up at startup via an application-level sweep using `git worktree remove --force`; `git worktree prune` only removes stale metadata for worktrees whose paths are already gone — directories that survive a crash remain registered and will not be pruned automatically.
- Agents may not create their own worktrees or branches; Symphony owns the workspace lifecycle.

### 8.2 Blocker eligibility (SPEC §8.2)

**Decision: Check blocker eligibility before dispatch in Phase 1 (not deferred).**

Before dispatching any issue, the orchestrator checks whether that issue has unresolved blocking relationships in Linear. If blockers exist, the issue is skipped and a structured warning is logged (`blocker_skip` event). The issue remains in its current state and will be reconsidered on the next poll tick.

- No blocker state is written to Linear (Symphony does not modify tracker state for the skip).
- Unresolvable blocker loops (A blocks B, B blocks A) are treated as permanent skips until operator intervention.

**Rationale:** Dispatching a blocked issue wastes agent compute and can produce PRs that cannot be merged until the upstream work lands.

### 8.3 Approval gate (SPEC §10.5)

**Decision: Fail-closed — Symphony will not dispatch if no approval state is configured and an approval gate fires.**

When the WORKFLOW.md `approval_policy` is `on-request` but no `approval_state` is defined, Symphony treats it as a configuration error at startup (`symphony doctor` reports it as a fatal misconfiguration). At runtime, if an agent requests approval and no approval resolution path exists, the run is aborted and the issue is moved to the failure state.

Auto-approve (`never`) remains the Phase 1 default preset behavior. Fail-closed only activates when `approval_policy: on-request` is explicitly set.

**Rationale:** Silent approval (fail-open) in a multi-agent environment risks unreviewed actions landing in production. A misconfigured approval gate should surface as a hard error, not silently proceed.

### 8.4 Run failure handling (SPEC §18.2)

**Decision: On run failure, move the issue to the configured failure state and stop. No auto-retry into the same workspace.**

When a run exits with a non-recoverable error (agent crash, stall timeout exhausted, approval rejected), Symphony:

1. Moves the Linear issue to the workflow-defined `failure_state` (e.g., `Cancelled`).
2. Logs the structured failure event with reason and last turn output.
3. Deletes the workspace unless `keep_on_failure: true` is set.
4. Does **not** re-queue or retry automatically.

The operator decides whether to re-queue by moving the issue back to an active state in Linear.

A persistent retry queue (SPEC §18.2) remains in the Phase 6 backlog. When implemented, it must never reuse the same workspace — each retry provisions a fresh worktree.

**Cross-doc conflict and resolution:** SPEC.md §8.4/§10.7 and ARCHITECTURE.md require failure-driven retries to be scheduled automatically. This PRD's decision narrows, not contradicts, that contract: the constraint is **no auto-retry into the same workspace**, not no retry at all. Retries are permitted provided they use a fresh worktree. SPEC.md and ARCHITECTURE.md must be updated before IN-289 is implemented to reflect this narrowing — specifically, any retry scheduling must provision a new workspace rather than resuming the failed one. Until those documents are updated, AGENTS.md instructs implementers to treat the cross-doc conflict as a blocker on IN-289.

**Rationale:** Auto-retry into a dirty workspace risks compounding the original failure. The operator is better positioned to judge whether a retry is warranted after reading the failure log.

### 8.5 Multi-instance claim race prevention (IN-290)

**Decision: Linear state transition is a best-effort claim guard, not an atomic compare-and-set. Symphony moves the ticket to `in_progress_state` before launching the agent — not after, not mid-run. This eliminates the common case (sequential polls from one instance) and shrinks the race window for multi-instance deployments, but does not provide exclusive-claim guarantees. Phase 2B adds a claim-comment tie-breaker for strict multi-instance safety.**

#### The problem

Multiple Symphony instances (or multiple poll workers within one instance) can see the same ticket in a dispatchable state at nearly the same time. Without any claim step, both will dispatch the same issue, producing:

- Duplicate agent sessions running against the same ticket
- Duplicate PRs opened and potentially merged
- Wasted agent compute proportional to the number of instances

An in-memory lock only protects within one process. Linear must be the coordination point.

#### Important: Linear `updateIssue` is not a compare-and-set

Linear's `updateIssue` mutation is a plain update with no conditional semantics. If two Symphony instances poll the same `Todo` issue before either update propagates, both can call `updateIssue → In Progress` and both will receive success. Linear has no built-in mechanic to make the second caller fail.

The state-transition claim therefore works as follows:

- **Within one instance:** poll workers are serialized by the dispatch loop, so only one worker ever calls `updateIssue` per issue per tick. This case is fully protected.
- **Across multiple instances:** the race window equals the API propagation delay plus the poll interval. For typical deployments (poll interval ≥ 30 s, instances in different data-centers) the practical duplicate rate approaches zero, but the guarantee does not hold under tight timing or network partition.

#### Solution: state-transition as best-effort claim

```
Dispatchable states        Claimed state          Handoff / terminal
(queued_states)      →     (in_progress_state)  →  (handoff_state / failure_state)
e.g. "Todo"                "In Progress"            "In Review" / "Cancelled"
```

**Dispatch sequence (before agent launch):**

1. Orchestrator polls Linear for issues in `queued_states` (e.g., `Todo`).
2. Before launching any agent, Symphony immediately moves the issue to `in_progress_state` (e.g., `In Progress`) via `linear_graphql`.
3. After the state move, Symphony re-fetches the issue to verify the current state is `in_progress_state`. If verification fails (e.g., the issue was moved again by a concurrent instance), Symphony aborts the dispatch and does not launch the agent.
4. **If workspace setup fails after the claim** (e.g., `git worktree add` errors), Symphony must determine whether it is safe to roll back before touching Linear state:
   - **If the re-fetch in step 3 confirmed this instance is the verified owner** (i.e., single-instance deployment, or claim-comment tie-breaker confirmed first-place): move the issue back to `queued_states[0]` and log a `claim_rollback` event.
   - **If ownership cannot be confirmed** (multi-instance deployment without claim-comment tie-breaker): do NOT roll back — another instance may have already launched an agent against this ticket. Log a `claim_abandoned` event and alert the operator to inspect and manually re-queue if needed.
   Never roll back unconditionally: if a second instance claims the ticket due to the non-CAS race and then hits a setup failure, an unconditional rollback would re-queue a ticket that the first (winning) instance is actively running.
5. Agent runs. On completion, agent (or Symphony on handoff) moves to `handoff_state`.
6. On failure, Symphony moves to `failure_state` (per §8.4). Not back to `queued_states` — operator re-queues manually.

Issues already in `In Progress` when Symphony polls are skipped — they are either claimed by another instance or being worked on by a human.

**WORKFLOW.md config:**

```yaml
states:
  queued:      ["Todo"]           # Symphony polls these; tickets here are eligible for dispatch
  in_progress: "In Progress"      # Symphony moves here immediately on claim (before agent starts)
  handoff:     "In Review"        # Agent moves here when work is done
  terminal:    ["Done", "Cancelled"]
  failure:     "Cancelled"        # Symphony moves here on non-recoverable failure
```

`in_progress` replaces the old `active` catch-all.

#### Phase 2B: claim-comment tie-breaker (strict multi-instance safety)

For teams running multiple Symphony instances with short poll intervals, a claim-comment tie-breaker can eliminate the remaining race:

1. Before the state transition, fetch the issue's state-transition history and find the `createdAt` timestamp of the `IssueHistory` entry where the issue was last moved into a `queued_states` value (call it `queued_since`). This is a stable, per-requeue marker: it only changes when an operator explicitly moves the ticket back to a queued state, not when other fields are edited. Do **not** use `issue.updatedAt` — that timestamp changes on any field edit and will diverge between two instances that read the issue at slightly different times, causing each to filter out the other's claim comment and both believe they won.
2. Post a claim comment: `{"claim": "symphony", "instance_id": "<host>:<pid>", "run_id": "<run-id>", "queued_since": "<iso8601>"}`.
3. Call `updateIssue → in_progress_state`.
4. Re-fetch the issue's comments ordered by `createdAt`. Consider only claim comments whose `queued_since` matches the value read in step 1 — this scopes the tie-breaker to the current dispatch attempt and excludes stale comments from prior crashed or aborted instances. If the earliest matching claim comment's `instance_id` does not match this instance, abort and do not launch.

Stale claim comments (from a crashed instance on a prior attempt) have an older `queued_since` (the state-transition history timestamp from before the operator re-queued) and are therefore ignored after the operator re-queues the ticket. This prevents a zombie comment from permanently blocking future dispatches.

This reduces the race to the Linear write ordering of two near-simultaneous comment mutations — a much smaller window than polling. Full elimination requires Linear webhook push (replaces polling entirely and removes the window).

#### Multi-instance observability

Symphony logs a `claim_succeeded` event when it successfully claims and verifies a ticket, with fields: `issue_id`, `instance_id` (daemon hostname + PID), `claimed_at`. On workspace failure, it logs a `claim_rollback` event with `reason`. This makes duplicate dispatch visible in logs without requiring a shared database.

#### `symphony doctor` check

Doctor warns if `states.in_progress` is not configured in WORKFLOW.md, since omitting it disables the claim guard entirely.

---

## 9. Open Questions

1. **Linear OAuth app registration:** Should Symphony ship with a shared OAuth client_id (users install the Symphony Linear app from Linear's marketplace), or does each team register their own Linear application with their own client_id/secret?
2. **Hermes deployment:** Is the target Ollama on localhost, a remote vLLM cluster, or a hosted inference endpoint?
2. **GPT-Image-1 workflow:** Should Symphony auto-commit generated images and open a PR, or just save to workspace and leave the commit to a coding agent in a subsequent issue?
3. **Runner selection:** Should WORKFLOW.md support a single `runner` per workflow, or a per-label/per-state dispatch map (e.g., `In Progress → claude_code`, `Merging → codex`)?
4. **Tracker scope:** Linear-only for the initial Python implementation, or should GitHub Issues be co-designed from the start to avoid Linear-specific leakage in the adapter interface?
5. **Desktop app distribution:** Direct `.dmg` download from GitHub Releases, or target the Mac App Store (requires sandboxing and notarization review)?
6. **IM backend priority:** Telegram (simpler setup, free, long polling) or Slack (enterprise-friendly, socket mode)? Both are designed; see ARCHITECTURE.md §7.5 for the trade-off table.
7. **Approval gate scope:** Is remote approval of agent action gates (approve/reject from phone) a launch requirement, or is read-only monitoring + `Human Review` notifications sufficient for v1?
