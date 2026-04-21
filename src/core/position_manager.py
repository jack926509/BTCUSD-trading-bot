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
    realized_pnl:        float           = 0.0   # 累計已實現 PnL（TP1 後更新）

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

    def is_trailing_sl_triggered(self, close_price: float) -> bool:
        """Trailing 期間：K 棒收盤跌破 stop_loss 則觸發"""
        if self.state != PositionState.TRAILING:
            return False
        if self.side == "BUY":
            return close_price <= self.stop_loss
        else:
            return close_price >= self.stop_loss

    def is_tp1_hit(self, price: float) -> bool:
        if self.side == "BUY":
            return price >= self.take_profit_1
        else:
            return price <= self.take_profit_1

    def is_tp2_hit(self, price: float) -> bool:
        if self.side == "BUY":
            return price >= self.take_profit_2
        else:
            return price <= self.take_profit_2

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
    def __init__(self, on_close, db, tg, smc, circuit=None):
        self.positions:  dict = {}
        self._on_close       = on_close
        self.db              = db
        self.tg              = tg
        self.smc             = smc
        self.circuit         = circuit
        self.executor        = None      # set by TradingSystem after init

        config_dir   = os.getenv("CONFIG_DIR", "/app/config")
        rules_path   = os.path.join(config_dir, "position_rules.yaml")
        try:
            with open(rules_path, "r") as f:
                rules = yaml.safe_load(f)
        except FileNotFoundError:
            print(f"[WARN] position_rules.yaml not found at {rules_path}, using defaults")
            rules = {}

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
        pos.trade_id           = trade_id
        self.positions[symbol] = pos
        return pos

    # ── Tick Handler (Fast Track) ─────────────────────────────────────────────

    async def on_tick(self, symbol: str, price: float):
        pos = self.positions.get(symbol)
        if not pos or pos.state not in (PositionState.OPEN, PositionState.TRAILING):
            return

        # TP1 check（只在 OPEN 態，避免重複觸發）
        if self._tp_enabled and pos.state == PositionState.OPEN and pos.is_tp1_hit(price):
            pos.state = PositionState.TRAILING
            asyncio.create_task(self._execute_tp1(pos, price))
            return

        # TP2 check（TRAILING 態，全倉出場）
        if pos.state == PositionState.TRAILING and pos.is_tp2_hit(price):
            pos.state = PositionState.CLOSING
            asyncio.create_task(self._execute_tp2_close(pos, price))
            return

        # Hard SL: 不等收盤，盤中觸發立即出場
        if pos.is_hard_sl_triggered(price):
            pos.state = PositionState.CLOSING
            asyncio.create_task(self._execute_hard_sl_close(pos, price))

    async def _execute_tp1(self, pos: Position, price: float):
        """TP1 部分平倉；更新 last_pnl"""
        if not self.executor:
            return
        try:
            close_qty = round(pos.qty * self._tp1_close_pct, 8)
            realized  = (price - pos.entry_price) * close_qty if pos.side == "BUY" else \
                        (pos.entry_price - price) * close_qty

            await self.executor.partial_close(pos.symbol, pos.side, close_qty)

            pos.qty          -= close_qty
            pos.realized_pnl += realized
            pos.last_pnl      = realized
            pos.stop_loss     = pos.entry_price     # Move SL to entry（已鎖利）

            self.tg.mark_state_changed("SL_MOVED")
            asyncio.create_task(self.tg.notify_tp1(pos.symbol, pos, realized))
        except Exception as e:
            pos.state = PositionState.OPEN  # 回滾狀態，下次 tick 可重試
            await self.tg.alert(f"⚠️ TP1 減倉失敗：{e}", level="WARNING")

    async def _execute_tp2_close(self, pos: Position, price: float):
        """TP2 全倉出場（Trailing 後達到 3R 目標）"""
        if not self.executor:
            return
        try:
            order = await self.executor.close_position(pos)
            fill  = float(getattr(order, "filled_avg_price", price) or price)
            pos.last_pnl = (fill - pos.entry_price) * pos.qty if pos.side == "BUY" else \
                           (pos.entry_price - fill) * pos.qty

            if self.circuit:
                self.circuit.record("TP2")

            try:
                await self.db.close_position(pos, "TP2", order)
            except Exception as db_err:
                await self.tg.alert(f"⚠️ TP2 DB 寫入失敗：{db_err}", level="WARNING")

            await self.tg.notify_close(pos, "TP2",
                extra=f"TP2 全倉出場：${fill:,.0f}，獲利 +${pos.last_pnl:,.0f}")
            self.positions.pop(pos.symbol, None)
            await self._on_close(pos, "TP2")
        except Exception as e:
            pos.state = PositionState.TRAILING
            await self.tg.alert(f"🔴 TP2 平倉失敗：{e}", level="CRITICAL")

    async def _execute_hard_sl_close(self, pos: Position, trigger_price: float):
        """circuit.record；await db/tg"""
        if not self.executor:
            return
        try:
            order = await self.executor.close_position(pos)
            fill  = float(getattr(order, "filled_avg_price", trigger_price) or trigger_price)
            pos.last_pnl = (fill - pos.entry_price) * pos.qty if pos.side == "BUY" else \
                           (pos.entry_price - fill) * pos.qty

            if self.circuit:
                self.circuit.record("HARD_SL")

            try:
                await self.db.close_position(pos, "HARD_SL", order)
            except Exception as db_err:
                await self.tg.alert(f"⚠️ Hard SL DB 寫入失敗：{db_err}", level="WARNING")

            await self.tg.notify_close(
                pos, "HARD_SL",
                extra=f"插針觸發價：${trigger_price:,.0f}\n出場成交：${fill:,.0f}（市價）\n虧損：−${abs(pos.last_pnl):,.0f}"
            )
            self.positions.pop(pos.symbol, None)
            await self._on_close(pos, "HARD_SL")
        except Exception as e:
            pos.state = PositionState.OPEN  # 回滾，允許下次 tick 重試
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

        # INVALIDATED check（收盤確認）
        if pos.is_invalidated(candle["close"]):
            pos.state = PositionState.CLOSING
            asyncio.create_task(self._execute_invalidated_close(pos, candle))
            return

        # Trailing stop: 收盤跌破 stop_loss → TRAILING_SL 出場
        if pos.is_trailing_sl_triggered(candle["close"]):
            pos.state = PositionState.CLOSING
            asyncio.create_task(self._execute_trailing_sl_close(pos, candle))
            return

        # Trailing stop level 更新
        if pos.state == PositionState.TRAILING:
            old_sl = pos.stop_loss
            pos.update_structural_trailing(candle)
            if pos.stop_loss != old_sl:
                self.tg.mark_state_changed("SL_MOVED")

        # Max hold check：超時且虧損則出場；超時且獲利也出場（不無限持有）
        elapsed_h = (datetime.now(timezone.utc) - pos.open_time).total_seconds() / 3600
        if elapsed_h >= self._max_hold_hours:
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
            fill  = float(getattr(order, "filled_avg_price", candle["close"]) or candle["close"])
            pos.last_pnl = (fill - pos.entry_price) * pos.qty if pos.side == "BUY" else \
                           (pos.entry_price - fill) * pos.qty

            if self.circuit:
                self.circuit.record("INVALIDATED")

            try:
                await self.db.close_position(pos, "INVALIDATED", order)
            except Exception as db_err:
                await self.tg.alert(f"⚠️ INVALIDATED DB 寫入失敗：{db_err}", level="WARNING")

            await self.tg.notify_close(pos, "INVALIDATED")
            self.positions.pop(pos.symbol, None)
            await self._on_close(pos, "INVALIDATED")
        except Exception as e:
            pos.state = PositionState.OPEN
            await self.tg.alert(f"🔴 INVALIDATED 平倉失敗：{e}", level="CRITICAL")

    async def _execute_trailing_sl_close(self, pos: Position, candle: dict):
        """Trailing SL 收盤確認出場"""
        if not self.executor:
            return
        try:
            order = await self.executor.close_position(pos)
            fill  = float(getattr(order, "filled_avg_price", candle["close"]) or candle["close"])
            pos.last_pnl = (fill - pos.entry_price) * pos.qty if pos.side == "BUY" else \
                           (pos.entry_price - fill) * pos.qty

            if self.circuit:
                self.circuit.record("TRAILING_SL")

            try:
                await self.db.close_position(pos, "TRAILING_SL", order)
            except Exception as db_err:
                await self.tg.alert(f"⚠️ TRAILING_SL DB 寫入失敗：{db_err}", level="WARNING")

            await self.tg.notify_close(pos, "TRAILING_SL")
            self.positions.pop(pos.symbol, None)
            await self._on_close(pos, "TRAILING_SL")
        except Exception as e:
            pos.state = PositionState.TRAILING
            await self.tg.alert(f"🔴 TRAILING_SL 平倉失敗：{e}", level="CRITICAL")

    async def _execute_htf_flip_close(self, pos: Position):
        if not self.executor:
            return
        try:
            order = await self.executor.close_position(pos)
            fill  = float(getattr(order, "filled_avg_price", pos.entry_price) or pos.entry_price)
            pos.last_pnl = (fill - pos.entry_price) * pos.qty if pos.side == "BUY" else \
                           (pos.entry_price - fill) * pos.qty

            if self.circuit:
                self.circuit.record("HTF_FLIP")

            try:
                await self.db.close_position(pos, "HTF_FLIP", order)
            except Exception as db_err:
                await self.tg.alert(f"⚠️ HTF_FLIP DB 寫入失敗：{db_err}", level="WARNING")

            await self.tg.notify_close(pos, "HTF_FLIP")
            self.positions.pop(pos.symbol, None)
            await self._on_close(pos, "HTF_FLIP")
        except Exception as e:
            pos.state = PositionState.OPEN
            await self.tg.alert(f"🔴 HTF_FLIP 平倉失敗：{e}", level="CRITICAL")

    # ── Reconciliation ────────────────────────────────────────────────────────

    def has_open_positions(self) -> bool:
        return len(self.positions) > 0

    async def adopt_orphan(self, symbol: str, pos_data):
        """接管 Alpaca 持倉並寫入 DB"""
        try:
            qty       = float(pos_data.qty)
            avg_entry = float(pos_data.avg_entry_price)
            side      = "BUY" if pos_data.side.value == "long" else "SELL"

            pos = Position(
                symbol             = symbol,
                side               = side,
                entry_price        = avg_entry,
                qty                = qty,
                notional           = qty * avg_entry,
                stop_loss          = avg_entry * (0.98 if side == "BUY" else 1.02),
                hard_sl_price      = avg_entry * (0.977 if side == "BUY" else 1.023),
                take_profit_1      = avg_entry * (1.04 if side == "BUY" else 0.96),
                take_profit_2      = avg_entry * (1.06 if side == "BUY" else 0.94),
                invalidation_level = avg_entry * (0.975 if side == "BUY" else 1.025),
                trailing_pivot_bars = self._trailing_pivot,
                structural_buffer   = self._struct_buffer,
                min_trail_pct       = self._min_trail_pct,
            )

            try:
                trade_id = await self.db.open_trade(
                    analysis_id  = 0,
                    symbol       = symbol,
                    side         = side,
                    notional     = qty * avg_entry,
                    fill_price   = avg_entry,
                    limit_price  = avg_entry,
                    stop_loss    = pos.stop_loss,
                    hard_sl_price= pos.hard_sl_price,
                    take_profit  = pos.take_profit_1,
                )
                pos.trade_id = trade_id
            except Exception as db_err:
                await self.tg.alert(f"⚠️ adopt_orphan DB 寫入失敗：{db_err}", level="WARNING")

            self.positions[symbol] = pos
        except Exception as e:
            await self.tg.alert(f"⚠️ adopt_orphan 失敗：{e}", level="WARNING")

    async def force_close_missing(self, symbol: str):
        """本地有記錄但 Alpaca 上已無持倉（Server Stop 觸發）→ 同步 DB 並清除"""
        pos = self.positions.pop(symbol, None)
        if not pos:
            return
        if pos.trade_id:
            try:
                await self.db.close_trade(
                    trade_id        = pos.trade_id,
                    close_price     = 0.0,    # 成交價未知（server stop 觸發）
                    pnl_usd         = 0.0,
                    close_reason    = "SERVER_STOP_TRIGGERED",
                    broker_order_id = None,
                )
            except Exception as e:
                await self.tg.alert(f"⚠️ force_close_missing DB 更新失敗：{e}", level="WARNING")
        await self.tg.alert(
            f"⚠️ Reconcile：{symbol} 持倉已在 Alpaca 消失（Server Stop 可能已觸發），已同步清除",
            level="WARNING",
        )

    async def reconcile_single(self, order):
        """Ghost 訂單確認成交後，建立本地追蹤持倉"""
        try:
            symbol     = order.symbol
            side_val   = str(order.side.value).lower()
            side       = "BUY" if side_val == "buy" else "SELL"
            fill_price = float(order.filled_avg_price or 0)
            qty        = float(order.filled_qty or 0)

            if not fill_price or not qty:
                await self.tg.alert(
                    f"⚠️ reconcile_single：訂單 {order.id} 無有效成交資料，略過",
                    level="WARNING"
                )
                return

            if symbol in self.positions:
                return  # 已追蹤，不重複建立

            pos = Position(
                symbol              = symbol,
                side                = side,
                entry_price         = fill_price,
                qty                 = qty,
                notional            = qty * fill_price,
                stop_loss           = fill_price * (0.98  if side == "BUY" else 1.02),
                hard_sl_price       = fill_price * (0.977 if side == "BUY" else 1.023),
                take_profit_1       = fill_price * (1.04  if side == "BUY" else 0.96),
                take_profit_2       = fill_price * (1.06  if side == "BUY" else 0.94),
                invalidation_level  = fill_price * (0.975 if side == "BUY" else 1.025),
                trailing_pivot_bars = self._trailing_pivot,
                structural_buffer   = self._struct_buffer,
                min_trail_pct       = self._min_trail_pct,
            )

            try:
                trade_id = await self.db.open_trade(
                    analysis_id   = 0,
                    symbol        = symbol,
                    side          = side,
                    notional      = qty * fill_price,
                    fill_price    = fill_price,
                    limit_price   = fill_price,
                    stop_loss     = pos.stop_loss,
                    hard_sl_price = pos.hard_sl_price,
                    take_profit   = pos.take_profit_1,
                )
                pos.trade_id = trade_id
            except Exception as db_err:
                await self.tg.alert(f"⚠️ reconcile_single DB 寫入失敗：{db_err}", level="WARNING")

            self.positions[symbol] = pos
            await self.tg.alert(
                f"⚠️ Ghost 訂單 {order.id} 已成交並接管：{symbol} {side} "
                f"{qty:.6f} @ ${fill_price:,.0f}（已套用預設 SL）",
                level="WARNING",
            )
        except Exception as e:
            await self.tg.alert(f"⚠️ reconcile_single 失敗：{e}", level="WARNING")

    def get_float_pnl(self, symbol: str, price: float) -> float:
        pos = self.positions.get(symbol)
        if not pos:
            return 0.0
        return pos.calc_float_pnl(price)
