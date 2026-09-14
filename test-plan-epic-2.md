# Test Plan: Epic 2 Phase 1 Closeout

## Purpose

This plan defines the tests and evidence required to mark the Phase 1 closeout
tickets complete:

- `IN-171` Workspace lifecycle manager
- `IN-174` Agent runner base contracts
- `IN-175` Codex app-server runner
- `IN-172` Minimal status API

The current implementation has strong offline proof. The remaining completion
gate is one live dispatch against a disposable active Linear issue to prove the
integrated Linear -> workspace -> Codex -> `linear_graphql` path.

## Completion Rule

Mark tickets complete only after all required gates pass and evidence is posted
to the corresponding Linear tickets and PR #7.

If the live dispatch cannot be run, keep `IN-171`, `IN-175`, and `IN-172` in
review and record the blocker. `IN-174` may remain complete because PR #6 is
already merged and the contracts are covered by automated tests.

## Prerequisites

- Current branch: `feat/in-171-workspace-lifecycle`
- PR: https://github.com/humanbased-ai/jazzband/pull/7
- Python dependencies synced with `uv`
- Linear auth available through `LINEAR_API_KEY`
- Codex CLI authenticated and able to run `codex app-server`
- A disposable Linear issue in the configured project/team
- A `WORKFLOW.md` that targets the disposable issue's active state

## Gate 1: Repository State

Run:

```bash
git status --short --branch
gh pr view 7 --json number,title,state,isDraft,mergeable,reviewDecision,statusCheckRollup,url
```

Pass criteria:

- Branch is not `main`.
- Working tree is clean except intentional test-plan/doc changes.
- PR #7 is open, non-draft, and mergeable.
- Any reported required checks are passing.

Evidence to record:

- Branch name
- PR URL
- PR mergeability and check status

## Gate 2: Automated Python Tests

Run:

```bash
uv run python -m unittest discover -s tests -p 'test_*.py'
uv run jazzband --help
git diff --check
```

Pass criteria:

- Full Python unittest suite passes.
- CLI help renders successfully.
- Whitespace check passes.

Evidence to record:

- Number of tests run and pass result
- CLI command result
- `git diff --check` result

## Gate 3: Config Preflight

Run against the workflow file intended for the smoke test:

```bash
uv run jazzband doctor /path/to/WORKFLOW.md
```

Pass criteria:

- Workflow parses successfully.
- Required Linear token is resolved.
- Config validation reports success.
- Logs root and status API port are printed.

Evidence to record:

- Workflow path
- Logs root
- Status API port

## Gate 4: Live Linear Polling Smoke Test

Before enabling dispatch, run one polling tick with no disposable active issue
or with the disposable issue outside active states:

```bash
uv run jazzband run /path/to/WORKFLOW.md --once --log-level INFO
```

Pass criteria:

- Command exits successfully.
- Linear authentication works.
- Polling completes without dispatching unintended issues.

Evidence to record:

- Tick summary
- Candidate count
- Confirmation that no unintended issue dispatched

## Gate 5: Live Dispatch Smoke Test

Create or choose one disposable Linear issue. Move it into a configured active
state and ensure the prompt asks Codex to perform a low-risk action, such as:

- create a small marker file in the issue workspace,
- post a Linear comment through `linear_graphql`,
- move the issue to the workflow-defined handoff state if that is safe for the
  test issue.

Run:

```bash
uv run jazzband run /path/to/WORKFLOW.md --once --log-level INFO
```

Pass criteria:

- Jazzband fetches the disposable active issue.
- The issue is selected for dispatch.
- A workspace is created under `workspace.root`.
- `after_create`, `before_run`, and `after_run` hooks behave as configured.
- Codex app-server starts in the issue workspace.
- The rendered prompt includes the expected issue fields.
- Codex completes the turn or fails in a controlled, retryable way.
- If the test prompt includes `linear_graphql`, the Linear comment or state
  update appears on the disposable issue.
- Runtime exits with a clear tick summary.

Evidence to record:

