import asyncio
import os
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Optional

from src.config.loader import get_position_rules


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
    trade_id:            Optional[int]  = None
    analysis_id:         Optional[int]  = None
    server_stop_order_id: Optional[str] = None
    open_time:           datetime       = field(default_factory=lambda: datetime.now(timezone.utc))
    trailing_pivot_bars: int            = 3
    structural_buffer:   float          = 50.0
    min_trail_pct:       float          = 0.003
    last_pnl:            float          = 0.0
    realized_pnl:        float          = 0.0   # 累計已實現 PnL（TP1 後更新）

    # Rolling candle window for trailing stop（repr=False 避免日誌污染 BE-M4）
    _candle_buffer: deque = field(
        default_factory=lambda: deque(maxlen=20),
        repr=False,
    )

    # ── State Checks ──────────────────────────────────────────────────────────

    def is_hard_sl_triggered(self, current_price: float) -> bool:
        if self.side == "BUY":
            return current_price <= self.hard_sl_price
        return current_price >= self.hard_sl_price

    def is_invalidated(self, close_price: float) -> bool:
        if self.side == "BUY":
            return close_price <= self.invalidation_level
        return close_price >= self.invalidation_level

    def is_trailing_sl_triggered(self, price: float) -> bool:
        """
        AT-M3 FIX: 支援 tick-level 和 candle-close 兩種觸發來源；
        只在 TRAILING 狀態有效。
        """
        if self.state != PositionState.TRAILING:
            return False
        if self.side == "BUY":
            return price <= self.stop_loss
        return price >= self.stop_loss

    def is_tp1_hit(self, price: float) -> bool:
        return price >= self.take_profit_1 if self.side == "BUY" else price <= self.take_profit_1

    def is_tp2_hit(self, price: float) -> bool:
        return price >= self.take_profit_2 if self.side == "BUY" else price <= self.take_profit_2

    # ── Trailing Stop ─────────────────────────────────────────────────────────

    def update_structural_trailing(self, candle: dict):
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
        return (self.entry_price - current_price) * self.qty

    def hold_duration_str(self) -> str:
        """持倉時間格式化字串"""
        elapsed = (datetime.now(timezone.utc) - self.open_time).total_seconds()
        h, rem  = divmod(int(elapsed), 3600)
        m       = rem // 60
        return f"{h}h {m}m" if h else f"{m}m"


# ─────────────────────────────────────────────────────────────────────────────

