# Symphony — System Architecture

> Reference design for the Python implementation with Mac desktop app and IM-based remote control.

---

## 1. System Overview

```
╔══════════════════════════════════════════════════════════════════════════╗
║                         OPERATOR INTERFACES                              ║
╠═══════════════════╦════════════════════╦═════════════════════════════════╣
║  Mac Desktop App  ║   Web Dashboard    ║   IM Remote Control             ║
║  (Tauri v2)       ║   (React PWA)      ║   (Telegram Bot / Slack Bot)    ║
║                   ║                   ║                                  ║
║  • System tray    ║  • Live dashboard  ║  • Push notifications           ║
║  • Native notifs  ║  • Agent status    ║  • Inline action buttons        ║
║  • Embedded UI    ║  • Token metrics   ║  • /status /cancel /approve     ║
║  • Sidecar mgmt   ║  • PWA install     ║  • Approval gate flow           ║
╚═════════╤═════════╩════════╤═══════════╩══════════════╤══════════════════╝
          │  HTTP + SSE       │  HTTP + SSE               │  Bot API (out)
          │                   │                           │  Webhook / Socket (in)
          └───────────────────┼───────────────────────────┘
                              │
╔═════════════════════════════▼════════════════════════════════════════════╗
║                     SYMPHONY CORE  (Python 3.12 / asyncio)               ║
║                                                                          ║
║  ┌────────────────────────────────────────────────────────────────────┐  ║
║  │                         Event Bus                                  │  ║
║  │        typed SymphonyEvent pub/sub  (asyncio.Queue fan-out)        │  ║
║  └───────┬──────────────────────────────────────────────┬────────────┘  ║
║          │                                              │               ║
║  ┌───────▼───────────────┐              ┌───────────────▼─────────────┐  ║
║  │      Orchestrator     │              │    Notification Service      │  ║
║  │  ───────────────────  │              │  ─────────────────────────  │  ║
║  │  poll loop / tick     │◄─ approval ──┤  TelegramBackend            │  ║
║  │  dispatch / claims    │   resolve    │  SlackBackend               │  ║
║  │  retry / backoff      │              │  WebhookBackend             │  ║
║  │  reconciliation       │              │  SSEBroadcaster             │  ║
║  └───────┬───────────────┘              └─────────────────────────────┘  ║
║          │                                                               ║
║  ┌───────▼───────────────────────────────────────────────────────────┐   ║
║  │                    Agent Runner Registry                           │   ║
║  │   CodexRunner  │  ClaudeCodeRunner  │  GeminiRunner  │  ...       │   ║
║  └───────┬───────────────────────────────────────────────────────────┘   ║
║          │                                                               ║
║  ┌───────▼──────────────┐   ┌──────────────────────┐                    ║
║  │   Workspace Manager  │   │   Linear Tracker      │                    ║
║  │   + Hook Executor    │   │   Adapter             │                    ║
║  └──────────────────────┘   └──────────────────────┘                    ║
║                                                                          ║
║  ┌────────────────────────────────────────────────────────────────────┐  ║
║  │                      FastAPI HTTP Server                           │  ║
║  │  REST  /api/v1/state          SSE   /api/v1/events                 │  ║
║  │        /api/v1/<id>                 /api/v1/health                 │  ║
║  │        /api/v1/refresh        Bot   /telegram/webhook              │  ║
║  │        /api/v1/approvals/<id>       /slack/events                  │  ║
║  └────────────────────────────────────────────────────────────────────┘  ║
╚══════════════════════════════════════════════════════════════════════════╝
          │
          │  Subprocess (local or SSH)
          ▼
╔═════════════════════════════════════════════════════════════════════════╗
║                      AGENT SUBPROCESSES                                  ║
║   codex app-server   │   claude --print   │   gemini   │   ...          ║
╚═════════════════════════════════════════════════════════════════════════╝
```

---

## 2. Core Process Model

Symphony runs as a **single Python asyncio event loop**. All concurrency is cooperative:

```
asyncio event loop
├── orchestrator_tick()         runs every poll_interval_ms
│   ├── reconcile_running()
│   ├── fetch_candidates()
│   └── dispatch_eligible()
│
├── agent_worker(issue)         one Task per running issue
│   ├── workspace.create()
│   ├── runner.start_session()
│   └── runner.run_turn() × N  streams events via asyncio.Queue
│
├── http_server()               FastAPI + uvicorn ASGI
│   ├── REST endpoints
│   ├── SSE endpoint            asyncio.Queue per connected client
│   └── Bot webhook endpoints
│
├── notification_service()      subscribes to Event Bus
│   ├── telegram_backend()      aiogram long-poll or webhook
│   └── slack_backend()         slack_bolt socket mode
│
└── workflow_watcher()          watchfiles → hot reload WORKFLOW.md
```

The orchestrator is the single authority for state mutation. Agent workers, the HTTP server, and the notification service are all read-only consumers of orchestrator state — they never mutate it directly. They communicate back to the orchestrator via message queues (worker results) or API calls (approval resolutions).

