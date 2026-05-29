# Changelog

All notable user-facing changes to the Symphony CLI are recorded here before a
release tag is created.

## v0.1.0.10 — 2026-05-29

**[PR #142](https://github.com/codatta/symphony/pull/142)**: chore: default codex-safe preset to 3 concurrent agents

## Linear

- Issue: N/A — housekeeping default change, no behavior/contract change beyond the preset value.

## Summary

- Bump the default `codex-safe` onboarding preset from `max_concurrent_agents=1` to `3` (`symphony/onboarding.py`).
- Freshly generated `WORKFLOW.md` files now dispatch up to three agents in parallel out of the box.
- All other `codex-safe` defaults are unchanged: 30s polling, `max_turns=20`, `approval_policy=never`, `workspace-write` sandbox.

## Decision Context

- Selected solution: raise the concurrency value on the default preset itself, keeping it as the default preset.
- Alternatives considered: switch `DEFAULT_PRESET` to `codex-autonomous` (already 3) — rejected because it would also change polling interval (15s) and `max_turns` (30), which is broader than requested.
- Follow-up work: none.

## Validation

- Targeted checks: `python -m pytest tests/test_onboarding.py -q` — 5 passed.
- Full gates: not run.
- Not run: full suite / lint.

## UI Evidence

- Not applicable: backend/config default change only.

## Review

- Reviewer / agent requested: TBD
- Blocking comments resolved: n/a

---

## v0.1.0.8 — 2026-05-27

**[PR #136](https://github.com/codatta/symphony/pull/136)**: feat(in-390): run manifest collection and report generation

## Linear

- Issue: IN-390

## Summary

- `symphony/manifest.py` — `ManifestWriter` appends one JSON line per dispatch to `~/.symphony/runs/<project-slug>/<run-id>/manifest.jsonl`; run ID persists across daemon restarts so data isn't fragmented by restarts; `--new-run` starts a fresh run
- `symphony/report.py` — reads manifest, calls `claude` CLI, writes `report.md`; token costs and counterfactual (no-cache) cost are computed from manifest fields
- `runtime.py` — accepts optional `manifest_writer`; records each dispatch result (ticket ID, session ID, timing, token usage, PR URL) after the agent returns
- `cli.py` — wires `ManifestWriter` into `create_runtime` using project slug from config; automatically generates report on daemon stop without prompting; adds `symphony report` command; adds `--new-run` flag

## Decision Context

- Token data sourced from `result.usage` (already flowing through `_run_dispatched_issue`) instead of scanning `~/.claude/` files — more reliable
- Storage at `~/.symphony/runs/<project-slug>/<run-id>/` — project-isolated
- Report generated automatically on daemon stop — no prompt, no opt-in flag; cost is ~$0.10 per report, negligible vs run cost
- Run ID persists in `current_run_id` file — survives restarts without fragmenting the manifest
- `claude` CLI used for report generation (already a required dependency), no new SDK dependency

## Validation

- `python -c "import symphony.manifest; import symphony.report; import symphony.cli"` passes
- `symphony report --help` shows correct options
- `symphony --help` shows `symphony report` in epilog

## UI Evidence

- Not applicable

## Review

- Reviewer / agent requested: —
- Blocking comments resolved: N/A

---

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
