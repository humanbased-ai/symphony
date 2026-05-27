from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

# Pricing per million tokens (claude-sonnet-4-6 as of 2026)
_PRICE_INPUT = 3.00
_PRICE_OUTPUT = 15.00
_PRICE_CACHE_WRITE = 3.75
_PRICE_CACHE_READ = 0.30


def load_manifest(manifest_path: Path) -> list[dict]:
    entries = []
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            entries.append(json.loads(line))
    return entries


def _build_prompt(entries: list[dict], run_id: str) -> str:
    completed = [e for e in entries if e.get("status") == "completed"]
    failed = [e for e in entries if e.get("status") != "completed"]
    total_secs = sum(
        (e.get("ended_at") or 0) - (e.get("started_at") or 0) for e in entries
    )
    total_input = sum(e.get("input_tokens", 0) for e in entries)
    total_output = sum(e.get("output_tokens", 0) for e in entries)
    total_cache_read = sum(e.get("cache_read_tokens", 0) for e in entries)
    total_cache_write = sum(e.get("cache_creation_tokens", 0) for e in entries)

    actual_cost = (
        total_input * _PRICE_INPUT / 1_000_000
        + total_output * _PRICE_OUTPUT / 1_000_000
        + total_cache_write * _PRICE_CACHE_WRITE / 1_000_000
        + total_cache_read * _PRICE_CACHE_READ / 1_000_000
    )
    counterfactual_cost = (
        (total_input + total_cache_read) * _PRICE_INPUT / 1_000_000
        + total_output * _PRICE_OUTPUT / 1_000_000
    )
    cache_savings = counterfactual_cost - actual_cost

    return f"""You are generating a Symphony run analysis report for run: {run_id}

Raw manifest data (one JSON object per dispatch):
{json.dumps(entries, indent=2)}

Pre-computed statistics:
- Total dispatches: {len(entries)} ({len(completed)} completed, {len(failed)} failed)
- Total wall time: {total_secs / 3600:.1f}h
- Input tokens: {total_input:,}
- Output tokens: {total_output:,}
- Cache read tokens: {total_cache_read:,}
- Cache write tokens: {total_cache_write:,}
- Actual cost: ${actual_cost:.2f}
- Counterfactual cost (no cache): ${counterfactual_cost:.2f}
- Cache savings: ${cache_savings:.2f} ({cache_savings / counterfactual_cost * 100:.0f}% saved)

Write a structured Markdown report with exactly these sections:

## Section 1 — Product Outcome
List every ticket_id with its status (✓ completed / ✗ failed) and PR URL if available.
Summarise in 2-3 sentences what was built overall.
Call out any tickets that failed or have no PR.

## Section 2 — Execution Performance
A table: ticket | duration | input tokens | output tokens | status
Flag any tickets with unusually high token usage or long durations.
Show overall cache efficiency: cache_read / (input + cache_read) as a percentage.

## Section 3 — Cost Analysis
Break down cost by ticket (use the pricing rates in the statistics above).
Show total actual cost, counterfactual cost, and cache savings with percentage.

## Technical Glossary
Brief definitions of: input tokens, output tokens, cache write tokens, cache read tokens, counterfactual cost, cache hit ratio.

Output only the Markdown. No preamble or closing remarks.
"""


def generate_report(
    manifest_path: Path,
    run_id: str,
    *,
    out_path: Path | None = None,
    quiet: bool = False,
) -> str:
    entries = load_manifest(manifest_path)
    if not entries:
        raise ValueError(f"manifest is empty: {manifest_path}")

    prompt = _build_prompt(entries, run_id)

    if not quiet:
        print("[symphony] Generating run analysis...", file=sys.stderr, flush=True)

    result = subprocess.run(
        ["claude", "-p", prompt, "--output-format", "text"],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"claude CLI failed (exit {result.returncode}): {result.stderr.strip()}"
        )

    report = result.stdout.strip()

    if out_path is None:
        out_path = manifest_path.parent / "report.md"

    out_path.write_text(report, encoding="utf-8")
    if not quiet:
        print(f"[symphony] Report written → {out_path}", file=sys.stderr, flush=True)

    return report
