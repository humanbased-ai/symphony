# Changelog

All notable user-facing changes to the Symphony CLI are recorded here before a
release tag is created.

## v0.1.0.7 — 2026-05-27

**[PR #135](https://github.com/codatta/symphony/pull/135)**: feat(runtime): auto-detect PR merge conflicts and dispatch resolution agent

## Linear

- Issue: N/A

## Summary

- On each poll cycle, fetch full PR data via `get_pr()` and check `mergeable_state`
- When `mergeable == False` and `mergeable_state == "dirty"`, dispatch an agent with instructions to `git merge origin/main`, resolve conflicts, commit, and push
- `_pr_conflict_dispatched: set[str]` prevents re-dispatching on every poll cycle until the conflict is cleared
- Guard resets automatically when the PR becomes clean or is closed/merged

## Decision Context

- Selected solution: reuse existing `_handle_pr_feedback()` + `_run_pr_feedback()` machinery — conflict resolution is structurally identical to reviewer feedback, just with a different prompt
- Alternatives considered: GitHub Merge Queue — only handles the merge side, not the agent context divergence problem; rejected in favor of Symphony-native resolution
- `max_pr_turns` budget is shared with regular feedback turns to bound total agent iterations per PR

## Validation

- Targeted checks: `python -c "import symphony.runtime"` passes; logic reviewed manually
- Full gates: will verify on next real conflict scenario
- Not run: N/A

## UI Evidence

- Not applicable

## Review

- Reviewer / agent requested: —
- Blocking comments resolved: N/A

---

## Unreleased

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
