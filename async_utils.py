"""Async helpers for offloading blocking work.

Uses daemon threads with asyncio.Event signaling — no polling, no busy-wait.
Avoids asyncio.to_thread() and the default executor to prevent shutdown hangs
in containerized environments.
"""

from __future__ import annotations

import asyncio
import queue
import threading
from collections.abc import AsyncIterator, Callable
from functools import partial
from typing import TypeVar

T = TypeVar("T")


async def run_blocking(func, /, *args, **kwargs) -> T:
    """Run blocking work in a daemon thread and await the result."""
    call = partial(func, *args, **kwargs)
    loop = asyncio.get_running_loop()
    done = asyncio.Event()
    result: list = []  # [ok: bool, value: T | BaseException]

    def _worker() -> None:
        try:
            result.extend((True, call()))
        except BaseException as exc:
            result.extend((False, exc))
        loop.call_soon_threadsafe(done.set)

    thread = threading.Thread(target=_worker, name="lucyd-blocking", daemon=True)
    thread.start()
    await done.wait()
    if result[0]:
        return result[1]  # type: ignore[return-value]
    raise result[1]  # type: ignore[misc]


async def threaded_stream(sync_iterable_factory: Callable[[], object]) -> AsyncIterator:
    """Run a synchronous iterable in a daemon thread, yield items async.

    sync_iterable_factory is a callable that returns an iterable (e.g. an SDK
    stream context manager body).  Items are forwarded through a queue;
    exceptions raised inside the factory propagate to the caller.
    """
    q: queue.Queue = queue.Queue()
    sentinel = object()

    def _run() -> None:
        try:
            for item in sync_iterable_factory():
                q.put(item)
        except Exception as exc:
            q.put(exc)
        finally:
            q.put(sentinel)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    try:
        while True:
            item = await run_blocking(q.get)
            if item is sentinel:
                break
            if isinstance(item, Exception):
                raise item
            yield item
    finally:
        thread.join(timeout=5)
