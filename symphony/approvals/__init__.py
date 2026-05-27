"""Approval gate service — asyncio.Event + configurable timeout.

See :mod:`symphony.approvals.service` for the public API.
"""
from symphony.approvals.service import ApprovalGate, ApprovalService

__all__ = ["ApprovalGate", "ApprovalService"]
