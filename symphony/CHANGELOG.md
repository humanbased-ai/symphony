# Changelog

All notable user-facing changes to the Symphony CLI are recorded here before a
release tag is created.

## v0.1.0.14 — 2026-06-01

**[PR #149](https://github.com/humanbased-ai/symphony/pull/149)**: Update CHANGELOG.md and README.md for acceptance gate

## Linear

- Issue: https://linear.app/inductive-network/issue/IN-521/update-changelogmd-and-readmemd-for-acceptance-gate

## Summary

Docs-only sync for the acceptance gate that shipped across #138 (convergence core), #145 (judge prompt), #146 (runtime wiring), and #148 (Phase 2 auto-merge). No code under `symphony/` was touched.

- **CHANGELOG.md** — added three bullets under the existing `## Unreleased` section covering the judge-based PR acceptance flow, the guard-rail force-downgrade (SPEC.md / migrations / `.github/**` / secrets), and the opt-in Phase 2 auto-merge with its four-condition gate.
- **README.md** — added an `Acceptance gate` bullet to the Key Features list that names `acceptance.enabled` as the opt-in, describes the convergence → judge → verdict-comment flow, and explains that Phase 1 only judges/escalates while Phase 2 auto-merges only when the user explicitly sets `acceptance.auto_merge: true` and the four-condition gate passes.

## Decision Context

- Selected solution: extend the existing `## Unreleased` block in `CHANGELOG.md` with three focused bullets (judge flow, guard-rails, opt-in auto-merge) and append a single `Acceptance gate` bullet to the README Key Features list, keeping the same voice and structure as neighbouring entries.
- Alternatives considered: (1) a dedicated "Acceptance gate" subsection in `README.md` with its own heading — rejected as out-of-scope per the issue's "no restructuring" guard-rail; (2) folding all three changelog bullets into one paragraph — rejected because the judge / guard-rail / auto-merge capabilities are independently consumable by release readers.
- Follow-up work: none for IN-521. The acceptance gate's deeper docs continue to live in `prd.md` / `ARCHITECTURE.md` / `WORKFLOW.md` and are out of scope here.

## Validation

- Targeted checks: `git diff` — confirmed only `CHANGELOG.md` and `README.md` changed, additive only.
- Full gates: not applicable; docs-only change with no code under `symphony/` touched, so no compile/test gate runs.
- Not run: code test suites (`mix test`, etc.) — irrelevant for a docs-only diff.

## UI Evidence

For UI-impacted changes, include `.png` captures of the changed screens.
Committed screenshots should live under `docs/pr-screenshots/<issue>/` or
`review-artifacts/<issue>/` so Git LFS tracks them.

- Screenshots:
- Not applicable: docs-only change to `CHANGELOG.md` and `README.md`; no UI surface affected.

## Review

- Reviewer / agent requested: Symphony automation / repo maintainers.
- Blocking comments resolved: none outstanding.

---

## v0.1.0.13 — 2026-06-01

**[PR #148](https://github.com/humanbased-ai/symphony/pull/148)**: feat(acceptance): Phase 2 — gated auto-merge for the safest PRs

## Linear

- Issue: N/A — Phase 2 follow-up to merged PR #138/#145/#146. Closes the `[Acceptance: structured verdict + auto-merge]` task in `prd.md` §7.5.1.

## Summary

- ``GitHubClient.merge_pr(pr_number, *, sha=None, merge_method="squash", commit_title=None)`` — squash-merges via `PUT /repos/{owner}/{repo}/pulls/{n}/merge`. The optional ``sha`` is forwarded to GitHub's ``required_head`` check so a commit pushed after the judge ran blocks the merge instead of slipping past Symphony.
- ``maybe_run_acceptance`` now fires auto-merge only when **all four** preconditions hold:
  1. ``config.acceptance.auto_merge`` is true (default false — flipping it on requires an explicit WORKFLOW.md edit)
  2. ``verdict.overall == "pass"``
  3. ``verdict.confidence >= config.acceptance.confidence_threshold`` (default 0.80)
  4. ``verdict.touched_sensitive_paths`` is empty
- Any failed precondition skips the merge; `_auto_merge_skip_reason` produces a human-readable explanation that the PR comment surfaces (e.g. *"overall confidence 0.65 is below the configured threshold 0.80"*). When ``merge_pr`` itself returns False (GH branch protection / stale head sha / missing required reviews), we fall back to the human-escalation comment with the rejection reason named.
- ``render_verdict_comment`` gains ``merged`` and ``auto_merge_skip_reason`` parameters. Merged comments celebrate the auto-merge and skip the human-escalation tail; non-merged comments surface the skip reason so the gate's behavior is auditable from the PR thread alone.

## Decision Context

- **Default off** rationale: auto-merge is the only Symphony feature that irreversibly touches ``main``. Flipping it on must remain an explicit edit, not a side-effect of enabling acceptance. The onboarding scaffold from PR #147 already writes ``auto_merge: false`` for this reason.
- **Squash merge** rationale: implementer agents typically take multiple turns on a feature branch (initial diff, CI fixes, review responses). Squashing keeps ``main`` history flat and readable; ``merge_method`` is configurable on the client for callers that want a different mode.
- **Head-sha forwarding** rationale: there is a window between the judge running and the merge call landing — closing that race with GitHub's ``required_head`` field is cheaper than re-judging.
- Alternatives considered:
  - Calibration-data collection before unlocking auto-merge — rejected for this PR: collection can stand up alongside auto-merge once enough verdicts exist to measure. Shipping auto-merge first lets users start generating that data on real PRs.
  - Server-side / external decision (a separate "auto-merger" workflow) — rejected: the acceptance verdict already carries the four signals we need; routing through another component buys nothing but latency.
  - Merging on ``uncertain`` with very high confidence — rejected: ``uncertain`` exists specifically because the judge has doubts. Hard-gating on ``pass`` keeps the contract simple.
- Follow-up work: bounce-back (judge ``fail`` → re-implement with ``max_pr_turns``); structured calibration metrics for tuning ``confidence_threshold``.

## Validation

- Targeted checks: ``uv run python -m unittest tests.test_acceptance tests.test_acceptance_runtime`` — 76 tests pass (30 core + 46 runtime, of which 8 new auto-merge / comment-shape tests).
- New ``AutoMergeTests`` suite walks each of the four preconditions plus the GitHub-rejects-merge fallback. ``RenderVerdictCommentTests`` covers the merged / skip-reason comment shapes.
- Not run: live end-to-end against a real PR (auto-merge would actually merge — would need a sacrificial PR). Live validation is deferred until ``auto_merge`` is enabled in the production WORKFLOW.md.

## UI Evidence

- Not applicable: backend-only change. The PR comment text now varies based on whether auto-merge fired or not.

## Review

- Reviewer / agent requested: ``@motivation-labs/crosscheck``.
- Blocking comments resolved: N/A — new PR.

---

## v0.1.0.11 — 2026-06-01

**[PR #145](https://github.com/humanbased-ai/symphony/pull/145)**: feat(acceptance): judge system prompt with scope-boundary tests

## Linear

- Issue: N/A — extends the `feat/acceptance-agent` branch (commit `2af3731` already shipped the convergence core). This PR completes the prompt layer ahead of the runtime-wiring follow-up tracked under `prd.md` §7.5.1 `[Acceptance: runtime wiring + judge]`.

## Summary

- Add `ACCEPTANCE_JUDGE_SYSTEM_PROMPT` in `symphony/acceptance.py` — the system prompt the runner must pass verbatim when it dispatches the one-shot acceptance judge.
- Prompt frames scope as "did it do the right thing" (issue requirements, item by item) and explicitly lists out-of-scope dimensions (style, naming, refactoring, latent bugs, performance, security) so the judge does not drift into code review.
- Prompt mirrors the `AcceptanceVerdict` output shape so the runner can parse the response, and names guard-path triggers that force `overall = "uncertain"` regardless of per-check confidence.
- Lock the prompt with 3 regression tests that fail review if a future edit weakens scope or guard-rail wording.

## Decision Context

- Selected solution: keep the prompt as a single module-level constant co-located with the convergence core, so the gate's scope boundary is reviewable in one place and the runner imports a stable, version-controlled string.
- Alternatives considered:
  - Inline the prompt inside the future runner module — rejected: the prompt is product contract; co-locating with the core keeps scope visible without cross-file hunting.
  - Defer the prompt to the runtime-wiring PR — rejected: the prompt is independently reviewable today, and shipping it separately keeps the next PR purely about I/O (poller integration, judge dispatch, verdict comment).
- Follow-up work: `[Acceptance: runtime wiring + judge]` — gather convergence inputs in the PR poller, dispatch the judge with this prompt + issue text + diff at the post-poll quiet point, and post the human-readable verdict comment. Reuses `max_pr_turns`.

## Validation

- Targeted checks: `uv run python -m unittest tests.test_acceptance` — 30 tests pass (27 from the prior commit + 3 new prompt-lock tests).
- Full gates: not run locally — this change is a pure-string constant plus assertions, no UI/runtime surface to exercise.
- Not run: integration / end-to-end (the acceptance runner does not exist yet; it lands in the follow-up PR).

## UI Evidence

- Not applicable: backend-only change to a pure-string constant.

## Review

- Reviewer / agent requested: `@motivation-labs/crosscheck`.
- Blocking comments resolved: N/A — new PR.

---

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
