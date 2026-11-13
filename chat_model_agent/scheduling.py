"""Cooperative notifications and a bounded pool for synchronous SDK calls."""

import asyncio
from concurrent.futures import Future
from queue import Empty, Queue
import threading


RPC_WORKERS = 4


class AsyncCondition(threading.Condition):
    """Use the same state lock and notifications for threads and async tasks."""

    def __init__(self, lock=None):
        super().__init__(lock)
        self._async_waiters = set()

    @staticmethod
    def _wake(future):
        if not future.done():
            future.set_result(None)

    def notify_all(self):
        super().notify_all()
        waiters = tuple(self._async_waiters)
        self._async_waiters.clear()
        for loop, future in waiters:
            if not loop.is_closed():
                try:
                    loop.call_soon_threadsafe(self._wake, future)
                except RuntimeError:
                    if not loop.is_closed():
                        raise

    async def wait_async(self, predicate, timeout=None):
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + max(0, timeout)
        while True:
            with self:
                if predicate():
                    return True
                remaining = None if deadline is None else deadline - loop.time()
                if remaining is not None and remaining <= 0:
                    return False
                future = loop.create_future()
                waiter = loop, future
                self._async_waiters.add(waiter)
            try:
                if remaining is None:
                    await future
                else:
                    try:
                        await asyncio.wait_for(future, remaining)
                    except asyncio.TimeoutError:
                        with self:
                            return bool(predicate())
            finally:
                with self:
                    self._async_waiters.discard(waiter)


class RpcPool:
    """Fixed daemon workers: SDK calls never own a conversation's waits."""

    def __init__(self, size=RPC_WORKERS):
        self.size = size
        self.threads = []
        self._queue = Queue()
        self._lock = threading.Lock()
        self._closed = False

    def submit(self, operation, *args, **kwargs):
        with self._lock:
            if self._closed:
                raise RuntimeError("Agent RPC pool is closed")
            if not self.threads:
                try:
                    for index in range(self.size):
                        thread = threading.Thread(target=self._work, name=f"chat-rpc-{index}", daemon=True)
                        thread.start()
                        self.threads.append(thread)
                except Exception:
                    self._closed = True
                    for _ in self.threads:
                        self._queue.put(None)
                    raise
            future = Future()
            self._queue.put((future, operation, args, kwargs))
            return future

    async def call(self, operation, *args, **kwargs):
        return await asyncio.wrap_future(self.submit(operation, *args, **kwargs))

    def _work(self):
        while True:
            item = self._queue.get()
            if item is None:
                return
            future, operation, args, kwargs = item
            if not future.set_running_or_notify_cancel():
                continue
            try:
                result = operation(*args, **kwargs)
            except BaseException as error:
                future.set_exception(error)
            else:
                future.set_result(result)

    def close(self):
        # Cancel queued calls. In-flight calls keep daemon workers until the
        # transport closes or their own RPC budgets expire; never join here.
        with self._lock:
            if self._closed:
                return
            self._closed = True
            while True:
                try:
                    item = self._queue.get_nowait()
                except Empty:
                    break
                if item is not None:
                    item[0].cancel()
            for _ in self.threads:
                self._queue.put(None)
