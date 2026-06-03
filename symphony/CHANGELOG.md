# Changelog

All notable user-facing changes to the Symphony CLI are recorded here before a
release tag is created.

## v0.1.0.21 — 2026-06-03

**[PR #157](https://github.com/humanbased-ai/symphony/pull/157)**: feat(verifyflow): ci_green trigger for repos without Crosscheck (IN-570)

## Linear

- Issue: https://linear.app/inductive-network/issue/IN-570/verifyflow优化

## What

Stacked on #156 (IN-569). Decouples the verifyflow step's entry condition from Crosscheck:

- New `verifyflow.trigger` config: `crosscheck` (default — behavior unchanged) | `ci_green`.
- **`ci_green`**: enter once the PR head's latest check runs carry no failures — the exact green convention the acceptance subsystem already uses (`acceptance_runtime.py`: `ci_green = len(failed) == 0`; pending/no checks count as green, the once-per-head-SHA dedup bounds the cost). This unblocks repos that don't run Crosscheck, where the default trigger would never fire and the step would silently do nothing.
- A crosscheck verdict, when one happens to exist, is still passed via `--crosscheck-verdict` for traceability — `ci_green` records it but never gates on it.
- Unknown trigger values fail at config parse (`unsupported_verifyflow_trigger`).

PRD synced in §7.5.2.

## Tests

4 new in `tests/test_verifyflow_runtime.py` (12 pass): ci_green runs with no crosscheck comment (and omits the verdict arg), skips on failed checks, records a present verdict without gating on it, invalid trigger raises ConfigError. Full suite: 454 passed; the 13 pre-existing `classify_feedback` failures on main remain untouched.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

---

## v0.1.0.20 — 2026-06-03

**[PR #155](https://github.com/humanbased-ai/symphony/pull/155)**: fix(onboarding): default acceptance gate to off in init/onboard

## Linear

- Issue: None — onboarding default-behavior change, no product-intent change to the gate itself; prd.md updated in this PR per repo workflow.

## Summary

- `symphony init` / `symphony onboard` now ship the acceptance gate **disabled by default**: `InitConfig.acceptance_enabled` defaults to `False`, automated mode no longer auto-enables the gate, and the interactive prompt defaults to `[y/N]`.
- Explicit `--acceptance` / `--no-acceptance` continue to win over every default; opting in still writes the full production-safe block (`auto_merge: false`, `bounce_back_on_fail: false`).
- This aligns the onboarding scaffold with `AcceptanceConfig`, whose master switch has always defaulted to off.

## Decision Context

- Selected solution: flip the three onboarding defaults (dataclass default, automated-mode branch, interactive prompt) and keep everything else — opt-in path, GitHub gating, block omission semantics — unchanged.
- Alternatives considered: keeping automated mode on while flipping only the interactive prompt (rejected — automated runs are exactly where a silent extra judge agent is least visible); writing `enabled: false` instead of omitting the block (rejected — existing convention is to omit, avoiding config noise).
- Follow-up work: none.

## Validation

- Targeted checks: `uv run python -m pytest tests/test_onboarding.py tests/test_cli.py -q` — 69 passed.
- Full gates: `uv run python -m pytest -q` — 442 passed, 13 failed; the 13 failures (`FeedbackSignalTests` in `tests/test_linear_tracker.py`, `FeedbackGateTests` in `tests/test_runtime.py`) reproduce identically on a clean `origin/main` checkout and are unrelated to this change.
- Not run: interactive `symphony init` end-to-end (prompt-path behavior covered by unit tests).

## UI Evidence

- Screenshots:
- Not applicable: CLI-only change.

## Review

- Reviewer / agent requested: maintainers
- Blocking comments resolved: n/a

🤖 Generated with [Claude Code](https://claude.com/claude-code)

---

## v0.1.0.19 — 2026-06-01

**[PR #153](https://github.com/humanbased-ai/symphony/pull/153)**: fix(acceptance): gate bounce-back behind opt-in flag (default off)

## Linear

- Issue: N/A — operator feedback after PR #151: bounce-back on judge ``fail`` was unconditional, but early rollouts should keep "judge → comment → human decides" semantics until verdict quality is trusted.

## Summary

- New config field ``acceptance.bounce_back_on_fail`` (default ``false``).
- When false (default): a ``fail`` verdict still posts the full evaluation report on the PR; the loop then waits for a human to decide what to do.
- When true: the historical PR #151 behavior — verdict's unmet checks are forwarded through ``_handle_pr_feedback`` and the implementer agent gets another turn, capped by ``max_pr_turns``.
- ``symphony init`` writes ``bounce_back_on_fail: false`` explicitly so users see the knob and know it's the off-by-default opt-in.
- Verdict ``uncertain`` and ``pass`` are unaffected — they never triggered bounce-back, so no flag check is needed.

## Decision Context

- **Default off rationale**: a verdict from the acceptance judge is still a model output — confidence calibration on real workloads is not yet built. Spending agent budget on a retry before an operator has seen even one fail verdict on a real PR is too aggressive. The verdict comment IS the evaluation report (summary + per-check evidence) — that's the human's data to act on.
- **Why not remove bounce-back entirely**: the code path is small, tested, and useful once trust is established. Removing it would mean re-implementing later. Gating it behind a flag keeps the option live without imposing it.
- **Why write the false explicitly in ``symphony init``**: same reason ``auto_merge: false`` is written explicitly — the file should disclose every irreversible automation knob, not hide them behind library defaults. Users discover what's tunable by reading the generated file.
- Alternatives considered:
  - Couple ``bounce_back_on_fail`` to ``auto_merge`` (only auto-bounce if auto-merge is on) — rejected: they answer different questions ("can the system retry a fail" vs "can the system merge a pass"); coupling them would confuse the mental model.
  - Make the default true and let operators opt out — rejected: the safe direction for an unproven feature is "off → user explicitly opts in", not the reverse.
- Follow-up: a future PR can add a separate threshold (only bounce when verdict confidence is above N) once we have data on how often the judge's fails are themselves wrong.

## Validation

- 135 tests pass (``test_acceptance test_acceptance_runtime test_github_client test_onboarding test_pr_polling``).
- New ``test_default_off_does_not_bounce_on_fail`` exercises the production-safe default.
- Existing ``TestAcceptanceBounceBack`` fixture takes a kwarg, defaulting to ``True`` so the bounce-loop tests keep covering the opt-in branch.
- ``OnboardingTests`` asserts ``bounce_back_on_fail`` is present and false in the scaffold.

## UI Evidence

- Not applicable: config field + runtime gating. No graphical surface.

## Review

- Reviewer / agent requested: ``@motivation-labs/crosscheck``.

---

## v0.1.0.18 — 2026-06-01

**[PR #152](https://github.com/humanbased-ai/symphony/pull/152)**: fix(acceptance): hold auto mode for crosscheck before falling through

## Linear

- Issue: N/A — surfaced by review of the acceptance gate intro doc: ``auto`` mode could race a slow crosscheck and judge a PR before code review posted.

## Summary

- ``auto`` mode holds open for ``acceptance.crosscheck_wait_seconds`` (default **1200 = 20 min**) before letting the silent branch fire. Inside that window, ``evaluate_convergence`` returns *"holding for crosscheck — Ns left in the grace window"* so the wait is observable from the log.
- After the window expires (or when ``pr.created_at`` is unparseable), the silent branch becomes eligible again — crosscheck is treated as not connected, ``auto`` falls back to legacy behavior.
- The wait does NOT apply to ``review_source: crosscheck`` (missing verdict is already a not-converged reason) or to ``review_source: none`` (explicit opt-out).
- ``symphony init`` writes ``crosscheck_wait_seconds`` into the generated WORKFLOW.md so the knob is discoverable.

## Decision Context

- **Grace window not forever-wait**: a project may disconnect crosscheck mid-flight; ``auto`` is meant to be "use crosscheck if it shows up, else carry on". Forever-wait freezes acceptance on any project that drops crosscheck without flipping ``review_source: none``.
- **Default 20 min**: crosscheck typically takes 1-5 min, worst case (long diffs, retries, queue depth) extends it. 20 min leaves headroom without making operators wait half an hour to discover crosscheck is offline. Tunable per project.
- **``pr_age_seconds`` from ``pr.created_at``**: cheap signal already in the PR response. ``None`` (missing/malformed field) disables the wait so a parsing hiccup never freezes acceptance forever.
- **``crosscheck_wait_seconds=0`` is the escape hatch**: legacy callers default to 0, keep working unchanged.

## Validation

- 134 tests pass (``test_acceptance test_acceptance_runtime test_github_client test_onboarding test_pr_polling``).
- New ``ConvergenceCrosscheckWaitTests`` (7 cases) covers young/old/unknown PR age, wait=0 legacy, crosscheck verdict short-circuit, ``crosscheck`` / ``none`` modes ignored.
- New ``GatherConvergenceInputsTests`` cases for ``pr_age_seconds`` computation.
- ``OnboardingTests`` asserts scaffold writes the new field with a positive value.

## UI Evidence

- Not applicable: convergence logic + config field. No graphical surface.

## Review

- Reviewer / agent requested: ``@motivation-labs/crosscheck``.

---

## v0.1.0.16 — 2026-06-01

**[PR #147](https://github.com/humanbased-ai/symphony/pull/147)**: feat(onboarding): scaffold disabled-by-default acceptance block

## Linear

- Issue: N/A — onboarding ergonomics follow-up to the acceptance gate shipped in PR #138/#145/#146. Surface the feature inside `symphony init` rather than burying it in docs.

## Summary

- `symphony init` / `symphony onboard` now ask **"Enable acceptance gate? [Y/n]"** after the GitHub repo step, when github is configured and the run is interactive. Pressing Enter keeps it on; "n" turns it off.
- New CLI flags `--acceptance` / `--no-acceptance` (BooleanOptionalAction) override the prompt for non-interactive and explicit-intent callers.
- Automated mode (`--yes` / `--mode automated`) defaults to on; force off with `--no-acceptance`.
- `InitConfig` gains `acceptance_enabled: bool = True`. When True and github is configured, the generated `WORKFLOW.md` carries `acceptance: { enabled: true, review_source: auto, auto_merge: false, quiet_period_seconds: 300, guard_paths: [...] }`. When False the block is omitted entirely — explicit `enabled: false` would still leave config noise behind for a feature the user said no to.
- Defaults sourced from `symphony.config.DEFAULT_ACCEPTANCE_*` so scaffold and runtime parser stay in sync.

## Decision Context

- Selected solution: interactive opt-in flow with default-yes + complementary CLI flags. Acceptance becomes part of the answered questions during onboarding, not a post-step the user must remember.
- **Default-on rationale**: Phase 1 acceptance is human-escalation only (no auto-merge), so opting new projects in costs only the judge dispatch on PR convergence — there is no destructive default. Users who do not want that cost have one obvious moment to say no (the prompt) and one explicit way to encode it (`--no-acceptance`).
- Alternatives considered:
  - Disabled-by-default scaffold (the earlier commit on this branch, kept in history) — rejected after feedback: discoverable but still requires a post-onboarding edit, so the discoverability win is small.
  - No prompt, default-off, no scaffold — rejected: leaves the feature buried in docs.
  - No prompt, default-on, no flag — rejected: gives users no way to opt out without editing the generated file.
- Follow-up work: Phase 2 (auto-merge) will need its own opt-in moment when it actually does something — that prompt will be gated behind the acceptance one.

## Validation

- Targeted checks: `uv run python -m unittest tests.test_onboarding tests.test_acceptance tests.test_acceptance_runtime` — 76 tests pass (combined acceptance suite).
- `uv run symphony init --help` shows `--acceptance, --no-acceptance` with the right default-behavior text.
- `uv run symphony --check` on the existing `WORKFLOW.md` still parses cleanly (no schema break for projects without an `acceptance:` block).
- Not run: live `symphony onboard` interactive flow end-to-end — covered indirectly by `test_generate_workflow_enables_acceptance_by_default_with_github` and `test_generate_workflow_omits_acceptance_block_when_opted_out`.

## UI Evidence

- CLI flag visible in `symphony init --help` output. No graphical UI surface.

## Review

- Reviewer / agent requested: `@motivation-labs/crosscheck`.
- Blocking comments resolved: N/A — open PR.

---

## v0.1.0.15 — 2026-06-01

**[PR #150](https://github.com/humanbased-ai/symphony/pull/150)**: fix(github): dedup check runs by name in get_pr_failed_check_runs

## Linear

- Issue: N/A — bug surfaced during the acceptance-gate end-to-end on PR #149 (the verification run for the Phase 2 auto-merge PR #148).

## Summary

- GitHub's ``?filter=latest`` on ``/repos/{owner}/{repo}/commits/{sha}/check-runs`` returns the latest run **per check_suite**, not per check name. When the same check (e.g. ``validate-pr-description``) is re-run on the same commit via a different mechanism — a PR description edit spawning a new check_suite, a manual re-run, a workflow_dispatch — the response carries multiple entries for that name: the old failure AND the new success.
- ``get_pr_failed_check_runs`` filtered by ``conclusion == "failure"`` without deduplicating across suites, so a stale failure looked "still failing" forever and the silent acceptance branch never saw ``ci_green=True``.
- Fix: keep only the most recent run per check name (by ``started_at``) before filtering. Successful re-runs supersede earlier failures; later failures supersede earlier successes; distinct names all come through.

## Decision Context

- **Live repro on PR #149**: Symphony's implementer agent rewrote the PR description to satisfy ``validate-pr-description`` (the check came back green in a new suite). ``get_pr_failed_check_runs`` still reported the old failure id, ``ci_green`` stayed False, the silent branch never converged, and acceptance never fired. The bug had been masked until now because Phase 1 wiring only landed in PR #146 and the silent branch was not exercised end-to-end before.
- Dedup-by-name keyed on ``started_at`` was chosen over (a) trusting only the highest run-id, because run-id ordering by recency is not contractually documented, and (b) querying ``check_suites`` separately, which would double the API calls.
- Follow-up work: none — this is a contained correctness fix.

## Validation

- Targeted checks: ``uv run python -m unittest tests.test_github_client tests.test_acceptance tests.test_acceptance_runtime`` — 82 tests pass.
- ``tests/test_github_client.py`` is new and covers: the PR #149 scenario (success rerun supersedes failure → empty list), the symmetric case (later failure supersedes earlier success → returned), unique failures, multi-name failures, malformed PR responses, and API errors.
- Live verification: the acceptance gate end-to-end on PR #149 was blocked by this exact bug; once this branch is checked out the silent branch converges and acceptance fires.
- Not run: full ``make all`` (pre-existing unrelated failures in ``test_runtime`` and ``test_linear_tracker`` exist on ``main``).

## UI Evidence

- Not applicable: backend correctness fix to a single method.

## Review

- Reviewer / agent requested: ``@motivation-labs/crosscheck``.
- Blocking comments resolved: N/A — new PR.

---

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