---

## 3. Component Responsibilities

### Orchestrator (`orchestrator.py`)

Owns all scheduling state in a single `OrchestratorState` dataclass. No other module mutates this state.

```
OrchestratorState
  poll_interval_ms         current effective interval
  max_concurrent_agents    current limit
  running: dict            issue_id → RunningEntry
  claimed: set             issue IDs reserved or running
  retry_attempts: dict     issue_id → RetryEntry
  completed: set           bookkeeping only
  codex_totals             aggregate token + runtime counters
  codex_rate_limits        latest rate limit snapshot
  pending_approvals: dict  approval_id → ApprovalGate
```

### Event Bus (`event_bus.py`)

Fan-out pub/sub backed by `asyncio.Queue`. The orchestrator `publish(event)` — all subscribers receive a copy. Subscribers that fall behind are dropped (bounded queues, `maxsize=500`).

```python
class EventBus:
    async def publish(self, event: SymphonyEvent) -> None
    def subscribe(self) -> AsyncIterator[SymphonyEvent]   # returns a queue reader
```

### Notification Service (`notifications/service.py`)

Subscribes to the Event Bus and dispatches to all configured backends. Runs in its own `asyncio.Task`. A backend failure never propagates to the orchestrator.

### Agent Runner Registry (`agents/registry.py`)

Maps `runner` config string to a `AgentRunner` class. New runners register themselves here. The orchestrator calls `registry.get(config.agent.runner)` and gets back a runner instance.

### Approval Service (`approvals/service.py`)

Manages the lifecycle of approval gates:

```python
class ApprovalGate:
    id: str
    session_id: str
    issue_identifier: str
    prompt: str              # what the agent is requesting approval for
    created_at: datetime
    timeout_ms: int
    result: asyncio.Event    # set when resolved
    approved: bool | None    # None until resolved
```

The orchestrator creates a gate, awaits `gate.result` with a timeout, then reads `gate.approved`. The HTTP endpoint and IM bot callbacks call `approval_service.resolve(id, approved)`.

---

## 4. Event Bus — Event Types

```python
class EventType(str, Enum):
    # Agent lifecycle
    AGENT_STARTED          = "agent_started"
    TURN_COMPLETED         = "turn_completed"
    TURN_FAILED            = "turn_failed"
    AGENT_FINISHED         = "agent_finished"       # normal worker exit

    # Attention events — these trigger IM notifications
    HUMAN_REVIEW           = "human_review"          # issue moved to review state
    AGENT_BLOCKED          = "agent_blocked"         # missing credential / hard stop
    AGENT_STALLED          = "agent_stalled"         # stall timeout fired
    WORKER_FAILED          = "worker_failed"         # retries exhausted
    APPROVAL_REQUESTED     = "approval_requested"    # gate waiting on human

    # Approval resolution
    APPROVAL_RESOLVED      = "approval_resolved"

    # System
    CONFIG_RELOADED        = "config_reloaded"
    TICK_COMPLETED         = "tick_completed"        # heartbeat for SSE keepalive
```

```python
class SymphonyEvent(BaseModel):
    type: EventType
    issue_id: str | None = None
    issue_identifier: str | None = None
    session_id: str | None = None
    message: str = ""
    data: dict = {}
    timestamp: datetime = Field(default_factory=datetime.utcnow)
```

---

## 5. HTTP API Surface

All endpoints are served by FastAPI on `localhost:<port>` (loopback only by default).

### REST endpoints

```
GET  /api/v1/health
     → 200 {"status": "ok", "running": 3, "uptime_s": 1234}

GET  /api/v1/state
     → OrchestratorSnapshot (running, retrying, codex_totals, rate_limits)

GET  /api/v1/<issue_identifier>
     → per-issue runtime detail, workspace path, recent events
     → 404 if unknown

POST /api/v1/refresh
     → queues immediate poll + reconcile tick
     → 202 {"queued": true}

POST /api/v1/approvals/<approval_id>/approve
POST /api/v1/approvals/<approval_id>/reject
     → resolves the gate; 404 if unknown or expired
     → 200 {"resolved": true, "approved": true/false}

POST /api/v1/sessions/<session_id>/cancel
     → terminates the running worker; schedules retry
     → 200 or 404
```

### SSE endpoint

```
GET /api/v1/events
Content-Type: text/event-stream

data: {"type": "tick_completed", "timestamp": "..."}
data: {"type": "agent_started", "issue_identifier": "MT-42", ...}
data: {"type": "human_review",  "issue_identifier": "MT-55", ...}
```

Each connected client gets its own `asyncio.Queue` subscription to the Event Bus. The SSE stream sends a `tick_completed` heartbeat every 15 seconds to keep connections alive through proxies.

### Bot webhook endpoints

```
POST /telegram/webhook         Telegram Bot API → aiogram dispatcher
POST /slack/events             Slack Events API → slack_bolt handler
POST /slack/actions            Slack interactive components (block actions)
```

These are only active when the respective backend is configured.

---

## 6. Agent Runner Interface

