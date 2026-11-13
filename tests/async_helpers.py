"""Run async workflows from the existing synchronous SDK integration fixtures."""

import asyncio
from functools import wraps
import inspect
import threading


def wait_for_reader(engine, through):
    """Keep synchronous test publishers deterministic while the reader runs."""
    with engine.condition:
        if not engine.condition.wait_for(lambda: engine._confirmation_ready(through), timeout=5):
            raise AssertionError("test reader did not reach the published event")


class AsyncRunner:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True,
                                       name="test-async-workflows")
        self.thread.start()

    def run(self, awaitable, *, timeout=30):
        if not inspect.isawaitable(awaitable):
            return awaitable
        try:
            if asyncio.get_running_loop() is self.loop:
                return awaitable
        except RuntimeError:
            pass
        async def wait():
            return await awaitable
        return asyncio.run_coroutine_threadsafe(wait(), self.loop).result(timeout)

    def close(self):
        if self.loop.is_closed():
            return
        async def cancel_remaining():
            pending = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        self.run(cancel_remaining())
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)
        if self.thread.is_alive():
            raise AssertionError("test event loop did not stop")
        self.loop.close()


class SyncProxy:
    __slots__ = ("_target", "_runner")

    def __init__(self, target, runner):
        object.__setattr__(self, "_target", target)
        object.__setattr__(self, "_runner", runner)

    @property
    def __dict__(self):
        return self._target.__dict__

    def __getattr__(self, name):
        value = getattr(self._target, name)
        if not inspect.iscoroutinefunction(value):
            return value
        @wraps(value)
        def invoke(*args, **kwargs):
            return self._runner.run(value(*args, **kwargs))
        return invoke

    def __setattr__(self, name, value):
        original = getattr(self._target, name, None)
        if inspect.iscoroutinefunction(original) and callable(value) and not inspect.iscoroutinefunction(value):
            callback = value
            @wraps(callback)
            async def invoke(*args, **kwargs):
                result = callback(*args, **kwargs)
                return await result if inspect.isawaitable(result) else result
            value = invoke
        setattr(self._target, name, value)

    def __delattr__(self, name):
        delattr(self._target, name)