- Disposable issue key and URL
- Command output summary
- Workspace path
- Hook evidence, if hooks are configured
- Codex session or event summary
- Linear comment/state proof, if `linear_graphql` was exercised
- Any retry entry or failure message, if applicable

## Gate 6: Status API Handler Proof

Run the unit coverage from Gate 2, then manually inspect the handler behavior
through a local runtime object or, if FastAPI is installed, a local app.

Minimum endpoint coverage:

- `GET /api/v1/health`
- `GET /api/v1/state`
- `GET /api/v1/<issue_identifier>`
- `POST /api/v1/refresh`

Pass criteria:

- Health reports service status and running count.
- State includes running/retry/token/workspace information.
- Per-issue detail maps issue identifier to run/workspace/log metadata.
- Refresh triggers one runtime tick and returns the tick summary.

Evidence to record:

- Endpoint list tested
- Response summaries
- Any missing optional FastAPI runtime dependency

## Ticket-Specific Completion Criteria

### IN-171: Workspace Lifecycle

Required proof:

- Workspace path is deterministic and under `workspace.root`.
- Unsafe issue identifiers are sanitized.
- Path traversal outside the root is rejected by tests.
- Lifecycle hooks run in order where configured.
- Hook timeout/failure behavior is covered by tests.
- Terminal cleanup behavior is covered by tests.
- Live smoke test creates or reuses the expected issue workspace.

Mark complete when:

- Gate 2 passes.
- Gate 5 proves workspace creation during live dispatch.
- Evidence is posted to `IN-171`.

### IN-174: Agent Runner Base Contracts

Required proof:

- PR #6 is merged.
- Base runner/session/event/token/result models are present.
- CLI and API runner contracts are covered by tests.
- Current integration branch still passes the full test suite.

Mark complete when:

- Gate 2 passes.
- PR #6 merge link is recorded on `IN-174`.
- Evidence is posted to `IN-174`.

### IN-175: Codex Runner

Required proof:

- Codex app-server subprocess startup is covered by tests.
- JSON-RPC session and turn handling are covered by tests.
- Event normalization, malformed frames, timeout paths, cleanup, token usage,
  and `linear_graphql` tool routing are covered by tests.
- Live smoke test starts Codex against a disposable active Linear issue.
- If safe, live smoke test exercises `linear_graphql`.

Mark complete when:

- Gate 2 passes.
- Gate 5 proves live Codex dispatch.
- `linear_graphql` proof is captured or a clear reason is recorded for deferring
  only that sub-proof.
- Evidence is posted to `IN-175`.

### IN-172: Minimal Status API

Required proof:

- Health, state, per-issue detail, and refresh routes are covered by tests.
- State payload exposes enough runtime information to debug issue id to
  workspace/run state.
- Scope deferral is documented: SSE and approval endpoints are Phase 3 work.

Mark complete when:

- Gate 2 passes.
- Gate 6 passes.
- Evidence is posted to `IN-172`.

## Linear Closeout Template

Use this format when moving a ticket to `Done`:

```markdown
Closed after Phase 1 closeout validation.

PR:
- https://github.com/humanbased-ai/jazzband/pull/7

Validation:
- `uv run python -m unittest discover -s tests -p 'test_*.py'` -> <result>
- `uv run jazzband --help` -> <result>
- `git diff --check` -> <result>
- Config preflight: <workflow path and result>
- Live dispatch smoke test: <issue key, result, workspace path>
- Status API proof, if applicable: <endpoint summary>

Notes:
- <remaining caveat or "No known completion blockers">
```

## Failure Handling

If any gate fails:

1. Keep the affected ticket in `In Review`.
2. Add a Linear comment with the failing command, observed result, and proposed
   fix.
3. Fix the issue on the feature branch.
4. Re-run the failed gate and any related regression tests.
5. Only move the ticket to `Done` after the gate passes.

## Expected Final State

After successful execution:

- `IN-171` is `Done`.
- `IN-174` is `Done`.
- `IN-175` is `Done`.
- `IN-172` is `Done`.
- PR #7 includes the validation summary.
- The Phase 2 entry gate has live proof for the Linear/Codex dispatch path.