```python
class AgentRunner(ABC):
    """Base class for all agent backends."""

    @abstractmethod
    async def start_session(
        self,
        workspace: Path,
        worker_host: str | None = None,
    ) -> AgentSession: ...

    @abstractmethod
    async def run_turn(
        self,
        session: AgentSession,
        prompt: str,
        issue: Issue,
        on_event: Callable[[SymphonyEvent], Awaitable[None]],
    ) -> TurnResult: ...

    @abstractmethod
    async def stop_session(self, session: AgentSession) -> None: ...
```

`TurnResult` has `.success: bool`, `.exit_reason: str`, `.usage: TokenUsage | None`.

For generative runners (GPT-Image-1) that have no session concept:

```python
class GenerativeRunner(ABC):
    @abstractmethod
    async def run_task(
        self,
        workspace: Path,
        prompt: str,
        issue: Issue,
        on_event: Callable[[SymphonyEvent], Awaitable[None]],
    ) -> TaskResult: ...
```

The `AgentRunnerRegistry` returns the right base class based on `config.agent.task_type`.

---

## 7. Operator Interfaces

### 7.1 Web Dashboard

A React (or Svelte) SPA served at `/` by FastAPI. Connects to `/api/v1/events` for real-time updates via `EventSource`. Also functions as a PWA: `manifest.json` at `/manifest.json` enables "Add to Home Screen" on iOS/Android.

The same frontend bundle is embedded in the Tauri desktop app — no duplication.

### 7.2 Mac Desktop App (Tauri v2)

```
Tauri App (Rust shell)
│
├── main.rs
│   ├── on_ready:     spawn Python sidecar, poll /api/v1/health
│   ├── on_quit:      SIGTERM sidecar, wait max 5s, SIGKILL
│   ├── tray_menu:    [Open Dashboard] [Running: N] [Stop] [Quit]
│   └── sse_thread:   subscribe /api/v1/events → native notifications
│
├── sidecar/symphony  (PyInstaller bundle, registered in tauri.conf.json externalBin)
│
└── src/              (React frontend, same code as web dashboard)
```

**Sidecar startup sequence:**

```
User opens Symphony.app
  → Tauri main.rs: Command::new_sidecar("symphony")
      .args(["--headless", "--port", "7337", workflow_path])
      .spawn()
  → poll GET /api/v1/health every 500ms, timeout 10s
  → on 200: load WebView → http://localhost:7337/
  → subscribe GET /api/v1/events (via reqwest SSE reader in Rust)
  → on HUMAN_REVIEW / WORKER_FAILED / AGENT_BLOCKED events:
       tauri_plugin_notification::send(title, body)
```

**Menubar icon state:**

```
♩ 0      idle (no running agents)
♩ 3      3 agents running (green)
♩ ! 1    1 agent needs attention (amber — human_review or blocked)
♩ ✕      daemon not responding (red)
```

**Key Tauri plugins:**

```toml
tauri-plugin-shell          # sidecar spawn + lifecycle
tauri-plugin-notification   # native macOS User Notifications
tauri-plugin-single-instance# prevent duplicate daemons
tauri-plugin-store          # persist preferences (workflow path, port)
tauri-plugin-updater        # auto-update from GitHub Releases
```

**Distribution:** `tauri build` → signed `.dmg` via GitHub Actions. No Mac App Store initially (sandboxing incompatible with subprocess spawning).

---

### 7.3 Telegram Bot

**Library:** `aiogram` 3.x (modern Python async)

**Operation mode:**
- Development: long polling (no public URL needed)
- Production: webhook via `POST /telegram/webhook`

Configured in WORKFLOW.md:

```yaml
notifications:
  telegram:
    token: $TELEGRAM_BOT_TOKEN
    chat_id: $TELEGRAM_CHAT_ID    # group chat or operator user ID
    mode: polling                  # polling | webhook
    webhook_url: $SYMPHONY_URL    # required when mode: webhook
```

**Notification message format (Markdown):**

```
🔍 *MT-42 — Human Review*
Title: Add retry logic to payment processor
PR: https://github.com/org/repo/pull/88

[Open PR](https://...) [Open Issue](https://...)
```

```
⚠️ *MT-55 — Agent Blocked*
Missing credential: STRIPE_SECRET_KEY
Action needed: add secret to workspace env

[Open Dashboard](http://localhost:7337/MT-55)
```

```
🔐 *MT-60 — Approval Requested*
Session: thread-8-turn-3
Command: `git push --force origin feat/MT-60`

[Approve ✓] [Reject ✗]  (expires in 5 min)
```

**Command handling:**

```
/status          → GET /api/v1/state, format summary
/cancel MT-42    → POST /api/v1/sessions/<id>/cancel
/refresh         → POST /api/v1/refresh
/approve <id>    → POST /api/v1/approvals/<id>/approve
/reject <id>     → POST /api/v1/approvals/<id>/reject
```

Inline keyboard callbacks encode: `"approve:abc123"`, `"reject:abc123"`, `"cancel:MT-42"`. The bot resolves them by calling the Symphony HTTP API.

