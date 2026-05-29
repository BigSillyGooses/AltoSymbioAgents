"""tests/test_sse_events.py — fan-out semantics of the SSE event pump.

Regression coverage for the singleton-queue race that dropped events when
two EventSources overlapped (renderer reload during bootstrap). Each test
runs against a freshly-cleared module state.
"""

import asyncio
import threading

import pytest

import sse_events


@pytest.fixture(autouse=True)
def _reset_sse_module_state():
    """Reset module-level subscriber list and bound loop between tests."""
    sse_events._subscribers.clear()
    sse_events._loop = None
    yield
    sse_events._subscribers.clear()
    sse_events._loop = None


@pytest.mark.asyncio
async def test_single_subscriber_receives_published_event():
    sse_events.attach_loop(asyncio.get_running_loop())
    with sse_events.subscribe() as sub:
        t = threading.Thread(
            target=lambda: sse_events.publish("ping", {"x": 1}),
        )
        t.start()
        t.join()
        events = await sub.drain()
    assert events == [{"event": "ping", "data": {"x": 1}}]


@pytest.mark.asyncio
async def test_two_concurrent_subscribers_each_get_every_event():
    sse_events.attach_loop(asyncio.get_running_loop())
    with sse_events.subscribe() as s1, sse_events.subscribe() as s2:
        for i in range(3):
            sse_events.publish(f"e{i}", {"i": i})
        # Yield once so the call_soon_threadsafe callbacks scheduled by
        # publish() get a chance to run and set both subscribers' signals.
        await asyncio.sleep(0)
        results = await asyncio.gather(s1.drain(), s2.drain())
    assert [e["event"] for e in results[0]] == ["e0", "e1", "e2"]
    assert [e["event"] for e in results[1]] == ["e0", "e1", "e2"]
    assert results[0] == results[1]


def test_unsubscribe_via_context_manager_cleans_up():
    sse_events.attach_loop(asyncio.new_event_loop())

    with sse_events.subscribe():
        assert sse_events.subscriber_count() == 1
    assert sse_events.subscriber_count() == 0

    with pytest.raises(RuntimeError, match="boom"):
        with sse_events.subscribe():
            assert sse_events.subscriber_count() == 1
            raise RuntimeError("boom")
    assert sse_events.subscriber_count() == 0


def test_publish_with_no_subscribers_does_not_raise():
    sse_events.attach_loop(asyncio.new_event_loop())
    sse_events.publish("orphan", {"k": "v"})  # must not raise


@pytest.mark.asyncio
async def test_non_serializable_payload_is_dropped_silently():
    sse_events.attach_loop(asyncio.get_running_loop())
    with sse_events.subscribe() as sub:
        sse_events.publish("bad", {1, 2, 3})  # set is not JSON-serializable
        await asyncio.sleep(0)
        assert sub.drain_nowait() == []


def test_subscribe_before_attach_loop_raises_runtimeerror():
    # Autouse fixture leaves _loop = None; do not attach here.
    with pytest.raises(RuntimeError, match="attach_loop"):
        with sse_events.subscribe():
            pass
