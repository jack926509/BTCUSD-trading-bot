import asyncio
import os
from datetime import datetime
from enum import Enum


class BreakerState(Enum):
    CLOSED = "CLOSED"
    OPEN   = "OPEN"
    HALF   = "HALF"


class CircuitBreaker:
    def __init__(self, db=None, tg=None):
        self.db          = db
        self.tg          = tg
        self.state       = BreakerState.CLOSED
        self.loss_streak = 0
        self.opened_at   = None
        self.max_losses  = int(os.getenv("CB_MAX_LOSSES", "3"))
        self.pause_hours = float(os.getenv("CB_PAUSE_HOURS", "4"))

    async def load_state(self, db):
        """啟動時從 DB 恢復熔斷器狀態"""
        self.db = db
        row = await db.get_circuit_breaker_state()
        if row:
            self.state       = BreakerState[row["state"]]
            self.loss_streak = row["loss_streak"]
            opened_at_str    = row.get("opened_at")
            if opened_at_str:
                try:
                    self.opened_at = datetime.fromisoformat(opened_at_str)
                except ValueError:
                    self.opened_at = None
            else:
                self.opened_at = None

            if self.state == BreakerState.OPEN and self.opened_at:
                elapsed_h = (datetime.utcnow() - self.opened_at).total_seconds() / 3600
                if elapsed_h >= self.pause_hours:
                    self.state = BreakerState.HALF
                    await self._persist(db)

        print(
            f"Circuit breaker state restored: {self.state.value} "
            f"(streak: {self.loss_streak})"
        )

    def is_open(self) -> bool:
        """OPEN 態拒絕新訊號；HALF 態允許試單"""
        return self.state == BreakerState.OPEN

    def record(self, close_reason: str):
        """
        HALF 態試單失敗時，重置 loss_streak = 1（記錄本次失敗），
        而非延用舊值，避免「永遠無法恢復」的狀態鎖。
        """
        if close_reason in ("SL", "INVALIDATED", "TRAILING_SL", "HARD_SL"):
            if self.state == BreakerState.HALF:
                self.state       = BreakerState.OPEN
                self.opened_at   = datetime.utcnow()
                self.loss_streak = 1
            else:
                self.loss_streak += 1
                if self.loss_streak >= self.max_losses:
                    self.state     = BreakerState.OPEN
                    self.opened_at = datetime.utcnow()
        else:
            self.loss_streak = 0
            if self.state == BreakerState.HALF:
                self.state = BreakerState.CLOSED

        asyncio.create_task(self._persist_with_fallback())

    async def _persist(self, db):
        await db.save_circuit_breaker_state(
            self.state.value, self.loss_streak, self.opened_at
        )

    async def _persist_with_fallback(self):
        """
        DB 寫入失敗：強制 OPEN 以最保守姿態運行。
        """
        try:
            await self._persist(self.db)
        except Exception as e:
            self.state = BreakerState.OPEN
            if self.tg:
                await self.tg.alert(
                    f"🔴 熔斷器 DB 持久化失敗：{e}\n已強制切換為 OPEN 狀態",
                    level="CRITICAL",
                )

    def get_state_summary(self) -> dict:
        return {
            "state":       self.state.value,
            "loss_streak": self.loss_streak,
            "opened_at":   self.opened_at.isoformat() if self.opened_at else None,
        }
