"""Async helpers for offloading blocking work without the default executor.

`asyncio.to_thread()` uses the event loop's shared default executor. In this
environment that executor can hang at interpreter shutdown even after tests
have finished, which stalls the audit suite. For Lucyd's short-lived blocking
calls, a one-shot daemon thread avoids that teardown path and still gives us an
`await`-able interface.
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
    result_queue: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)

    def _worker() -> None:
        try:
            result_queue.put((True, call()))
        except BaseException as exc:  # propagate to awaiter, including SDK-specific errors
            result_queue.put((False, exc))

    thread = threading.Thread(target=_worker, name="lucyd-blocking", daemon=True)
    thread.start()
    while True:
        try:
            ok, value = result_queue.get_nowait()
        except queue.Empty:
            await asyncio.sleep(0.001)
            continue
        if ok:
            return value  # type: ignore[return-value]
        raise value  # type: ignore[misc]


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
