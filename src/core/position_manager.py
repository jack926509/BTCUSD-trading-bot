import asyncio
import os
import yaml
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class PositionState(Enum):
    OPEN     = "OPEN"
    TRAILING = "TRAILING"
    CLOSING  = "CLOSING"
    CLOSED   = "CLOSED"


@dataclass
class Position:
    symbol:              str
    side:                str          # BUY / SELL
    entry_price:         float
    qty:                 float
    notional:            float
    stop_loss:           float
    hard_sl_price:       float
    take_profit_1:       float
    take_profit_2:       float
    invalidation_level:  float
    state:               PositionState = PositionState.OPEN
    trade_id:            Optional[int]   = None
    analysis_id:         Optional[int]   = None
    server_stop_order_id: Optional[str]  = None
    open_time:           datetime        = field(default_factory=lambda: datetime.now(timezone.utc))
    trailing_pivot_bars: int             = 3
    structural_buffer:   float           = 50.0
    min_trail_pct:       float           = 0.003
    last_pnl:            float           = 0.0

    # Rolling candle window for trailing stop
    _candle_buffer: deque = field(default_factory=lambda: deque(maxlen=20))

    def is_hard_sl_triggered(self, current_price: float) -> bool:
        """盤中插針防護：若價格穿越 hard_sl_price 則觸發"""
        if self.side == "BUY":
            return current_price <= self.hard_sl_price
        else:
            return current_price >= self.hard_sl_price

    def is_invalidated(self, close_price: float) -> bool:
        """收盤確認 INVALIDATED：結構失效則觸發"""
        if self.side == "BUY":
            return close_price <= self.invalidation_level
        else:
            return close_price >= self.invalidation_level

    def is_tp1_hit(self, price: float) -> bool:
        if self.side == "BUY":
            return price >= self.take_profit_1
        else:
            return price <= self.take_profit_1

    def update_structural_trailing(self, candle: dict):
        """
        跟蹤 LTF Swing Low/High，而非固定百分比。
        Pivot 確認：左右各 trailing_pivot_bars 根均低於/高於中間根。
        """
        swing = self._detect_swing(candle)
        if not swing:
            return
        if self.side == "BUY":
            candidate = swing["low"] - self.structural_buffer
            if (candidate > self.stop_loss and
                    (candidate - self.stop_loss) >= self.entry_price * self.min_trail_pct):
                self.stop_loss     = candidate
                self.hard_sl_price = round(candidate * (1 - 0.003), 2)
        elif self.side == "SELL":
            candidate = swing["high"] + self.structural_buffer
            if (candidate < self.stop_loss and
                    (self.stop_loss - candidate) >= self.entry_price * self.min_trail_pct):
                self.stop_loss     = candidate
                self.hard_sl_price = round(candidate * (1 + 0.003), 2)

    def _detect_swing(self, latest_candle: dict) -> Optional[dict]:
        """Pivot Point 確認（N 根 = trailing_pivot_bars）"""
        N = self.trailing_pivot_bars
        self._candle_buffer.append(latest_candle)
        if len(self._candle_buffer) < 2 * N + 1:
            return None

        window = list(self._candle_buffer)[-(2 * N + 1):]
        pivot  = window[N]
        left   = window[:N]
        right  = window[N + 1:]

        if all(pivot["low"] < c["low"] for c in left + right):
            return {"low": pivot["low"]}
        if all(pivot["high"] > c["high"] for c in left + right):
            return {"high": pivot["high"]}
        return None

    def calc_float_pnl(self, current_price: float) -> float:
        if self.side == "BUY":
            return (current_price - self.entry_price) * self.qty
        else:
            return (self.entry_price - current_price) * self.qty


