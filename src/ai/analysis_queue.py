import asyncio
from dataclasses import dataclass
from typing import Any


@dataclass
class AnalysisJob:
    symbol: str
    signal: Any
    ctx:    Any = None


class AnalysisQueue:
    def __init__(self, max_size: int = 50):
        self._queue = asyncio.Queue(maxsize=max_size)

    async def enqueue(self, symbol: str, ctx):
        """
        將 SMC 訊號放入佇列。若佇列已滿則丟棄（訊號過期無意義）。
        """
        signal = ctx.get_signal() if hasattr(ctx, "get_signal") else ctx
        job    = AnalysisJob(symbol=symbol, signal=signal, ctx=ctx)
        try:
            self._queue.put_nowait(job)
        except asyncio.QueueFull:
            pass  # 佇列滿時丟棄，避免積壓

    async def worker(self, handler):
        """
        持續消費佇列，依序呼叫 handler(job)。
        """
        while True:
            job = await self._queue.get()
            try:
                await handler(job)
            except Exception as e:
                print(f"[AnalysisQueue] worker error: {e}")
            finally:
                self._queue.task_done()
