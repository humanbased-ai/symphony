# Changelog

All notable user-facing changes to the Symphony CLI are recorded here before a
release tag is created.

## Unreleased

- Acceptance gate: after a PR converges (CI green and no new reviewer
  feedback for the configured quiet period) Symphony dispatches a one-shot
  judge that re-checks the diff against the original Linear issue and posts
  a verdict comment (`pass` / `fail` / `uncertain` with confidence and
  rationale) on the PR.
- Acceptance gate guard-rails: a `pass` verdict that touches `SPEC.md`,
  database migrations, anything under `.github/**`, or files matching the
  configured secrets patterns is force-downgraded to `uncertain` so a human
  still reviews before merge.
- Optional Phase 2 auto-merge (`acceptance.auto_merge: true`) gated on four
  conditions: a `pass` verdict, confidence at or above
  `acceptance.confidence_threshold` (default `0.80`), no guard-rail paths
  touched, and GitHub branch protection still gets the final say. Disabled
  by default; Phase 1 only judges and escalates to a human.
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