**aiogram integration in `notifications/telegram.py`:**

```python
class TelegramBackend(NotificationBackend):
    router: Router                 # aiogram command/callback handlers
    bot: Bot
    dp: Dispatcher

    async def start(self) -> None:
        # registers /start /status /cancel /approve /reject commands
        # starts polling or registers webhook
        await dp.start_polling(bot)   # dev mode

    async def send_notification(self, event: SymphonyEvent) -> None:
        text, keyboard = self._format(event)
        await bot.send_message(chat_id, text, reply_markup=keyboard)

    async def _on_callback(self, query: CallbackQuery) -> None:
        action, approval_id = query.data.split(":")
        await self.symphony_api.post(f"/api/v1/approvals/{approval_id}/{action}")
        await query.answer("Done")
```

---

### 7.4 Slack Bot

**Library:** `slack_bolt` with `AsyncApp` in Socket Mode (no public URL required).

Configured in WORKFLOW.md:

```yaml
notifications:
  slack:
    bot_token: $SLACK_BOT_TOKEN
    app_token: $SLACK_APP_TOKEN     # required for socket mode
    channel: $SLACK_CHANNEL_ID      # #symphony-alerts or channel ID
```

**Socket Mode** means Slack opens an outbound WebSocket from Symphony to Slack's servers — no inbound port needed. This is the same user experience as Telegram long polling.

**Message format (Block Kit):**

```json
{
  "blocks": [
    {"type": "header", "text": {"type": "plain_text", "text": "🔍 MT-42 — Human Review"}},
    {"type": "section", "text": {"type": "mrkdwn", "text": "Add retry logic to payment processor"}},
    {"type": "actions", "elements": [
      {"type": "button", "text": {"type": "plain_text", "text": "Open PR"},
       "url": "https://github.com/org/repo/pull/88"},
      {"type": "button", "text": {"type": "plain_text", "text": "Open Issue"},
       "url": "https://linear.app/..."}
    ]}
  ]
}
```

**Slash commands:** `/symphony status`, `/symphony cancel MT-42`, `/symphony approve <id>`

**`slack_bolt` integration in `notifications/slack.py`:**

```python
class SlackBackend(NotificationBackend):
    app: AsyncApp   # slack_bolt

    async def start(self) -> None:
        handler = AsyncSocketModeHandler(app, app_token)
        await handler.start_async()

    async def send_notification(self, event: SymphonyEvent) -> None:
        blocks = self._build_blocks(event)
        await app.client.chat_postMessage(channel=channel, blocks=blocks)

    @app.action("approve_action")
    async def _on_approve(self, ack, body):
        await ack()
        approval_id = body["actions"][0]["value"]
        await self.symphony_api.post(f"/api/v1/approvals/{approval_id}/approve")
```

---

### 7.5 Telegram vs Slack — When to use which

| Criterion | Telegram | Slack |
|---|---|---|
| No public URL needed | ✅ long polling | ✅ socket mode |
| Enterprise team already on Slack | — | ✅ |
| Easiest dev setup | ✅ (just a bot token) | ⚠️ (needs App config + socket token) |
| Interactive buttons (approvals) | ✅ inline keyboard | ✅ block kit |
| Mobile push reliability | ✅ excellent | ✅ good |
| Self-hosted option | ✅ (Telegram gateway can be self-hosted) | ❌ |
| Cost | Free | Free tier; paid for large teams |

Configure one or both. `NotificationService` iterates all configured backends.

---

## 8. Approval Gate Protocol

End-to-end flow when `codex.approval_policy: on-request`:

```
Agent subprocess requests approval
         │
         ▼
AgentRunner.run_turn() → yields ApprovalRequiredEvent
         │
         ▼
Orchestrator.handle_approval_request(session_id, prompt)
  1. Creates ApprovalGate(id=uuid4(), timeout=config.approval_timeout_ms)
  2. Stores in state.pending_approvals[gate.id]
  3. Publishes APPROVAL_REQUESTED to Event Bus
         │
         ├──► TelegramBackend: sends message with [Approve] [Reject] inline buttons
         ├──► SlackBackend: sends Block Kit message with action buttons
         └──► SSEBroadcaster: streams to web/desktop dashboard
         │
  4. awaits asyncio.wait_for(gate.result.wait(), timeout_s)
         │
    ┌────┴──────────────────────────────────────────────────────────┐
    │  Path A: Operator taps [Approve] in Telegram/Slack            │
    │    → bot callback → POST /api/v1/approvals/<id>/approve       │
    │    → ApprovalService.resolve(id, approved=True)               │
    │    → gate.approved = True; gate.result.set()                  │
    │                                                               │
    │  Path B: Operator taps [Reject]                               │
    │    → gate.approved = False; gate.result.set()                 │
    │                                                               │
    │  Path C: Timeout elapsed (default 5 min)                      │
    │    → asyncio.TimeoutError caught                              │
    │    → treated as rejection                                     │
    └────┬──────────────────────────────────────────────────────────┘
         │
         ▼
  5. AgentRunner receives resolution
     - approved=True  → passes approval to agent subprocess, turn continues
     - approved=False → fails current turn → orchestrator schedules retry
  6. Publishes APPROVAL_RESOLVED to Event Bus
  7. Removes gate from state.pending_approvals
```

