# Symphony

## Project Intent

Symphony is an agent orchestration system that turns Linear issues into isolated
agent implementation runs. Keep implementation aligned with `prd.md`, `SPEC.md`,
and `ARCHITECTURE.md`.

When these documents disagree, stop and surface the mismatch before making broad
implementation changes. Do not silently choose one contract over another.

## Document Ownership

`SPEC.md` is the external reference design published by OpenAI. It is **read-only**.
Never edit, reformat, or annotate `SPEC.md` for any reason — including resolving
cross-doc conflicts, adding constraints, or clarifying behavior. It is the fixed
reference that the implementation is measured against.

Cross-doc conflicts between `SPEC.md` and `prd.md` or `ARCHITECTURE.md` must be
resolved by updating `prd.md` and `ARCHITECTURE.md` only. Document the mismatch
and resolution rationale there.

## Branch Discipline

- Do not make direct changes on `main`.
- Before code changes, ensure the work is on a feature branch.
- If the current branch is `main`, create a feature branch before editing code.
- Keep branch scope tied to one product or implementation change whenever practical.
- Do not rewrite, reset, or discard user changes unless explicitly requested.
- Never commit directly to `main` unless the user explicitly asks for a direct
  `main` commit.

## Product And Tracker Traceability

For behavior, architecture, workflow, configuration, or user-facing changes:

- Read `prd.md` before implementation starts.
- Update `prd.md` with the intended solution and relevant product context.
- After implementation, sync `prd.md` again so it reflects the actual shipped
  approach, validation, and any follow-up work.
- Update any corresponding Linear issue with:
  - the selected solution,
  - the decision-making context,
  - meaningful alternatives considered,
  - validation steps and expected proof,
  - links to resulting PRs or follow-up work.
- If no corresponding Linear issue exists, create one before implementation work
  proceeds beyond investigation.

Docs-only or housekeeping changes do not require a new Linear issue unless they
change product intent, behavior, workflow, or release expectations.

## Implementation Process

- Read the existing code and documents before proposing architecture.
- Read `prd.md` before making implementation changes and confirm the intended
  change is consistent with `prd.md`, `SPEC.md`, and `ARCHITECTURE.md`.
- Prefer small, behavior-preserving changes over broad rewrites.
- Keep orchestration, workspace safety, retry, reconciliation, and cleanup behavior
  explicit and testable.
- Prefer existing project patterns and local helper APIs over new abstractions.
- Do not introduce new dependencies without a clear reason recorded in the PR or
  corresponding Linear issue.
- Update docs in the same change when behavior, configuration, or workflow
  contracts change.
- Create a pull request for completed changes. Do not treat local commits or a
  pushed branch as the finished handoff unless the user explicitly asks to stop
  before PR creation.

## Python Environment

- Use `uv` to manage Python dependencies, lockfiles, virtual environments, and
  Python test runs.
- Prefer `uv add`, `uv remove`, `uv sync`, and `uv run ...` over direct `pip`,
  `python -m venv`, or ad hoc virtualenv commands.
- Keep the `uv.lock` file updated whenever Python dependencies change.

## Validation

- Run targeted checks while iterating.
- Run the relevant full project gate before handoff when feasible.
- For Elixir changes, follow `elixir/AGENTS.md`.
- Record any checks that could not be run, including the reason.

## Pull Requests And Review Loop

For code changes:

- Open a PR rather than landing directly.
- Use the repository PR template when available.
- Include the solution summary, validation evidence, and Linear issue link.
- For UI-impacted changes, set up a local test run and capture key impacted
  screens as `.png` files for PR review.
- Store large or recurring UI review artifacts through the repository's
  storage-saving large-file mechanism, preferably Git LFS for committed binary
  screenshots, so visual evidence does not bloat the normal Git history.
- Put committed PR screenshots under `docs/pr-screenshots/<issue>/` or
  `review-artifacts/<issue>/` so the root `.gitattributes` Git LFS rules apply.
- If Git LFS is not configured for the repository yet, configure it before
  adding committed screenshot artifacts or document the temporary alternative in
  the PR.
- Request review from another agent instance, such as Codex or Claude Code, when
  available.
- Treat review comments as actionable until resolved or explicitly rejected with
  rationale.
- Iterate through fix and review cycles until the outcome is acceptable.
- Do not merge while known blocking review comments, failing required checks, or
  unresolved product-contract mismatches remain.
