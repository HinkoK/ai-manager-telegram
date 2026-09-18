"""Очередь на каждый чат: сообщения одного человека обрабатываются по порядку.

Почему очередь, а не asyncio.Lock: блокировка даёт FIFO только среди тех,
кто уже успел дойти до acquire(), а порядок запуска задач в этом месте не
гарантирован. Очередь с одним воркером на чат гарантирует порядок всегда.
Чаты друг друга не ждут: у каждого свой воркер.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

Job = Callable[[], Awaitable[None]]


class ChatDispatcher:
    def __init__(self, idle_sec: float = 300.0) -> None:
        self._idle_sec = idle_sec
        self._queues: dict[int, asyncio.Queue[Job]] = {}
        self._workers: dict[int, asyncio.Task[None]] = {}
        self._guard = asyncio.Lock()
        self._closing = False

    async def submit(self, chat_id: int, job: Job) -> None:
        async with self._guard:
            if self._closing:
                raise RuntimeError("dispatcher закрывается")
            queue = self._queues.get(chat_id)
            if queue is None:
                queue = asyncio.Queue()
                self._queues[chat_id] = queue
                self._workers[chat_id] = asyncio.create_task(
                    self._worker(chat_id, queue), name=f"chat-{chat_id}"
                )
            queue.put_nowait(job)

    async def wait_idle(self) -> None:
        """Ждём, пока все принятые задачи доработают. Зовёт поллер перед новым батчем."""
        async with self._guard:
            queues = list(self._queues.values())
        for queue in queues:
            await queue.join()

    async def close(self, timeout: float) -> None:
        async with self._guard:
            self._closing = True
            queues = list(self._queues.values())
            workers = list(self._workers.values())
        try:
            await asyncio.wait_for(
                asyncio.gather(*(q.join() for q in queues)), timeout=timeout
            )
        except asyncio.TimeoutError:
            log.warning("обработчики не успели закончиться за %s c, обрываем", timeout)
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

    async def _worker(self, chat_id: int, queue: asyncio.Queue[Job]) -> None:
        while True:
            try:
                job = await asyncio.wait_for(queue.get(), timeout=self._idle_sec)
            except asyncio.TimeoutError:
                async with self._guard:
                    # Пока мы ждали guard, submit мог положить задачу. Проверяем.
                    if queue.empty():
                        self._queues.pop(chat_id, None)
                        self._workers.pop(chat_id, None)
                        return
                continue
            except asyncio.CancelledError:
                return
            try:
                await job()
            except Exception:
                # Падение одного сообщения не должно убивать очередь чата.
                log.exception("обработчик упал", extra={"chat_id": chat_id})
            finally:
                # Ровно один task_done на каждый get, иначе join() зависнет.
                # Отмена задачи тоже проходит здесь.
                queue.task_done()
