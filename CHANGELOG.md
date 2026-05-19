# Changelog

All notable user-facing changes to the Symphony CLI are recorded here before a
release tag is created.

## Unreleased

- Failure-state transition with no auto-retry (IN-289): when
  `tracker.failure_state` is configured, non-recoverable run failures (turn
  failure, exception, stall timeout, approval-unreachable) now move the
  issue to that state via Linear, clean up the per-run workspace (unless
  `workspace.keep_on_failure` is set), and release the issue without
  scheduling a retry. A structured `run_failed: issue=… reason=…
  failure_state=… last_message=…` event is logged. When
  `tracker.failure_state` is unset, the previous retry-scheduling behavior
  is preserved for backwards compatibility. `symphony doctor` adds a
  `failure state` row indicating which mode the workflow is in. New
  optional config keys: `tracker.failure_state`, `workspace.keep_on_failure`.
- Fail-closed approval gate (IN-288): `symphony doctor` now hard-fails when
  the configured runner can request approval (Codex `approval_policy` other
  than `never`, or Claude Code `permission_mode` other than
  `bypassPermissions`) but `tracker.approval_state` is not set — the daemon
  would otherwise stall on the first approval request. At runtime, an
  `approval_required` turn failure without a resolution path now parks the
  issue (no retry, kept in `claimed`) and logs `approval_unreachable`. New
  optional config key: `tracker.approval_state`.
- Blocker eligibility gate (IN-287): any candidate issue with an unresolved
  blocking relationship in Linear is skipped from dispatch (previously only
  Todo issues were filtered). A `blocker_skip: issue=… blockers=…` info-level
  event is logged when a blocked candidate first appears in the poll snapshot,
  giving operators visibility into upstream-held work without flooding logs.
- Best-effort state-transition claim before dispatch (IN-290): when
  `tracker.in_progress_state` is configured, Symphony moves the issue to that
  state via Linear `updateIssue` before launching the agent and re-fetches to
  verify ownership. Issues already in the in-progress state are skipped on
  poll (claimed by another instance or being worked on by a human). On
  workspace-setup failure after a successful claim, Symphony rolls the issue
  back to `tracker.queued_state` when set; otherwise it logs `claim_abandoned`
  for operator inspection (safer default for multi-instance deployments).
  `symphony doctor` adds a `claim guard` row warning when
  `tracker.in_progress_state` is unset. New optional config keys:
  `tracker.in_progress_state`, `tracker.queued_state`. New required `Issue`
  field: `team_id` (populated by Linear adapter).
- Per-run workspace isolation (IN-286): workspaces are now materialized at
  `<workspace.root>/<workspace_key>/<run_id>` per dispatch. When
  `workspace.repo_url` is configured, Symphony maintains a bare clone at
  `<workspace.root>/.repo.git` and creates a fresh `git worktree` on a unique
  branch for each run; cleanup force-removes the worktree and deletes the
  branch. A startup sweep clears orphan per-run directories from prior crashed
  dispatches. New optional config keys: `workspace.repo_url`,
  `workspace.default_branch` (default `main`), `workspace.branch_prefix`.
- Add `symphony --version` for installed CLI version checks.
- Add `symphony onboard` as the recommended first-run command with environment
  scanning and skip behavior for already-valid setup.
- Add release workflow scaffolding for dry-run, staging, and main artifact
  validation.