class PositionManager:
    def __init__(self, on_close, db, tg, smc):
        self.positions:  dict = {}
        self._on_close       = on_close
        self.db              = db
        self.tg              = tg
        self.smc             = smc
        self.executor        = None  # set by TradingSystem after init

        config_dir   = os.getenv("CONFIG_DIR", "/app/config")
        rules_path   = os.path.join(config_dir, "position_rules.yaml")
        with open(rules_path, "r") as f:
            rules    = yaml.safe_load(f)

        self._tp_enabled     = rules.get("partial_tp", {}).get("enabled", True)
        self._tp1_close_pct  = rules.get("partial_tp", {}).get("tp1_close_pct", 0.5)
        self._trailing_pivot = rules.get("trailing_stop", {}).get("trailing_pivot_bars", 3)
        self._struct_buffer  = rules.get("trailing_stop", {}).get("structural_buffer_usd", 50)
        self._min_trail_pct  = rules.get("trailing_stop", {}).get("min_trail_pct", 0.003)
        self._max_hold_hours = rules.get("max_hold", {}).get("hours", 72)

    # ── Open Position ─────────────────────────────────────────────────────────

    async def open(self, symbol: str, signal, order, analysis_id=None) -> Position:
        fill_price = float(getattr(order, "filled_avg_price", signal.entry_limit_price) or signal.entry_limit_price)
        qty        = float(getattr(order, "filled_qty", 0) or 0)
        notional   = fill_price * qty

        pos = Position(
            symbol              = symbol,
            side                = signal.direction,
            entry_price         = fill_price,
            qty                 = qty,
            notional            = notional,
            stop_loss           = signal.stop_loss,
            hard_sl_price       = signal.hard_sl_price,
            take_profit_1       = signal.take_profit_1,
            take_profit_2       = signal.take_profit_2,
            invalidation_level  = signal.invalidation_level,
            analysis_id         = analysis_id,
            trailing_pivot_bars = self._trailing_pivot,
            structural_buffer   = self._struct_buffer,
            min_trail_pct       = self._min_trail_pct,
        )

        trade_id = await self.db.open_trade(
            analysis_id         = analysis_id or 0,
            symbol              = symbol,
            side                = signal.direction,
            notional            = notional,
            fill_price          = fill_price,
            limit_price         = signal.entry_limit_price,
            stop_loss           = signal.stop_loss,
            hard_sl_price       = signal.hard_sl_price,
            take_profit         = signal.take_profit_1,
        )
        pos.trade_id    = trade_id
        self.positions[symbol] = pos
        return pos

    # ── Tick Handler (Fast Track) ─────────────────────────────────────────────

    async def on_tick(self, symbol: str, price: float):
        pos = self.positions.get(symbol)
        if not pos or pos.state not in (PositionState.OPEN, PositionState.TRAILING):
            return

        # TP1 check
        if self._tp_enabled and pos.state == PositionState.OPEN and pos.is_tp1_hit(price):
            pos.state = PositionState.TRAILING
            asyncio.create_task(self._execute_tp1(pos, price))
            return

        # Hard SL: 不等收盤，盤中觸發立即出場
        if pos.is_hard_sl_triggered(price):
            pos.state = PositionState.CLOSING
            asyncio.create_task(self._execute_hard_sl_close(pos, price))

    async def _execute_tp1(self, pos: Position, price: float):
        if not self.executor:
            return
        try:
            close_qty = round(pos.qty * self._tp1_close_pct, 8)
            realized  = (price - pos.entry_price) * close_qty if pos.side == "BUY" else \
                        (pos.entry_price - price) * close_qty
            pos.qty  -= close_qty
            pos.stop_loss = pos.entry_price  # Move SL to entry
            asyncio.create_task(self.tg.notify_tp1(pos.symbol, pos, realized))
            self.tg.mark_state_changed("SL_MOVED")
        except Exception as e:
            await self.tg.alert(f"⚠️ TP1 處理失敗：{e}", level="WARNING")

    async def _execute_hard_sl_close(self, pos: Position, trigger_price: float):
        if not self.executor:
            return
        try:
            order = await self.executor.close_position(pos)
            fill  = float(getattr(order, "filled_avg_price", trigger_price) or trigger_price)
            pos.last_pnl = (fill - pos.entry_price) * pos.qty if pos.side == "BUY" else \
                           (pos.entry_price - fill) * pos.qty
            from src.risk.circuit_breaker import CircuitBreaker
            asyncio.create_task(self.db.close_position(pos, "HARD_SL", order))
            asyncio.create_task(
                self.tg.notify_close(
                    pos, "HARD_SL",
                    extra=f"插針觸發價：${trigger_price:,.0f}\n出場成交：${fill:,.0f}（市價）\n虧損：−${abs(pos.last_pnl):,.0f}"
                )
            )
            del self.positions[pos.symbol]
        except Exception as e:
            await self.tg.alert(f"🔴 Hard SL 出場失敗：{e}", level="CRITICAL")

    # ── Candle Close Handler (Fast Track) ────────────────────────────────────

    async def on_candle_close(self, symbol: str, candle: dict):
        pos = self.positions.get(symbol)
        if not pos or pos.state not in (PositionState.OPEN, PositionState.TRAILING):
            return

        # HTF bias flip hook
        if self.smc.htf_bias_changed(symbol):
            await self._handle_htf_bias_flip(pos, symbol)
            if pos.state == PositionState.CLOSING:
                return

        # INVALIDATED check (candle close)
        if pos.is_invalidated(candle["close"]):
            pos.state = PositionState.CLOSING
            asyncio.create_task(self._execute_invalidated_close(pos, candle))
            return

        # Trailing stop update
        if pos.state == PositionState.TRAILING:
            old_sl = pos.stop_loss
            pos.update_structural_trailing(candle)
            if pos.stop_loss != old_sl:
                asyncio.create_task(self.tg.mark_state_changed("SL_MOVED"))

        # Max hold check
        elapsed_h = (datetime.now(timezone.utc) - pos.open_time).total_seconds() / 3600
        if elapsed_h >= self._max_hold_hours:
            float_pnl = pos.calc_float_pnl(candle["close"])
            if float_pnl < 0:
                pos.state = PositionState.CLOSING
                asyncio.create_task(self._execute_invalidated_close(pos, candle))

    async def _handle_htf_bias_flip(self, pos: Position, symbol: str):
        new_bias = self.smc.get_htf_bias(symbol)
        if (pos.side == "BUY" and new_bias == "BEARISH") or \
           (pos.side == "SELL" and new_bias == "BULLISH"):
            await self.tg.alert(
                f"⚠️ HTF 偏向翻轉為 {new_bias}，持倉方向衝突！已提前出場",
                level="WARNING",
            )
            pos.state = PositionState.CLOSING
            asyncio.create_task(self._execute_htf_flip_close(pos))
        else:
            await self.tg.alert(
                f"ℹ️ HTF 偏向翻轉為 {new_bias}，持倉方向一致，繼續持有",
                level="INFO",
            )

    async def _execute_invalidated_close(self, pos: Position, candle: dict):
        if not self.executor:
            return
        try:
            order = await self.executor.close_position(pos)
            asyncio.create_task(self.db.close_position(pos, "INVALIDATED", order))
            asyncio.create_task(self.tg.notify_close(pos, "INVALIDATED"))
            del self.positions[pos.symbol]
        except Exception as e:
            await self.tg.alert(f"🔴 INVALIDATED 平倉失敗：{e}", level="CRITICAL")

    async def _execute_htf_flip_close(self, pos: Position):
        if not self.executor:
            return
        try:
            order = await self.executor.close_position(pos)
            asyncio.create_task(self.db.close_position(pos, "HTF_FLIP", order))
            asyncio.create_task(self.tg.notify_close(pos, "HTF_FLIP"))
            del self.positions[pos.symbol]
        except Exception as e:
            await self.tg.alert(f"🔴 HTF_FLIP 平倉失敗：{e}", level="CRITICAL")

    # ── Reconciliation ────────────────────────────────────────────────────────

    def has_open_positions(self) -> bool:
        return len(self.positions) > 0

    async def adopt_orphan(self, symbol: str, pos_data):
        """接管 Alpaca 上存在但本地未追蹤的持倉"""
        try:
            qty        = float(pos_data.qty)
            avg_entry  = float(pos_data.avg_entry_price)
            side       = "BUY" if pos_data.side.value == "long" else "SELL"
            pos = Position(
                symbol            = symbol,
                side              = side,
                entry_price       = avg_entry,
                qty               = qty,
                notional          = qty * avg_entry,
                stop_loss         = avg_entry * (0.98 if side == "BUY" else 1.02),
                hard_sl_price     = avg_entry * (0.977 if side == "BUY" else 1.023),
                take_profit_1     = avg_entry * (1.04 if side == "BUY" else 0.96),
                take_profit_2     = avg_entry * (1.06 if side == "BUY" else 0.94),
                invalidation_level= avg_entry * (0.975 if side == "BUY" else 1.025),
            )
            self.positions[symbol] = pos
        except Exception as e:
            await self.tg.alert(f"⚠️ adopt_orphan 失敗：{e}", level="WARNING")

    async def force_close_missing(self, symbol: str):
        """本地有記錄但 Alpaca 上已無持倉 → 清除"""
        self.positions.pop(symbol, None)

    async def reconcile_single(self, order):
        """處理單筆訂單的 Reconciliation"""
        pass

    def get_float_pnl(self, symbol: str, price: float) -> float:
        pos = self.positions.get(symbol)
        if not pos:
            return 0.0
        return pos.calc_float_pnl(price)
