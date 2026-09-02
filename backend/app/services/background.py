"""Long work started from a request and left running in the API process.

asyncio holds only weak references to tasks, so a task nobody keeps a handle
to can be garbage-collected mid-run. Everything spawned this way is retained
here until it finishes, and a crash inside it is logged rather than lost.
"""

import asyncio
import logging

logger = logging.getLogger(__name__)

_TASKS: set = set()


def _finished(task: asyncio.Task) -> None:
    _TASKS.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("background task failed", exc_info=exc)


def spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _TASKS.add(task)
    task.add_done_callback(_finished)
    return task
