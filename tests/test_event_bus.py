"""Tests for the asyncio-native EventBus pub/sub."""
from __future__ import annotations

import asyncio
import unittest

from symphony.event_bus import EventBus, event_to_sse_data


class EventBusTests(unittest.IsolatedAsyncioTestCase):
    async def test_publish_delivers_to_subscriber(self) -> None:
        bus = EventBus()
        async with bus.subscribe() as q:
            await bus.publish({"type": "tick_completed"})
            event = q.get_nowait()
        self.assertEqual(event["type"], "tick_completed")

    async def test_publish_delivers_to_multiple_subscribers(self) -> None:
        bus = EventBus()
        async with bus.subscribe() as q1, bus.subscribe() as q2:
            await bus.publish({"type": "agent_started", "issue_identifier": "IN-1"})
            e1 = q1.get_nowait()
            e2 = q2.get_nowait()
        self.assertEqual(e1["type"], "agent_started")
        self.assertEqual(e2["type"], "agent_started")

    async def test_subscriber_removed_after_context_exit(self) -> None:
        bus = EventBus()
        async with bus.subscribe():
            self.assertEqual(bus.subscriber_count(), 1)
        self.assertEqual(bus.subscriber_count(), 0)

    async def test_full_queue_drops_event_without_raising(self) -> None:
        bus = EventBus(maxsize=1)
        async with bus.subscribe() as q:
            await bus.publish({"type": "first"})
            # Queue is now full — second publish should not raise.
            await bus.publish({"type": "second"})
            self.assertEqual(q.qsize(), 1)
            event = q.get_nowait()
        self.assertEqual(event["type"], "first")

    async def test_no_subscribers_publish_is_noop(self) -> None:
        bus = EventBus()
        # Should not raise even when there are no subscribers.
        await bus.publish({"type": "tick_completed"})

    async def test_multiple_events_ordered(self) -> None:
        bus = EventBus()
        async with bus.subscribe() as q:
            for i in range(3):
                await bus.publish({"type": "event", "seq": i})
            events = [q.get_nowait() for _ in range(3)]
        self.assertEqual([e["seq"] for e in events], [0, 1, 2])


class SSESerializationTests(unittest.TestCase):
    def test_event_to_sse_data_format(self) -> None:
        event = {"type": "tick_completed"}
        result = event_to_sse_data(event)
        self.assertTrue(result.startswith("data: "))
        self.assertTrue(result.endswith("\n\n"))

    def test_event_to_sse_data_is_valid_json(self) -> None:
        import json
        event = {"type": "agent_started", "issue_identifier": "IN-42"}
        result = event_to_sse_data(event)
        payload = result.removeprefix("data: ").strip()
        parsed = json.loads(payload)
        self.assertEqual(parsed["type"], "agent_started")
        self.assertEqual(parsed["issue_identifier"], "IN-42")
