import asyncio
from dataclasses import dataclass
from typing import Any


@dataclass
class AnalysisJob:
    symbol: str
    signal: Any
    ctx:    Any = None


class AnalysisQueue:
    def __init__(self, max_size: int = 50, tg=None):
        self._queue    = asyncio.Queue(maxsize=max_size)
        self._tg       = tg
        self._dropped  = 0

    async def enqueue(self, symbol: str, ctx):
        """
        將 SMC 訊號放入佇列。
        FIX P2-1: 佇列滿時記錄丟棄次數並告警，而非靜默丟棄。
        """
        signal = ctx.get_signal() if hasattr(ctx, "get_signal") else ctx
        job    = AnalysisJob(symbol=symbol, signal=signal, ctx=ctx)
        try:
            self._queue.put_nowait(job)
        except asyncio.QueueFull:
            self._dropped += 1
            msg = f"[AnalysisQueue] 訊號佇列已滿，丟棄第 {self._dropped} 筆訊號 ({symbol})"
            print(msg)
            if self._tg and self._dropped % 10 == 1:
                # 每 10 筆才推一次 Telegram，避免洗版
                asyncio.create_task(
                    self._tg.alert(
                        f"⚠️ 訊號佇列滿載，已累計丟棄 {self._dropped} 筆訊號",
                        level="WARNING",
                    )
                )

    async def worker(self, handler):
        """持續消費佇列，依序呼叫 handler(job)。"""
        while True:
            job = await self._queue.get()
            try:
                await handler(job)
            except Exception as e:
                print(f"[AnalysisQueue] worker error: {e}")
            finally:
                self._queue.task_done()