---

## 9. Key Data Flows

### Flow A: Issue dispatched → agent finishes → moves to Human Review

```
Poll tick
  → Linear: fetch active issues → [MT-42 "In Progress"]
  → dispatch_issue(MT-42)
  → asyncio.Task: agent_worker(MT-42)
      → workspace.create()
      → runner.start_session(workspace)
      → runner.run_turn(session, prompt)   # streams events for minutes/hours
          → EventBus.publish(AGENT_STARTED)   → SSE → desktop notif (subtle)
          → EventBus.publish(TURN_COMPLETED)  → SSE
          → Agent: calls linear_graphql → update_issue(state="Human Review")
      → runner.stop_session()
  → worker exits normally
  → orchestrator: schedule continuation retry (1s)

Retry fires:
  → Linear: fetch active issues → MT-42 no longer in active_states
  → orchestrator: release claim for MT-42
  → EventBus.publish(HUMAN_REVIEW, issue_identifier="MT-42")
      → TelegramBackend: "🔍 MT-42 — Human Review  [Open PR] [Open Issue]"
      → SlackBackend: Block Kit message to #symphony-alerts
      → SSEBroadcaster: update web dashboard
      → Desktop: Tauri shows native macOS notification
```

### Flow B: Desktop app cold start

```
User double-clicks Symphony.app
  → Tauri: reads stored workflow path from tauri-plugin-store
  → Tauri: Command::new_sidecar("symphony")
            .args(["--headless", "--port", "7337", workflow_path])
            .spawn()
  → Tauri: GET /api/v1/health every 500ms (max 10s)
  → 200: WebView loads http://localhost:7337/
  → Tauri Rust thread: reqwest EventSource → /api/v1/events
  → on HUMAN_REVIEW event: tauri_plugin_notification::send(...)
  → user sees tray icon: "♩ 0"

User changes workflow:
  → Preferences window → pick new WORKFLOW.md path
  → Tauri: store.set("workflow_path", new_path)
  → Tauri: SIGTERM sidecar
  → Tauri: respawn with new path
```

### Flow C: Stalled agent, operator cancels from Telegram

```
Orchestrator.reconcile_running():
  → MT-60 last event was 6 minutes ago, stall_timeout_ms=300000
  → terminate worker → EventBus.publish(AGENT_STALLED, issue_id="...")
      → TelegramBackend:
          "⚠️ MT-60 — Agent Stalled
           No activity for 6 minutes.
           [Cancel] [Retry Now]"
  → schedules exponential backoff retry (fresh workspace provisioned per SPEC §8.4 — failed workspace NOT reused)

Operator taps [Cancel]:
  → Telegram inline callback → POST /api/v1/sessions/<id>/cancel
  → orchestrator: release claim for MT-60, clear retry queue
  → TelegramBackend: edit message → "✓ MT-60 cancelled."
```

---

## 10. Module Layout

```
symphony/
├── symphony/
│   ├── cli.py                        # typer: --port, --logs-root, --headless, workflow path
│   ├── orchestrator.py               # OrchestratorState, poll loop, dispatch, retry
│   ├── config.py                     # pydantic schema + $VAR + ~ expansion
│   ├── workflow.py                   # WORKFLOW.md loader, jinja2 strict renderer
│   ├── workspace.py                  # per-issue dirs, hook executor, safety invariants
│   ├── event_bus.py                  # EventBus pub/sub (asyncio.Queue fan-out)
│   ├── path_safety.py                # workspace root containment checks
│   ├── log_file.py                   # structured logging sinks
│   │
│   ├── tracker/
│   │   ├── base.py                   # IssueTrackerAdapter ABC
│   │   ├── linear.py                 # GraphQL queries, pagination, normalization
│   │   ├── linear_auth.py            # OAuth flow, token exchange, scope validation
│   │   └── linear_webhook.py         # webhook registration, HMAC verify, event routing
│   │
│   ├── auth/
│   │   ├── token_store.py            # TokenStore: env → WORKFLOW.md → keychain → file
│   │   └── credentials.py            # ~/.symphony/credentials.json (0o600)
│   │
│   ├── agents/
│   │   ├── base.py                   # AgentRunner / GenerativeRunner ABCs + models
│   │   ├── registry.py               # runner_name → class mapping
│   │   ├── codex.py                  # Codex app-server JSON-RPC stdio adapter
│   │   ├── claude_code.py            # Anthropic SDK streaming multi-turn
│   │   ├── gemini_api.py             # google-genai SDK function-calling
│   │   ├── openai_compatible.py      # OpenAI SDK + base_url override (Hermes, etc.)
│   │   └── gpt_image.py              # openai.images.generate + workspace file save
│   │
│   ├── notifications/
│   │   ├── base.py                   # NotificationBackend ABC
│   │   ├── service.py                # EventBus subscriber → fan-out to backends
│   │   ├── telegram.py               # aiogram 3.x bot + callback handlers
│   │   ├── slack.py                  # slack_bolt AsyncApp socket mode
│   │   └── webhook.py                # generic HTTP POST (Pushover, ntfy, custom)
│   │
│   ├── approvals/
│   │   ├── service.py                # ApprovalGate lifecycle + asyncio.Event
│   │   └── models.py                 # ApprovalGate, ApprovalResult pydantic models
│   │
│   └── http_server.py                # FastAPI: REST + SSE + bot webhooks
│
├── symphony-desktop/
│   ├── src-tauri/
│   │   ├── Cargo.toml                # tauri + plugins
│   │   ├── tauri.conf.json           # sidecar, windows, tray, permissions
│   │   └── src/
│   │       ├── main.rs               # sidecar lifecycle, SSE reader, tray menu
│   │       └── commands.rs           # Tauri IPC ↔ Python API bridge
│   └── src/                          # React/Svelte frontend (shared with web)
│
├── tests/
│   ├── unit/
│   └── integration/
│
├── pyproject.toml
└── WORKFLOW.md
```

