import asyncio
import os
from datetime import datetime, timezone
from enum import Enum


class BreakerState(Enum):
    CLOSED = "CLOSED"
    OPEN   = "OPEN"
    HALF   = "HALF"


# 純機械性風險出場（不論 pnl 正負都視為風險事件；實際勝負由 pnl 決定）
_MECHANICAL_EXIT_REASONS = {"SL", "INVALIDATED", "TRAILING_SL", "HARD_SL", "HTF_FLIP"}

# 中性事件（不影響連虧計數）
_NEUTRAL_REASONS = {"MANUAL_CLOSE", "MAX_HOLD", "SERVER_STOP_TRIGGERED"}


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

        if os.getenv("RESET_CIRCUIT_BREAKER", "false").lower() == "true":
            self.state       = BreakerState.CLOSED
            self.loss_streak = 0
            self.opened_at   = None
            await self._persist(db)
            print("[CB] RESET_CIRCUIT_BREAKER=true → state forced to CLOSED")
            return

        row = await db.get_circuit_breaker_state()
        if row:
            self.state       = BreakerState[row["state"]]
            self.loss_streak = row["loss_streak"]
            opened_at_str    = row.get("opened_at")
            if opened_at_str:
                try:
                    dt = datetime.fromisoformat(opened_at_str)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    self.opened_at = dt
                except ValueError:
                    self.opened_at = None
            else:
                self.opened_at = None

            await self.check_auto_recovery()

        print(
            f"Circuit breaker state restored: {self.state.value} "
            f"(streak: {self.loss_streak})"
        )

    async def check_auto_recovery(self):
        """
        OPEN 經過 pause_hours 後自動 → HALF。
        可在 load_state 或 heartbeat loop 週期呼叫，長時間運行也能恢復。
        """
        if self.state != BreakerState.OPEN or not self.opened_at:
            return
        elapsed_h = (datetime.now(timezone.utc) - self.opened_at).total_seconds() / 3600
        if elapsed_h >= self.pause_hours:
            self.state = BreakerState.HALF
            if self.db:
                await self._persist(self.db)
            if self.tg:
                await self.tg.notify_circuit_breaker("HALF", self.loss_streak)

    def is_open(self) -> bool:
        """OPEN 態拒絕新訊號；HALF 態允許試單"""
        return self.state == BreakerState.OPEN

    async def record(self, close_reason: str, pnl: float = 0.0):
        """
        以 pnl 正負判斷勝負，不再純靠 reason 字串。
        - pnl < 0 → 連虧累加
        - pnl >= 0（包含 TRAILING_SL 但 TP1 已先鎖利情況）→ 清零連虧
        - 中性事件（MANUAL_CLOSE / MAX_HOLD / SERVER_STOP_TRIGGERED）不影響

        HALF 態下任何虧損立即回到 OPEN，loss_streak 重置為 1 避免鎖死。
        """
        if close_reason in _NEUTRAL_REASONS:
            await self._persist_with_fallback()
            return

        is_loss = pnl < 0

        if is_loss:
            if self.state == BreakerState.HALF:
                self.state       = BreakerState.OPEN
                self.opened_at   = datetime.now(timezone.utc)
                self.loss_streak = 1
            else:
                self.loss_streak += 1
                if self.loss_streak >= self.max_losses:
                    self.state     = BreakerState.OPEN
                    self.opened_at = datetime.now(timezone.utc)
        else:
            self.loss_streak = 0
            if self.state == BreakerState.HALF:
                self.state     = BreakerState.CLOSED
                self.opened_at = None

        await self._persist_with_fallback()

    async def reset(self):
        """手動重置（供管理端呼叫）"""
        self.state       = BreakerState.CLOSED
        self.loss_streak = 0
        self.opened_at   = None
        if self.db:
            await self._persist(self.db)

    async def _persist(self, db):
        await db.save_circuit_breaker_state(
            self.state.value, self.loss_streak, self.opened_at
        )

    async def _persist_with_fallback(self):
        """DB 寫入失敗：強制 OPEN 以最保守姿態運行。"""
        try:
            await self._persist(self.db)
        except Exception as e:
            self.state = BreakerState.OPEN
            if self.tg:
                await self.tg.alert(
                    f"熔斷器 DB 持久化失敗：{e}\n已強制切換為 OPEN 狀態",
                    level="CRITICAL",
                )

    def get_state_summary(self) -> dict:
        return {
            "state":       self.state.value,
            "loss_streak": self.loss_streak,
            "opened_at":   self.opened_at.isoformat() if self.opened_at else None,
        }
