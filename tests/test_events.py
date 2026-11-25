from __future__ import annotations

import asyncio

from app.events import EventBus


def test_event_bus_publish_and_subscribe() -> None:
    async def scenario():
        bus = EventBus()
        bus.bind_loop(asyncio.get_running_loop())
        queue = bus.subscribe()
        bus.publish({"type": "account_imported", "account_id": "a1"})
        return await asyncio.wait_for(queue.get(), timeout=1)

    event = asyncio.run(scenario())
    assert event["type"] == "account_imported"


def test_event_bus_unsubscribe() -> None:
    async def scenario():
        bus = EventBus()
        bus.bind_loop(asyncio.get_running_loop())
        queue = bus.subscribe()
        bus.unsubscribe(queue)
        bus.publish({"type": "account_changed"})
        await asyncio.sleep(0.05)
        return queue.empty()

    assert asyncio.run(scenario())