---

## 11. Configuration Schema Extension

New top-level WORKFLOW.md keys for this architecture:

```yaml
notifications:
  telegram:
    token: $TELEGRAM_BOT_TOKEN
    chat_id: $TELEGRAM_CHAT_ID
    mode: polling                     # polling | webhook
    webhook_url: $SYMPHONY_PUBLIC_URL # only for mode: webhook
  slack:
    bot_token: $SLACK_BOT_TOKEN
    app_token: $SLACK_APP_TOKEN       # socket mode app-level token
    channel: $SLACK_CHANNEL_ID
  webhook:
    url: $WEBHOOK_URL                 # generic fallback (ntfy, custom)
    headers:
      Authorization: Bearer $WEBHOOK_TOKEN
  approval_timeout_ms: 300000
  events:                             # which events trigger notifications
    - human_review
    - agent_blocked
    - agent_stalled
    - worker_failed
    - approval_requested

server:
  port: 7337                          # 0 = ephemeral; omit to disable HTTP server
  bind: 127.0.0.1                     # loopback only by default
```

The `notifications` key is ignored if no backends are configured. Adding `telegram` or `slack` blocks activates that backend on startup. Both can be active simultaneously.

---

## 13. Linear Integration & Authentication

Linear is Symphony's primary coordination surface — every goal, objective, and task enters the system as a Linear issue. This section covers authentication, token lifecycle, webhook-driven real-time coordination, and the onboarding setup wizard.

---

### 13.1 Auth Options

Symphony supports two authentication modes, which can coexist:

| Mode | How it works | Best for |
|---|---|---|
| **Personal API key** | `LINEAR_API_KEY` env var or `tracker.api_key: $VAR` in WORKFLOW.md | CLI / headless / CI use |
| **OAuth 2.0** | Full user-consent flow; token stored in Keychain or credentials file | Desktop app; team-shared installs |

Both modes produce an opaque bearer token consumed identically by `LinearClient`. The auth layer resolves the token at runtime; the tracker adapter never touches credentials directly.

---

### 13.2 Token Storage Hierarchy

```
TokenStore.resolve() — checked in order, first non-empty wins:

  1. LINEAR_API_KEY env var
  2. tracker.api_key in WORKFLOW.md (literal or $VAR)
  3. macOS Keychain  (desktop app; via `keyring` library / security CLI)
  4. ~/.symphony/credentials.json  (chmod 600; CLI fallback)
```

`~/.symphony/credentials.json` schema:

```json
{
  "linear": {
    "access_token": "lin_api_...",
    "token_type": "Bearer",
    "scope": "read,write",
    "workspace_name": "acme-corp",
    "obtained_at": "2026-05-06T00:00:00Z"
  }
}
```

The file is created with mode `0o600` and never logged or included in error messages.

---

### 13.3 OAuth 2.0 Flow

Linear supports standard OAuth 2.0 Authorization Code flow. Symphony registers as a **Linear Application** (developer.linear.app → Applications).

**Required OAuth scopes:**

| Scope | Why |
|---|---|
| `read` | Fetch issues, projects, teams, states, labels, comments |
| `write` | Update issue state, create comments, attach PR links |
| `app:assignIssues` | Assign dispatched issues to a Symphony service account (optional) |

**Required Linear App settings:**

```
Callback URL:  symphony://oauth/callback   (desktop app deep link)
               http://localhost:0/callback   (CLI ephemeral server)
Webhook URL:   https://<public_url>/linear/webhook
```

---

#### Flow A — Desktop App (Tauri)

