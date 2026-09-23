"""后台任务登记处。asyncio.create_task 只留弱引用,没人持有的任务可能被 GC 中途回收;
这里持有强引用、结束时自动移除,异常记 task_error(否则只会在回收时打一行没人看的 warning)。"""

import asyncio
from collections.abc import Coroutine

from . import events

_tasks: set[asyncio.Task] = set()


def spawn(coro: Coroutine, *, name: str) -> asyncio.Task:
    t = asyncio.create_task(coro, name=name)
    _tasks.add(t)
    t.add_done_callback(_done)
    return t


def _done(t: asyncio.Task) -> None:
    _tasks.discard(t)
    if t.cancelled():
        return
    exc = t.exception()
    if exc is not None:
        try:
            events.log("task_error", {"task": t.get_name(), "error": f"{type(exc).__name__}: {exc}"[:300]})
        except Exception:  # noqa: BLE001 — 关机时 DB 可能已关,日志写不进就算了
            pass


async def drain(timeout: float = 3) -> None:
    """关机时给后台任务(比如正在念的回复)一点时间收尾,超时的取消掉。"""
    pending = [t for t in _tasks if not t.done()]
    if not pending:
        return
    _, still = await asyncio.wait(pending, timeout=timeout)
    for t in still:
        t.cancel()
