"""Tests for the ApprovalGate / ApprovalService."""
from __future__ import annotations

import asyncio
import unittest

from symphony.approvals.service import ApprovalGate, ApprovalService


class ApprovalServiceTests(unittest.IsolatedAsyncioTestCase):
    def _service(self) -> ApprovalService:
        return ApprovalService()

    # ------------------------------------------------------------------
    # Gate creation
    # ------------------------------------------------------------------

    def test_create_gate_registers_pending(self) -> None:
        svc = self._service()
        gate = svc.create_gate("sess-1", "IN-1", "run tests?")
        self.assertIn(gate.id, svc.pending_gates())
        self.assertIsNone(gate.approved)
        self.assertFalse(gate.result.is_set())

    def test_create_gate_with_explicit_id(self) -> None:
        svc = self._service()
        gate = svc.create_gate("sess-1", "IN-1", "run tests?", gate_id="fixed-id")
        self.assertEqual(gate.id, "fixed-id")

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------

    def test_resolve_approve_sets_approved_true(self) -> None:
        svc = self._service()
        gate = svc.create_gate("sess-1", "IN-1", "run tests?")
        result = svc.resolve(gate.id, approved=True)
        self.assertTrue(result)
        self.assertTrue(gate.approved)
        self.assertTrue(gate.result.is_set())

    def test_resolve_reject_sets_approved_false(self) -> None:
        svc = self._service()
        gate = svc.create_gate("sess-1", "IN-1", "run tests?")
        result = svc.resolve(gate.id, approved=False)
        self.assertTrue(result)
        self.assertFalse(gate.approved)

    def test_resolve_unknown_id_returns_false(self) -> None:
        svc = self._service()
        result = svc.resolve("nonexistent-id", approved=True)
        self.assertFalse(result)

    def test_resolve_already_resolved_returns_false(self) -> None:
        svc = self._service()
        gate = svc.create_gate("sess-1", "IN-1", "run tests?")
        svc.resolve(gate.id, approved=True)
        result = svc.resolve(gate.id, approved=False)
        self.assertFalse(result)
        # First resolution wins — still approved.
        self.assertTrue(gate.approved)

    # ------------------------------------------------------------------
    # Wait for approval
    # ------------------------------------------------------------------

    async def test_wait_for_approval_approved(self) -> None:
        svc = self._service()
        gate = svc.create_gate("sess-1", "IN-1", "run tests?")

        async def resolve_soon() -> None:
            await asyncio.sleep(0.01)
            svc.resolve(gate.id, approved=True)

        asyncio.create_task(resolve_soon())
        approved = await svc.wait_for_approval(gate)
        self.assertTrue(approved)
        # Gate is removed after wait.
        self.assertNotIn(gate.id, svc.pending_gates())

    async def test_wait_for_approval_rejected(self) -> None:
        svc = self._service()
        gate = svc.create_gate("sess-1", "IN-1", "run tests?")

        async def reject_soon() -> None:
            await asyncio.sleep(0.01)
            svc.resolve(gate.id, approved=False)

        asyncio.create_task(reject_soon())
        approved = await svc.wait_for_approval(gate)
        self.assertFalse(approved)

    async def test_wait_for_approval_timeout(self) -> None:
        svc = self._service()
        gate = svc.create_gate("sess-1", "IN-1", "run tests?", timeout_ms=50)
        # No resolver — should time out and return False.
        approved = await svc.wait_for_approval(gate)
        self.assertFalse(approved)
        self.assertNotIn(gate.id, svc.pending_gates())

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def test_get_gate_returns_gate(self) -> None:
        svc = self._service()
        gate = svc.create_gate("sess-1", "IN-1", "run tests?")
        self.assertIs(svc.get_gate(gate.id), gate)

    def test_get_gate_unknown_returns_none(self) -> None:
        svc = self._service()
        self.assertIsNone(svc.get_gate("nonexistent"))

    def test_pending_gates_returns_snapshot(self) -> None:
        svc = self._service()
        g1 = svc.create_gate("s1", "IN-1", "p1")
        g2 = svc.create_gate("s2", "IN-2", "p2")
        pending = svc.pending_gates()
        self.assertIn(g1.id, pending)
        self.assertIn(g2.id, pending)

    # ------------------------------------------------------------------
    # Gate serialization
    # ------------------------------------------------------------------

    def test_gate_to_dict(self) -> None:
        svc = self._service()
        gate = svc.create_gate("sess-1", "IN-42", "approve this?", timeout_ms=60_000)
        d = gate.to_dict()
        self.assertEqual(d["session_id"], "sess-1")
        self.assertEqual(d["issue_identifier"], "IN-42")
        self.assertEqual(d["prompt"], "approve this?")
        self.assertEqual(d["timeout_ms"], 60_000)
        self.assertIsNone(d["approved"])
