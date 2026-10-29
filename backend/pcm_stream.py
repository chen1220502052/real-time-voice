"""Bounded native-worker to asyncio streaming with cooperative cancellation."""
import asyncio
import queue
import threading


async def executor_chunks(executor, factory):
    loop = asyncio.get_running_loop()
    output = queue.Queue(maxsize=2)
    ready, stopped, end = asyncio.Event(), threading.Event(), object()

    def put(value):
        while not stopped.is_set():
            try:
                output.put(value, timeout=.05)
                try:
                    loop.call_soon_threadsafe(ready.set)
                except RuntimeError:
                    return False
                return True
            except queue.Full:
                pass
        return False

    def produce():
        iterator = None
        try:
            iterator = iter(factory())
            while not stopped.is_set():
                try:
                    value = next(iterator)
                except StopIteration:
                    break
                if value and not put(value):
                    break
        except Exception as error:
            put(error)
        finally:
            try:
                if iterator is not None and hasattr(iterator, 'close'):
                    try:
                        iterator.close()
                    except Exception as error:
                        put(error)
            finally:
                put(end)

    # The per-model single-worker executor remains the native-call fence.
    # Cancelling the Future never claims that a running ONNX call has stopped.
    worker = loop.run_in_executor(executor, produce)
    try:
        while True:
            try:
                value = output.get_nowait()
            except queue.Empty:
                ready.clear()
                if output.empty():
                    await ready.wait()
                continue
            if value is end:
                break
            if isinstance(value, Exception):
                raise value
            yield value
    finally:
        stopped.set()
        worker.cancel()
