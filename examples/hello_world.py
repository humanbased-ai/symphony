"""
Hello World — Symphony example.

Demonstrates loading a WORKFLOW.md config and listing the fields
Symphony uses when dispatching an agent run. No network calls are made;
this example only reads a local file.

Usage:
    uv run examples/hello_world.py [path/to/WORKFLOW.md]

If no path is supplied, the script prints configuration defaults and exits.
"""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> None:
    print("Hello, World! — Symphony agent orchestration")
    print()

    if len(sys.argv) < 2:
        _print_defaults()
        return

    workflow_path = Path(sys.argv[1])
    if not workflow_path.exists():
        print(f"error: file not found: {workflow_path}", file=sys.stderr)
        sys.exit(1)

    _print_workflow(workflow_path)


def _print_defaults() -> None:
    from symphony.onboarding import (
        DEFAULT_ACTIVE_STATES,
        DEFAULT_PRESET,
        DEFAULT_RUNNER,
        DEFAULT_TERMINAL_STATES,
        DEFAULT_WORKFLOW_PATH,
    )

    print("No WORKFLOW.md supplied — showing built-in defaults.")
    print()
    print(f"  default workflow path : {DEFAULT_WORKFLOW_PATH}")
    print(f"  default runner        : {DEFAULT_RUNNER}")
    print(f"  default preset        : {DEFAULT_PRESET}")
    print(f"  default active states : {', '.join(DEFAULT_ACTIVE_STATES)}")
    print(f"  default terminal state: {', '.join(DEFAULT_TERMINAL_STATES)}")
    print()
    print("Run `symphony init` to create a WORKFLOW.md for your project,")
    print("then re-run this script with the path to inspect its values.")


def _print_workflow(path: Path) -> None:
    from symphony.config import ConfigError, WorkflowConfig

    try:
        cfg = WorkflowConfig.load(path)
    except ConfigError as exc:
        print(f"error loading {path}: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"Loaded: {path}")
    print()
    print(f"  linear project: {cfg.linear_project_id}")
    print(f"  github repo   : {cfg.github_repo}")
    print(f"  runner        : {cfg.runner}")
    print(f"  active states : {', '.join(cfg.active_states)}")
    print(f"  workspace root: {cfg.workspace_root}")


if __name__ == "__main__":
    main()
