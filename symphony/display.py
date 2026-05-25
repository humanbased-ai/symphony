"""Live terminal dashboard for Symphony runtime state."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

if TYPE_CHECKING:
    from symphony.agents.base import AgentEvent
    from symphony.orchestrator import OrchestratorState
    from symphony.runtime import RuntimeTickResult

_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_BAR_WIDTH = 18

# PR status → (style, label template)
_PR_STYLES: dict[str, tuple[str, str]] = {
    "open":     ("bold #60a5fa", "#{n} open"),
    "draft":    ("#4b5563",      "#{n} draft"),
    "review":   ("#d97706",      "#{n} review"),
    "approved": ("#16a34a",      "#{n} approved"),
    "merged":   ("#7c3aed",      "#{n} merged"),
    "ci_fail":  ("#dc2626",      "#{n} CI ✗"),
    "closed":   ("#4b5563",      "#{n} closed"),
}


def _now_ms() -> int:
    return int(time.monotonic() * 1000)


def _fmt_dur(ms: int) -> str:
    s = max(0, ms) // 1000
    return f"{s // 60}m {s % 60:02d}s"


def _bar(pct: float) -> str:
    n = int(min(max(pct, 0.0), 100.0) / 100 * _BAR_WIDTH)
    return "█" * n + "░" * (_BAR_WIDTH - n)


@dataclass
class _DoneEntry:
    identifier: str
    title: str
    started_ms: int
    ended_ms: int
    tokens: int
    branch: str | None = None


@dataclass
class _FailedEntry:
    identifier: str
    title: str
    started_ms: int
    ended_ms: int
    error: str
    branch: str | None = None


class LiveDashboard:
    """Rich live terminal dashboard showing per-issue Symphony state."""

    def __init__(self) -> None:
        self._console = Console()
        self._live = Live(
            renderable=Text(""),
            console=self._console,
            refresh_per_second=4,
            transient=False,
        )
        self._running: dict[str, Any] = {}       # issue_id → RunningEntry
        self._all_entries: dict[str, Any] = {}   # accumulates all seen RunningEntries
        self._done: list[_DoneEntry] = []
        self._failed: list[_FailedEntry] = []
        self._retrying: list[Any] = []           # list[RetryEntry]
        self._pr_numbers: dict[str, int] = {}    # branch → pr_number
        self._pr_statuses: dict[int, str] = {}   # pr_number → status key
        self._tick = 0
        self._fetched = 0
        self._poll_interval_s = 30
        self._spinner_idx = 0

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def start(self) -> None:
        self._live.start(refresh=True)

    def stop(self) -> None:
        self._live.stop()

    def __enter__(self) -> "LiveDashboard":
        self.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self.stop()

    # ── Callbacks ──────────────────────────────────────────────────────────

    def on_state_change(self, state: OrchestratorState) -> None:
        for issue_id, entry in state.running.items():
            self._all_entries[issue_id] = entry
        self._running = dict(state.running)
        self._retrying = list(state.retry_attempts.values())
        self._poll_interval_s = state.poll_interval_ms // 1000
        self._refresh()

    async def on_agent_event(self, event: AgentEvent) -> None:
        self._refresh()

    def update_tick(self, result: RuntimeTickResult) -> None:
        self._tick += 1
        self._fetched = result.fetched
        now = _now_ms()
        for identifier in result.completed:
            if any(d.identifier == identifier for d in self._done):
                continue
            entry = next(
                (e for e in self._all_entries.values() if e.identifier == identifier),
                None,
            )
            if entry:
                self._done.append(_DoneEntry(
                    identifier=identifier,
                    title=entry.issue.title,
                    started_ms=entry.started_at_ms,
                    ended_ms=now,
                    tokens=entry.total_tokens,
                    branch=entry.issue.branch_name,
                ))
        for identifier in result.failed:
            if any(f.identifier == identifier for f in self._failed):
                continue
            entry = next(
                (e for e in self._all_entries.values() if e.identifier == identifier),
                None,
            )
            if entry:
                self._failed.append(_FailedEntry(
                    identifier=identifier,
                    title=entry.issue.title,
                    started_ms=entry.started_at_ms,
                    ended_ms=now,
                    error=result.errors.get(identifier, "failed"),
                    branch=entry.issue.branch_name,
                ))
        self._refresh()

    def update_pr(self, branch: str, pr_number: int, status: str) -> None:
        self._pr_numbers[branch] = pr_number
        self._pr_statuses[pr_number] = status
        self._refresh()

    def tick_spinner(self) -> None:
        """Advance spinner one frame; call periodically for animation."""
        self._spinner_idx = (self._spinner_idx + 1) % len(_SPINNER)
        if self._running or self._retrying:
            self._live.update(self._render())

    # ── Rendering ──────────────────────────────────────────────────────────

    def _refresh(self) -> None:
        self._spinner_idx = (self._spinner_idx + 1) % len(_SPINNER)
        self._live.update(self._render())

    def _pr_cell(self, branch: str | None) -> Text:
        if not branch:
            return Text("—", style="#2a2a2a")
        pr_num = self._pr_numbers.get(branch)
        if pr_num is None:
            return Text("—", style="#2a2a2a")
        status = self._pr_statuses.get(pr_num, "open")
        style, tmpl = _PR_STYLES.get(status, _PR_STYLES["open"])
        label = tmpl.replace("{n}", str(pr_num))
        return Text(label, style=style)

    def _section_row(self, tbl: Table, label: str) -> None:
        tbl.add_row(
            Text(f"── {label} ", style="#3d3d3d"),
            Text(""), Text(""), Text(""), Text(""), Text(""),
        )

    def _render(self) -> Panel:
        now = _now_ms()
        n_run  = len(self._running)
        n_done = len(self._done)
        n_fail = len(self._failed)
        n_wait = max(0, self._fetched - n_run)

        # Stats line
        stats = Text()
        stats.append(f"⟳ {n_run} running",  style="#d97706")
        stats.append("   ")
        stats.append(f"✓ {n_done} done",    style="#16a34a")
        stats.append("   ")
        stats.append(f"✗ {n_fail} failed",  style="#dc2626")
        stats.append("   ")
        stats.append(f"⏳ {n_wait} waiting", style="#4b5563")

        # Table
        tbl = Table(
            box=None,
            padding=(0, 1),
            expand=True,
            show_header=True,
            header_style="dim #444444",
        )
        tbl.add_column("Issue",      width=8,  no_wrap=True)
        tbl.add_column("Title",      ratio=3,  no_wrap=True)
        tbl.add_column("Status",     width=11, no_wrap=True)
        tbl.add_column("Time",       width=7,  no_wrap=True)
        tbl.add_column("PR",         width=14, no_wrap=True)
        tbl.add_column("Last Event", ratio=2,  no_wrap=True)

        sp = _SPINNER[self._spinner_idx]

        # Running
        if self._running:
            self._section_row(tbl, "running")
            for entry in self._running.values():
                elapsed = now - entry.started_at_ms
                pct = min(elapsed / (10 * 60 * 1000) * 100, 90)
                tbl.add_row(
                    Text(entry.identifier, style="bold #60a5fa"),
                    Text(entry.issue.title, style="bold #f1f5f9"),
                    Text("⟳ running", style="#d97706"),
                    Text(_fmt_dur(elapsed), style="#6b7280"),
                    self._pr_cell(entry.issue.branch_name),
                    Text(""),
                )
                msg = (entry.last_message or entry.last_event or "")[:55]
                tbl.add_row(
                    Text(""),
                    Text(f"{sp}  {_bar(pct)}  {msg}", style="#4b5563 italic"),
                    Text(""), Text(""), Text(""), Text(""),
                )

        # Completed
        if self._done:
            self._section_row(tbl, "done")
            for e in self._done[-8:]:
                tbl.add_row(
                    Text(e.identifier, style="#2563a8 bold"),
                    Text(e.title, style="#6b7280"),
                    Text("✓ done", style="#16a34a"),
                    Text(_fmt_dur(e.ended_ms - e.started_ms), style="#4b5563"),
                    self._pr_cell(e.branch),
                    Text(f"{e.tokens:,} tok" if e.tokens else "—", style="#374151"),
                )

        # Failed
        if self._failed:
            self._section_row(tbl, "failed")
            for e in self._failed:
                tbl.add_row(
                    Text(e.identifier, style="bold #60a5fa"),
                    Text(e.title, style="bold #f1f5f9"),
                    Text("✗ failed", style="#dc2626"),
                    Text(_fmt_dur(e.ended_ms - e.started_ms), style="#4b5563"),
                    self._pr_cell(e.branch),
                    Text(e.error, style="#b91c1c"),
                )

        # Retrying
        if self._retrying:
            self._section_row(tbl, "retry")
            for r in self._retrying:
                due_in = max(0, (r.due_at_ms - now) // 1000)
                tbl.add_row(
                    Text(r.identifier, style="bold #60a5fa"),
                    Text(""),
                    Text("🔁 retry", style="#c2410c"),
                    Text(""),
                    Text(""),
                    Text(f"in {due_in}s (attempt {r.attempt})", style="#9a3412"),
                )

        # Footer
        footer = Text()
        footer.append(f"Tick #{self._tick}", style="#4b5563")
        footer.append(f"  ·  fetched={self._fetched}", style="#374151")
        footer.append(f"  ·  poll {self._poll_interval_s}s", style="#374151")

        from symphony import __version__  # noqa: PLC0415
        return Panel(
            Group(stats, Text(""), tbl, Rule(style="dim #1a1a1a"), footer),
            title=f"[bold white]Symphony[/bold white] [dim]{__version__}[/dim]",
            title_align="left",
            border_style="#2a2a2a",
        )
