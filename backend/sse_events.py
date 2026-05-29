"""
sse_events.py — Process-wide event pump that bridges legacy `_emit(event, payload)`
calls (originally PyWebView's `window.__emit`) to Server-Sent Events streams.

The sidecar runs FastAPI/uvicorn; routes that fan out work to threads (chat
streaming, hardware probing, health checks, etc.) used to call
`window.evaluate_js("window.__emit(...)")` to push results to the renderer.
That window doesn't exist anymore — instead, those events go onto per-subscriber
queues, and the GET /api/events SSE endpoint drains the subscriber it owns to
whichever EventSource the renderer has open.

Design notes:
- Per-subscriber fanout. Each open EventSource registers a Subscriber via
  the `subscribe()` context manager; `publish()` pushes the same event into
  every active subscriber's deque. This eliminates the race that occurred
  with the previous single-queue model when two EventSources overlapped
  (renderer reload during bootstrap, DevTools toggle, error overlay
  recovery) and one drained items the other was waiting on.
- `publish()` is callable from any thread (worker pool, asyncio loop,
  pywebview-shaped legacy callbacks) and never blocks.
- Backpressure: each subscriber's deque has a soft cap (deque.maxlen); the
  oldest events are dropped when the consumer falls behind.
- Format: each event is `{"event": str, "data": json_serializable}`. The SSE
  endpoint serializes this as `event: <name>\\ndata: <json>\\n\\n`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections import deque
from contextlib import contextmanager
from typing import Any, Deque, Iterator

log = logging.getLogger("sse_events")

# Cap on backlog per subscriber. ~30 minutes of chat tokens at a reasonable
# token/sec rate fits well below this; anything older is junk.
_MAX_BACKLOG = 4096


class Subscriber:
    """One open EventSource's view of the pump.

    Owns its own bounded deque and asyncio.Event signal. Constructed by
    `subscribe()` and torn down when its context manager exits.
    """

    def __init__(self) -> None:
        self._items: Deque[dict] = deque(maxlen=_MAX_BACKLOG)
        self._signal: asyncio.Event = asyncio.Event()
        self._lock = threading.Lock()

    def _append(self, item: dict) -> None:
        with self._lock:
            self._items.append(item)

    async def drain(self) -> list[dict]:
        """Wait for at least one event, then return everything currently queued."""
        await self._signal.wait()
        with self._lock:
            out = list(self._items)
            self._items.clear()
            self._signal.clear()
        return out

    def drain_nowait(self) -> list[dict]:
        """Synchronous drain. Returns whatever is queued (possibly empty)."""
        with self._lock:
            out = list(self._items)
            self._items.clear()
            self._signal.clear()
        return out

    def queue_size(self) -> int:
        with self._lock:
            return len(self._items)


_loop: asyncio.AbstractEventLoop | None = None
_subscribers: list[Subscriber] = []
_lock = threading.Lock()


def attach_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Bind the asyncio loop that publish() will use for cross-thread wakeups."""
    global _loop
    _loop = loop


def publish(event: str, payload: Any = None) -> None:
    """Fan out (event, payload) to every active subscriber. Safe from any thread."""
    try:
        data = payload if payload is not None else {}
        json.dumps(data)  # validate serializable up front
    except (TypeError, ValueError) as exc:
        log.debug("publish: dropping non-serializable payload for %s: %s", event, exc)
        return
    item = {"event": event, "data": data}
    with _lock:
        subs = list(_subscribers)
    for sub in subs:
        sub._append(item)
    loop = _loop
    if loop is not None:
        for sub in subs:
            try:
                loop.call_soon_threadsafe(sub._signal.set)
            except RuntimeError:
                pass


@contextmanager
def subscribe() -> Iterator[Subscriber]:
    """Register a Subscriber for the lifetime of the with-block."""
    if _loop is None:
        raise RuntimeError("attach_loop() must be called before subscribe()")
    sub = Subscriber()
    with _lock:
        _subscribers.append(sub)
    try:
        yield sub
    finally:
        with _lock:
            try:
                _subscribers.remove(sub)
            except ValueError:
                pass


def subscriber_count() -> int:
    """Diagnostic helper: number of currently-registered subscribers."""
    with _lock:
        return len(_subscribers)