```
User opens Symphony.app → no token found
  → Setup Wizard screen: "Connect to Linear"
  → User clicks Connect
  → Python: GET /api/v1/linear/auth/url
      returns: https://linear.app/oauth/authorize
                 ?client_id=<SYMPHONY_CLIENT_ID>
                 &redirect_uri=symphony://oauth/callback
                 &response_type=code
                 &scope=read,write
                 &state=<csrf_nonce>
  → Tauri: open URL in system browser
  → User authorizes in Linear
  → Linear redirects: symphony://oauth/callback?code=<code>&state=<nonce>
  → Tauri: deep-link handler captures URL
      → POST /api/v1/linear/auth/callback  {code, state}
  → Python:
      → verify state matches stored nonce
      → POST https://api.linear.app/oauth/token
           {grant_type, code, redirect_uri, client_id, client_secret}
      → stores access_token in macOS Keychain via `keyring`
      → returns {workspace_name, actor_name}
  → Setup Wizard: step 2 — select project
```

#### Flow B — CLI (`symphony auth linear`)

```
$ symphony auth linear
  → Python: binds ephemeral HTTP server on localhost:0
  → prints: "Open this URL in your browser:"
            "https://linear.app/oauth/authorize?..."
  → User opens URL → authorizes
  → Linear redirects: http://localhost:<port>/callback?code=...
  → Python: exchange code → store in ~/.symphony/credentials.json
  → prints: "Authenticated as Yi Zhang (acme-corp)"
  → exits

$ symphony auth linear --status
  → prints: "Authenticated  workspace=acme-corp  token_age=3d"

$ symphony auth linear --revoke
  → clears token from all stores; prints confirmation
```

---

### 13.4 Setup Wizard (Desktop App First-Run)

Runs on first launch or when `GET /api/v1/linear/auth/status` returns `authenticated: false`.

```
Step 1 — Connect Linear
  → OAuth flow (§13.3 Flow A)
  → on success: workspace name shown, continue button

Step 2 — Select Team + Project
  → GET /api/v1/linear/teams           (lists all teams user has access to)
  → GET /api/v1/linear/projects?teamId  (lists projects in selected team)
  → user picks team → picks project
  → project slug and teamId stored

Step 3 — Configure States
  → GET /api/v1/linear/workflow-states?teamId
  → shows all states; user checks which are "active" and "terminal"
  → defaults pre-selected: Todo + In Progress = active; Done + Cancelled = terminal

Step 4 — Choose AI Agent
  → dropdown: Codex / Claude Code / Gemini / OpenAI-compatible / GPT-Image
  → depending on selection: prompt for relevant API key

Step 5 — Generate WORKFLOW.md
  → Symphony renders a WORKFLOW.md from a template using all collected values
  → user sees a preview with syntax highlighting
  → [Save to repo] button — file picker for target repo root
  → [Download] as fallback

Step 6 — Start
  → Symphony daemon starts with the new WORKFLOW.md
  → Dashboard opens
```

---

### 13.5 Webhook Architecture

Polling alone is sufficient but slow (default 30s lag). Linear webhooks give sub-second issue state change notifications and reduce API load.

#### Webhook registration

Symphony auto-registers a webhook when `server.public_url` is configured:

```python
# tracker/linear_webhook.py
async def ensure_webhook_registered(client: LinearClient, config: Config) -> str:
    # 1. list existing webhooks for the team
    # 2. if Symphony webhook already exists → return its id
    # 3. else: create via GraphQL mutation WebhookCreate
    #    url = f"{config.server.public_url}/linear/webhook"
    #    resourceTypes = ["Issue"]
    #    teamId = config.tracker.team_id
    # 4. store webhook_id in ~/.symphony/state.json
```

#### Incoming webhook event flow

```
Linear: issue state changed
  → POST https://<public_url>/linear/webhook
      headers: X-Linear-Signature: <hmac-sha256>
      body: {action, type, data: {id, identifier, state, ...}}

FastAPI /linear/webhook:
  → verify HMAC-SHA256(body, SYMPHONY_WEBHOOK_SECRET)
  → reject 401 if invalid
  → route by (action, type):

      action="create", type="Issue"
        → EventBus.publish(WEBHOOK_ISSUE_CREATED, issue_id=...)
        → Orchestrator: trigger immediate dispatch check (skip wait for next tick)

      action="update", type="Issue"
        → EventBus.publish(WEBHOOK_ISSUE_UPDATED, issue_id=..., new_state=...)
        → Orchestrator: reconcile_issue(id) — may stop agent if state terminal

      action="remove", type="Issue"
        → EventBus.publish(WEBHOOK_ISSUE_REMOVED, issue_id=...)
        → Orchestrator: release claim, clean workspace

  → return 200 immediately (async processing)
```

#### Polling + webhooks hybrid model

```
Webhooks:   fast reaction to individual issue changes (< 1s)
Polling:    periodic full reconciliation (catchall for missed events)

polling.interval_ms still applies — it's the safety net.
When webhooks are active, interval_ms can be raised to 120000 (2 min)
without losing responsiveness, reducing Linear API quota usage.
```

#### Local development (no public URL)

Three options, configured in WORKFLOW.md:

```yaml
server:
  public_url: $SYMPHONY_PUBLIC_URL   # set by user (ngrok, cloudflare, etc.)
  tunnel: cloudflared                # auto | cloudflared | ngrok | none
```