class PositionManager:
    def __init__(self, on_close, db, tg, smc, circuit=None):
        self.positions:  dict = {}
        self._on_close       = on_close
        self.db              = db
        self.tg              = tg
        self.smc             = smc
        self.circuit         = circuit
        self.executor        = None      # set by TradingSystem after init

        rules = get_position_rules()

        self._tp_enabled     = rules.get("partial_tp", {}).get("enabled", True)
        self._tp1_close_pct  = rules.get("partial_tp", {}).get("tp1_close_pct", 0.5)
        self._trailing_pivot = rules.get("trailing_stop", {}).get("trailing_pivot_bars", 3)
        self._struct_buffer  = rules.get("trailing_stop", {}).get("structural_buffer_usd", 50)
        self._min_trail_pct  = rules.get("trailing_stop", {}).get("min_trail_pct", 0.003)
        self._max_hold_hours = rules.get("max_hold", {}).get("hours", 72)
        # AT-M2 FIX: 讀取 force_close_condition 設定
        self._force_close_cond = rules.get("max_hold", {}).get(
            "force_close_condition", "always"
        )

    # ── Open Position ─────────────────────────────────────────────────────────

    async def open(self, symbol: str, signal, order, analysis_id=None) -> Position:
        fill_price = float(
            getattr(order, "filled_avg_price", signal.entry_limit_price)
            or signal.entry_limit_price
        )
        qty = float(getattr(order, "filled_qty", 0) or 0)

        if qty <= 0:
            raise ValueError(
                f"open(): filled_qty={qty} — order may not have filled. "
                f"order_id={getattr(order, 'id', 'N/A')} status={getattr(order, 'status', 'N/A')}"
            )

        notional = fill_price * qty

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
            analysis_id  = analysis_id or 0,
            symbol       = symbol,
            side         = signal.direction,
            notional     = notional,
            fill_price   = fill_price,
            limit_price  = signal.entry_limit_price,
            stop_loss    = signal.stop_loss,
            hard_sl_price= signal.hard_sl_price,
            take_profit  = signal.take_profit_1,
        )
        pos.trade_id           = trade_id
        self.positions[symbol] = pos
        return pos

    # ── 共用出場執行邏輯 (BE-H6) ─────────────────────────────────────────────

    async def _execute_close(
        self,
        pos:            Position,
        reason:         str,
        fill_fallback:  float,
        extra_factory:  Callable[[float], str] = None,
        rollback_state: PositionState = PositionState.OPEN,
    ) -> bool:
        """
        BE-H6 FIX: 統一出場邏輯
        ─────────────────────────
        1. executor.close_position  → 取得成交價
        2. 計算 last_pnl
        3. circuit.record(reason)
        4. db.close_position
        5. tg.notify_close
        6. 清除本地持倉
        7. 呼叫 _on_close callback（通知 main 記錄 risk PnL）

        失敗時回滾 pos.state 並告警，回傳 False 讓 tick/candle handler 可重試。
        """
        if not self.executor:
            return False
        try:
            order = await self.executor.close_position(pos)
            fill  = float(
                getattr(order, "filled_avg_price", fill_fallback) or fill_fallback
            )
            pos.last_pnl = (
                (fill - pos.entry_price) * pos.qty
                if pos.side == "BUY"
                else (pos.entry_price - fill) * pos.qty
            )

            if self.circuit:
                await self.circuit.record(reason)  # R1 FIX: now async

            try:
                await self.db.close_position(pos, reason, order)
            except Exception as db_err:
                await self.tg.alert(
                    f"⚠️ {reason} DB 寫入失敗：{db_err}", level="WARNING"
                )

            extra = extra_factory(fill) if callable(extra_factory) else (extra_factory or "")
            await self.tg.notify_close(pos, reason, extra=extra)
            self.positions.pop(pos.symbol, None)
            await self._on_close(pos, reason)
            return True

        except Exception as e:
            pos.state = rollback_state
            await self.tg.alert(f"🔴 {reason} 平倉失敗：{e}", level="CRITICAL")
            return False

    # ── Tick Handler (Fast Track) ─────────────────────────────────────────────

    async def on_tick(self, symbol: str, price: float):
        pos = self.positions.get(symbol)
        if not pos or pos.state not in (PositionState.OPEN, PositionState.TRAILING):
            return

        # R7 FIX: 持續更新浮動 PnL（供 heartbeat / /status 顯示）
        pos.last_pnl = pos.calc_float_pnl(price)

        # TP1 check（只在 OPEN 態）
        if self._tp_enabled and pos.state == PositionState.OPEN and pos.is_tp1_hit(price):
            pos.state = PositionState.TRAILING
            self._create_task(self._execute_tp1(pos, price))
            return

        # TP2 check（TRAILING 態）
        if pos.state == PositionState.TRAILING and pos.is_tp2_hit(price):
            pos.state = PositionState.CLOSING
            self._create_task(
                self._execute_close(
                    pos, "TP2", price,
                    extra_factory=lambda fill: (
                        f"TP2 全倉出場：${fill:,.0f}，"
                        f"獲利 +${pos.last_pnl:,.0f}　持倉 {pos.hold_duration_str()}"
                    ),
                    rollback_state=PositionState.TRAILING,
                )
            )
            return

        # AT-M3 FIX: Trailing SL 也加入 tick-level 觸發
        if pos.state == PositionState.TRAILING and pos.is_trailing_sl_triggered(price):
            pos.state = PositionState.CLOSING
            self._create_task(
                self._execute_close(
                    pos, "TRAILING_SL", price,
                    rollback_state=PositionState.TRAILING,
                )
            )
            return

        # Hard SL：盤中觸發立即出場
        if pos.is_hard_sl_triggered(price):
            pos.state = PositionState.CLOSING
            trigger   = price
            self._create_task(
                self._execute_close(
                    pos, "HARD_SL", trigger,
                    extra_factory=lambda fill: (
                        f"插針觸發價：${trigger:,.0f}\n"
                        f"出場成交：${fill:,.0f}（市價）\n"
                        f"虧損：−${abs(pos.last_pnl):,.0f}　持倉 {pos.hold_duration_str()}"
                    ),
                    rollback_state=PositionState.OPEN,
                )
            )

    async def _execute_tp1(self, pos: Position, price: float):
        """TP1 部分平倉；AT-C2 FIX: 之後更新 server stop qty"""
        if not self.executor:
            return
        try:
            close_qty = round(pos.qty * self._tp1_close_pct, 8)
            if pos.side == "BUY":
                realized = (price - pos.entry_price) * close_qty
            else:
                realized = (pos.entry_price - price) * close_qty

            await self.executor.partial_close(pos.symbol, pos.side, close_qty)

            pos.qty          -= close_qty
            pos.realized_pnl += realized
            pos.last_pnl      = realized
            pos.stop_loss     = pos.entry_price  # Move SL to break-even

            # AT-C2 FIX: TP1 後更新 server stop
            # 原始 server stop 是全倉 qty，需改為剩餘 qty，
            # 且 hard_sl 跟著新的 stop_loss（= entry）重算
            buf = 0.003
            new_hard_sl = round(
                pos.entry_price * (1 - buf) if pos.side == "BUY" else pos.entry_price * (1 + buf),
                2,
            )
            pos.hard_sl_price = new_hard_sl

            if pos.server_stop_order_id:
                try:
                    self.executor.client.cancel_order_by_id(pos.server_stop_order_id)
                except Exception:
                    pass  # 可能已觸發或取消，忽略
                pos.server_stop_order_id = None

            new_stop_id = await self.executor.place_server_side_stop(
                side          = pos.side,
                qty           = pos.qty,        # 剩餘數量（≈50%）
                hard_sl_price = new_hard_sl,    # 成本線附近的保底停損
            )
            if new_stop_id:
                pos.server_stop_order_id = new_stop_id

            self.tg.mark_state_changed("SL_MOVED")
            self._create_task(self.tg.notify_tp1(pos.symbol, pos, realized))

        except Exception as e:
            pos.state = PositionState.OPEN  # 回滾，下次 tick 重試
            await self.tg.alert(f"⚠️ TP1 減倉失敗：{e}", level="WARNING")

    # ── Candle Close Handler ──────────────────────────────────────────────────

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
            self._create_task(
                self._execute_close(
                    pos, "INVALIDATED", candle["close"],
                    extra_factory=lambda fill: f"結構收盤失效　持倉 {pos.hold_duration_str()}",
                    rollback_state=PositionState.OPEN,
                )
            )
            return

        # Trailing stop（收盤確認，M1 收盤）
        if pos.is_trailing_sl_triggered(candle["close"]):
            pos.state = PositionState.CLOSING
            self._create_task(
                self._execute_close(
                    pos, "TRAILING_SL", candle["close"],
                    extra_factory=lambda fill: (
                        f"Trailing SL 收盤確認　持倉 {pos.hold_duration_str()}\n"
                        f"出場：${fill:,.0f}　損益：${pos.last_pnl:+,.0f}"
                    ),
                    rollback_state=PositionState.TRAILING,
                )
            )
            return

        # Trailing stop level 更新
        if pos.state == PositionState.TRAILING:
            old_sl = pos.stop_loss
            pos.update_structural_trailing(candle)
            if pos.stop_loss != old_sl:
                self.tg.mark_state_changed("SL_MOVED")

        # AT-M2 FIX: Max hold — 依 force_close_condition 決定是否強制出場
        elapsed_h = (datetime.now(timezone.utc) - pos.open_time).total_seconds() / 3600
        if elapsed_h >= self._max_hold_hours:
            should_close = True
            if self._force_close_cond == "overtime_and_losing":
                # 只在虧損時強制出場
                float_pnl = pos.calc_float_pnl(candle["close"])
                should_close = float_pnl < 0
            if should_close:
                pos.state = PositionState.CLOSING
                # R9 FIX: 使用 MAX_HOLD（中性）而非 INVALIDATED（虧損）
                self._create_task(
                    self._execute_close(
                        pos, "MAX_HOLD", candle["close"],
                        extra_factory=lambda fill: (
                            f"⏰ 超時 {elapsed_h:.0f}h 強制出場（持倉 {pos.hold_duration_str()}）"
                        ),
                        rollback_state=PositionState.OPEN,
                    )
                )

    async def _handle_htf_bias_flip(self, pos: Position, symbol: str):
        new_bias = self.smc.get_htf_bias(symbol)
        if (pos.side == "BUY" and new_bias == "BEARISH") or \
           (pos.side == "SELL" and new_bias == "BULLISH"):
            await self.tg.alert(
                f"⚠️ HTF 偏向翻轉為 {new_bias}，持倉方向衝突！已提前出場",
                level="WARNING",
            )
            pos.state = PositionState.CLOSING
            self._create_task(
                self._execute_close(
                    pos, "HTF_FLIP", pos.entry_price,
                    extra_factory=lambda fill: (
                        f"HTF 翻轉出場　持倉 {pos.hold_duration_str()}\n"
                        f"出場：${fill:,.0f}　損益：${pos.last_pnl:+,.0f}"
                    ),
                    rollback_state=PositionState.OPEN,
                )
            )
        else:
            await self.tg.alert(
                f"ℹ️ HTF 偏向翻轉為 {new_bias}，持倉方向一致，繼續持有",
                level="INFO",
            )

    # ── BE-C3 FIX: Task Management ────────────────────────────────────────────

    def _set_task_registry(self, registry: set):
        """由 TradingSystem 注入背景 task 集合"""
        self._tasks = registry

    def _create_task(self, coro):
        """建立 task 並保留引用，防止 GC 回收、靜默丟棄異常"""
        registry = getattr(self, "_tasks", None)
        task = asyncio.create_task(coro)
        if registry is not None:
            registry.add(task)
            task.add_done_callback(registry.discard)
        return task

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
                    analysis_id   = 0,
                    symbol        = symbol,
                    side          = side,
                    notional      = qty * avg_entry,
                    fill_price    = avg_entry,
                    limit_price   = avg_entry,
                    stop_loss     = pos.stop_loss,
                    hard_sl_price = pos.hard_sl_price,
                    take_profit   = pos.take_profit_1,
                )
                pos.trade_id = trade_id
            except Exception as db_err:
                await self.tg.alert(
                    f"⚠️ adopt_orphan DB 寫入失敗：{db_err}", level="WARNING"
                )

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
                    close_price     = 0.0,
                    pnl_usd         = 0.0,
                    close_reason    = "SERVER_STOP_TRIGGERED",
                    broker_order_id = None,
                )
            except Exception as e:
                await self.tg.alert(
                    f"⚠️ force_close_missing DB 更新失敗：{e}", level="WARNING"
                )
        await self._on_close(pos, "SERVER_STOP_TRIGGERED")
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
                    level="WARNING",
                )
                return

            if symbol in self.positions:
                return

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
                await self.tg.alert(
                    f"⚠️ reconcile_single DB 寫入失敗：{db_err}", level="WARNING"
                )

            self.positions[symbol] = pos
            await self.tg.alert(
                f"⚠️ Ghost 訂單 {order.id} 已接管：{symbol} {side} "
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
