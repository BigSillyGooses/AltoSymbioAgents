"""services/workers/base.py — Worker base class."""

from __future__ import annotations

from typing import Callable


class Worker:
    """A background worker.

    Subclasses set ``name``/``description`` and implement ``run``. ``run`` is
    synchronous (workers do bounded DB/index work); the daemon executes it in a
    thread so the event loop is never blocked. ``progress(fraction, message)``
    streams incremental status to the UI over SSE.
    """

    name: str = "base"
    description: str = ""

    def run(self, params: dict, progress: Callable[[float, str], None]) -> dict:
        raise NotImplementedError