- `tunnel: cloudflared` — Symphony spawns `cloudflared tunnel --url http://localhost:7337` and captures the printed URL, then registers the webhook using it. Requires `cloudflared` on PATH.
- `tunnel: ngrok` — same pattern with `pyngrok`.
- `tunnel: none` (default) — webhooks disabled; polling only. A warning is shown in the dashboard.

---

### 13.6 New HTTP Endpoints (Linear Auth & Webhook)

```
# Auth
GET  /api/v1/linear/auth/url
     → generates OAuth authorize URL with CSRF nonce
     → {url: "https://linear.app/oauth/authorize?..."}

POST /api/v1/linear/auth/callback
     body: {code, state}
     → exchanges code, stores token, returns {workspace_name, actor_name}

GET  /api/v1/linear/auth/status
     → {authenticated: bool, workspace_name: str, actor_name: str, token_age_s: int}

DELETE /api/v1/linear/auth/revoke
     → clears token from all stores; returns 200

# Setup wizard data endpoints
GET  /api/v1/linear/teams
     → [{id, name, key}]

GET  /api/v1/linear/projects?teamId=<id>
     → [{id, name, slugId, url}]

GET  /api/v1/linear/workflow-states?teamId=<id>
     → [{id, name, type, color}]  (type: triage | backlog | unstarted | started | completed | cancelled)

POST /api/v1/linear/generate-workflow
     body: {team_id, project_slug, active_states, terminal_states, agent_runner, ...}
     → {content: "<rendered WORKFLOW.md>"}

# Webhooks (inbound from Linear)
POST /linear/webhook
     headers: X-Linear-Signature required
     → 200 (async processing) | 401 (bad signature)
```

---

### 13.7 Module Layout Additions

```
symphony/
  ├── tracker/
  │   ├── base.py                   # IssueTrackerAdapter ABC
  │   ├── linear.py                 # GraphQL queries, pagination, normalization
  │   ├── linear_auth.py            # OAuth flow, token exchange, scope validation
  │   └── linear_webhook.py         # webhook registration, signature verification,
  │                                 # event parsing, dispatch to EventBus
  │
  └── auth/
      ├── token_store.py            # TokenStore: env → WORKFLOW.md → keychain → file
      └── credentials.py            # ~/.symphony/credentials.json read/write (0o600)
```

The `TokenStore` is injected into `LinearClient` at construction. `LinearClient` never reads env vars or files directly.

---

### 13.8 Config Schema Additions

New fields in the `tracker` block:

```yaml
tracker:
  kind: linear
  project_slug: "acme-engineering-a1b2c3d4"
  team_id: "abc123"                    # required for webhook registration + setup wizard
  api_key: $LINEAR_API_KEY             # optional if OAuth token already stored
  oauth_client_id: $LINEAR_CLIENT_ID   # required for OAuth flow
  oauth_client_secret: $LINEAR_CLIENT_SECRET
  webhook_secret: $LINEAR_WEBHOOK_SECRET   # HMAC key for incoming webhook verification

server:
  port: 7337
  bind: 127.0.0.1
  public_url: $SYMPHONY_PUBLIC_URL     # enables webhook registration when set
  tunnel: none                          # none | cloudflared | ngrok
```

`oauth_client_id` and `oauth_client_secret` can be omitted if using API key auth only. `webhook_secret` is auto-generated on first webhook registration if not set.

---

## 12. Dependency Summary

```
# Core orchestration
pydantic          2.x    config schema, event models
httpx             1.x    Linear GraphQL, OAuth token exchange, image APIs
jinja2            3.x    prompt template rendering + WORKFLOW.md generation
watchfiles        0.x    WORKFLOW.md hot reload
anyio             4.x    structured concurrency
pyyaml            6.x    WORKFLOW.md front matter

# HTTP server
fastapi           0.x    REST + SSE + bot webhooks + Linear webhook inbound
uvicorn           0.x    ASGI runner

# Auth + credentials
keyring           24.x   macOS Keychain / system credential store
cryptography      42.x   HMAC-SHA256 for Linear webhook signature verification

# Agent SDKs
anthropic         0.x    Claude Code runner
openai            1.x    Codex / GPT-Image runner
google-genai      1.x    Gemini runner

# IM backends (optional, installed if configured)
aiogram           3.x    Telegram bot
slack_bolt        1.x    Slack bot (async socket mode)

# Optional tunnel (local dev webhooks)
pyngrok           7.x    ngrok tunnel (optional; only when tunnel: ngrok)
                         cloudflared is invoked via subprocess (no Python package)

# CLI
typer             0.x    CLI entrypoint

# Desktop app (separate build)
Tauri v2 (Rust)
  tauri-plugin-shell
  tauri-plugin-notification
  tauri-plugin-single-instance
  tauri-plugin-store          # persists workflow path + oauth state
  tauri-plugin-updater
  tauri-plugin-deep-link      # handles symphony://oauth/callback deep links
```
