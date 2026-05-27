"""Approval gate lifecycle — SPEC §15.1.

An :class:`ApprovalGate` is created when an agent requests operator approval.
The orchestrator awaits the gate with a configurable timeout; the HTTP API
(or IM bot) resolves the gate by calling :meth:`ApprovalService.resolve`.

If the timeout elapses without a resolution the gate is treated as rejected,
matching the fail-closed policy described in PRD §8.3.
"""
from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


DEFAULT_APPROVAL_TIMEOUT_MS = 300_000  # 5 minutes


@dataclass
class ApprovalGate:
    """A pending approval request from an agent.

    :attr result: Set by :meth:`ApprovalService.resolve` or on timeout.
    :attr approved: ``True`` if approved, ``False`` if rejected/timed-out,
                    ``None`` until resolved.
    """

    id: str
    session_id: str
    issue_identifier: str
    prompt: str
    created_at: datetime
    timeout_ms: int
    result: asyncio.Event = field(default_factory=asyncio.Event, compare=False)
    approved: bool | None = None
    decision: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "issue_identifier": self.issue_identifier,
            "prompt": self.prompt,
            "created_at": self.created_at.isoformat().replace("+00:00", "Z"),
            "timeout_ms": self.timeout_ms,
            "approved": self.approved,
        }


class ApprovalService:
    """Manages the lifecycle of approval gates.

    Thread-safety note: all public methods must be called from the asyncio
    event loop.  The HTTP handler dispatches ``resolve`` calls via
    ``asyncio.run_coroutine_threadsafe`` when it runs in a separate thread.
    """

    def __init__(self) -> None:
        self._gates: dict[str, ApprovalGate] = {}

    # ------------------------------------------------------------------
    # Gate creation
    # ------------------------------------------------------------------

    def create_gate(
        self,
        session_id: str,
        issue_identifier: str,
        prompt: str,
        *,
        timeout_ms: int = DEFAULT_APPROVAL_TIMEOUT_MS,
        gate_id: str | None = None,
    ) -> ApprovalGate:
        """Create and register a new pending approval gate."""
        gate = ApprovalGate(
            id=gate_id or secrets.token_hex(8),
            session_id=session_id,
            issue_identifier=issue_identifier,
            prompt=prompt,
            created_at=datetime.now(timezone.utc),
            timeout_ms=timeout_ms,
        )
        self._gates[gate.id] = gate
        return gate

    # ------------------------------------------------------------------
    # Await resolution
    # ------------------------------------------------------------------

    async def wait_for_approval(self, gate: ApprovalGate) -> bool:
        """Block until the gate is resolved or its timeout elapses.

        Returns ``True`` if approved, ``False`` if rejected or timed-out.
        The gate is removed from the pending set before returning.
        """
        try:
            await asyncio.wait_for(gate.result.wait(), timeout=gate.timeout_ms / 1000)
        except asyncio.TimeoutError:
            gate.approved = False
        finally:
            self._gates.pop(gate.id, None)
        return bool(gate.approved)

    # ------------------------------------------------------------------
    # Resolution (called by HTTP API or IM bot)
    # ------------------------------------------------------------------

    def resolve(self, approval_id: str, *, approved: bool, decision: str | None = None) -> bool:
        """Resolve a pending gate.

        Returns ``True`` if the gate was found and resolved, ``False`` if the
        gate is unknown or has already been resolved.
        """
        gate = self._gates.get(approval_id)
        if gate is None or gate.result.is_set():
            return False
        gate.approved = approved
        gate.decision = decision
        gate.result.set()
        return True

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def get_gate(self, approval_id: str) -> ApprovalGate | None:
        return self._gates.get(approval_id)

    def pending_gates(self) -> dict[str, ApprovalGate]:
        return dict(self._gates)
