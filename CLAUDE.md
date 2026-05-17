# Symphony Contribution Guide For Claude

This repository is governed by the root `AGENTS.md`. Follow that file first;
this file mirrors the highest-impact rules for Claude Code sessions.

## Product Context

Symphony turns Linear issues into isolated agent implementation runs. Keep
behavior aligned with `prd.md`, `SPEC.md`, and `ARCHITECTURE.md`.

Read `prd.md` before implementing. If `prd.md`, `SPEC.md`, and
`ARCHITECTURE.md` disagree, stop and surface the mismatch before making broad
changes.

## Branch And Commit Rules

- Do not work directly on `main`.
- If the current branch is `main`, create a feature branch before editing.
- Never commit directly to `main` unless the user specifically asks for that.
- Do not reset, rewrite, or discard user changes unless explicitly requested.
- Keep each branch scoped to one independently reviewable outcome when possible.

## Implementation Workflow

1. Read the relevant code and docs before proposing or changing architecture.
2. Read `prd.md` and update it with the intended solution for behavior,
   architecture, workflow, configuration, or user-facing changes.
3. Implement the smallest change that satisfies the product intent.
4. Sync `prd.md` after implementation so it matches the actual shipped behavior,
   validation evidence, and follow-up work.
5. Run targeted validation and the relevant full gate when feasible.
6. Commit on the feature branch, push, and create or update the pull request.

Docs-only or housekeeping changes do not require a new Linear issue unless they
change product intent, behavior, workflow, or release expectations.

## Pull Requests

Use the repository PR template. Include:

- Linear issue link or a clear note when no issue is required.
- Solution summary.
- Decision context and alternatives considered.
- Validation run and any checks not run.
- UI screenshots for UI-impacted changes.

Do not merge while blocking review comments, failing required checks, or
product-contract mismatches remain.
