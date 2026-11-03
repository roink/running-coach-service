"""Graceful SIGTERM handling for Python running as container PID 1."""

import asyncio
import signal


async def run_service(coroutine):
    loop = asyncio.get_running_loop()
    task = asyncio.create_task(coroutine)
    for signum in [signal.SIGTERM, signal.SIGINT]:
        loop.add_signal_handler(signum, task.cancel)
    try:
        await task
    except asyncio.CancelledError:
        pass
    finally:
        for signum in [signal.SIGTERM, signal.SIGINT]:
            loop.remove_signal_handler(signum)
