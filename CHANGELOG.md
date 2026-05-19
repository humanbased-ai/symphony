# Changelog

All notable user-facing changes to the Symphony CLI are recorded here before a
release tag is created.

## Unreleased

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
