import asyncio
import functools
import inspect

import caproto as ca


class AsyncioQueue:
    '''
    Asyncio queue modified for caproto server layer queue API compatibility

    NOTE: This is bound to a single event loop for compatibility with
    synchronous requests.
    '''

    def __init__(self, maxsize=0):
        self._queue = asyncio.Queue(maxsize)

    async def async_get(self):
        return await self._queue.get()

    async def async_put(self, value):
        return await self._queue.put(value)

    def get(self):
        future = asyncio.run_coroutine_threadsafe(
            self._queue.get(), asyncio.get_running_loop())

        return future.result()

    def put(self, value):
        self._queue.put_nowait(value)


class _DatagramProtocol(asyncio.Protocol):
    def __init__(self, parent, recv_func):
        self.transport = None
        self.parent = parent
        self.recv_func = recv_func

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        if not data:
            return

        self.recv_func((data, addr))

    def error_received(self, ex):
        self.parent.log.error('%s receive error', self, exc_info=ex)


class _StreamProtocol(asyncio.Protocol):
    def __init__(self, parent, connection_callback, recv_func):
        self.connection_callback = connection_callback
        self.parent = parent
        self.recv_func = recv_func
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport
        self.connection_callback(True, transport)

    def eof_received(self):
        self.connection_callback(False, None)
        return False

    def connection_lost(self, exc):
        self.transport = None
        self.connection_callback(False, exc)

    def data_received(self, data):
        self.recv_func(data)

    def error_received(self, ex):
        self.parent.log.error('%s receive error', self, exc_info=ex)


class _TransportWrapper:
    """Make an asyncio transport something you can call sendto on."""
    # NOTE: taken from the server - combine usage
    def __init__(self, transport):
        self.transport = transport

    def getsockname(self):
        return self.transport.get_extra_info('sockname')

    async def sendto(self, bytes_to_send, addr_port):
        try:
            self.transport.sendto(bytes_to_send, addr_port)
        except OSError as exc:
            host, port = addr_port
            raise ca.CaprotoNetworkError(
                f"Failed to send to {host}:{port}") from exc

    def close(self):
        return self.transport.close()


class _TaskHandler:
    def __init__(self):
        self.tasks = []

    def create(self, coro):
        """Schedule the execution of a coroutine object in a spawn task."""
        task = asyncio.create_task(coro)
        self.tasks.append(task)
        return task

    async def cancel(self, task):
        self.tasks.remove(task)
        await asyncio.wait(task)

    async def cancel_all(self, wait=False):
        for task in list(self.tasks):
            task.cancel()

        if wait:
            await asyncio.wait(self.tasks)

        self.tasks.clear()


class _CallbackExecutor:
    def __init__(self, log):
        self.callbacks = AsyncioQueue()
        self.tasks = _TaskHandler()
        self.tasks.create(self._callback_loop())
        self.log = log

    async def shutdown(self):
        await self.tasks.cancel_all()

    async def _callback_loop(self):
        loop = asyncio.get_running_loop()
        # self.user_callback_executor = concurrent.futures.ThreadPoolExecutor(
        #      max_workers=self.context.max_workers,
        #      thread_name_prefix='user-callback-executor'
        # )

        while True:
            callback, args, kwargs = await self.callbacks.async_get()
            if inspect.iscoroutinefunction(callback):
                try:
                    await callback(*args, **kwargs)
                except Exception:
                    self.log.exception('Callback failure')
            else:
                try:
                    loop.run_in_executor(None, functools.partial(callback, *args,
                                                                 **kwargs))
                except Exception:
                    self.log.exception('Callback failure')

    def submit(self, callback, *args, **kwargs):
        self.callbacks.put((callback, args, kwargs))
