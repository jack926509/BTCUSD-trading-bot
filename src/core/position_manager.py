import asyncio
import os
import time as _time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Optional

from src.config.loader import get_position_rules, get_risk_config


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
    realized_pnl:        float          = 0.0

    # X2 多段出場：list of dicts [{"fraction": 0.3, "price": 105000, "filled": False}]
    tp_tiers:            list           = field(default_factory=list)

    # X4 Hard SL 延遲確認
    hard_sl_first_hit_at: Optional[float] = None

    # X5 INVALIDATED 連續 N 根確認計數
    inval_streak:        int            = 0

    # T3 MFE / MAE 追蹤（以浮動 PnL 計，方便轉成 R 倍數）
    max_favorable_pnl:   float          = 0.0
    max_adverse_pnl:     float          = 0.0

    # T2 Proximity alert 去重 flags + P2-1 開倉靜默起始 monotonic
    _proximity_alerted:  set            = field(default_factory=set, repr=False)
    _proximity_silent_until: float      = 0.0   # monotonic 時間點之前不推 proximity

    # Rolling candle window for trailing stop
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

    # Hard SL buffer（由 PositionManager 依 risk_config 注入）
    hard_sl_buffer_pct: float = 0.003

    def update_structural_trailing(self, candle: dict) -> bool:
        """回傳 True 若 stop_loss 有變動（供上層決定要不要更新 server stop）"""
        swing = self._detect_swing(candle)
        if not swing:
            return False
        buf = self.hard_sl_buffer_pct
        if self.side == "BUY":
            candidate = swing["low"] - self.structural_buffer
            if (candidate > self.stop_loss and
                    (candidate - self.stop_loss) >= self.entry_price * self.min_trail_pct):
                self.stop_loss     = candidate
                self.hard_sl_price = round(candidate * (1 - buf), 2)
                return True
        elif self.side == "SELL":
            candidate = swing["high"] + self.structural_buffer
            if (candidate < self.stop_loss and
                    (self.stop_loss - candidate) >= self.entry_price * self.min_trail_pct):
                self.stop_loss     = candidate
                self.hard_sl_price = round(candidate * (1 + buf), 2)
                return True
        return False

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
        risk_cfg = get_risk_config()

        ptp_cfg = rules.get("partial_tp", {})
        self._tp_enabled     = ptp_cfg.get("enabled", True)
        self._tp_tiers_cfg   = ptp_cfg.get("tiers") or [
            {"fraction": ptp_cfg.get("tp1_close_pct", 0.5), "target": "tp1"}
        ]
        self._be_plus_pct    = ptp_cfg.get("be_plus_pct", 0.0005)

        ts_cfg = rules.get("trailing_stop", {})
        # X6 Adaptive trailing
        self._trailing_pivot_pre  = ts_cfg.get("trailing_pivot_bars_pre_tp1",
                                       ts_cfg.get("trailing_pivot_bars", 5))
        self._trailing_pivot_post = ts_cfg.get("trailing_pivot_bars_post_tp1",
                                       ts_cfg.get("trailing_pivot_bars", 2))
        self._trailing_pivot = self._trailing_pivot_pre   # 預設用 pre_tp1
        self._struct_buffer  = ts_cfg.get("structural_buffer_usd", 50)
        self._min_trail_pct  = ts_cfg.get("min_trail_pct", 0.003)

        # X5 INVALIDATED 連續確認
        inval_cfg = rules.get("invalidation", {})
        self._inval_confirm_bars = inval_cfg.get("confirm_consecutive_bars", 1)

        # X4 Hard SL 延遲確認
        hsl_cfg = rules.get("hard_sl", {})
        self._hard_sl_confirm_delay = hsl_cfg.get("confirm_delay_sec", 0)

        self._max_hold_hours = rules.get("max_hold", {}).get("hours", 72)
        self._force_close_cond = rules.get("max_hold", {}).get(
            "force_close_condition", "always"
        )
        # Hard SL / Server-stop buffer 改由 risk_config 統一控制
        self._hard_sl_buffer     = risk_cfg.get("hard_sl", {}).get("buffer_pct", 0.003)
        self._server_stop_buffer = risk_cfg.get("server_side_stop", {}).get("buffer_pct", 0.005)

        # T6 Entry risk tracking：當日交易編號
        self._daily_trade_count = 0
        self._daily_trade_date  = None

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

        # P0-1: 若 TP 非結構型（2R / 3R fallback），以真實 fill_price 重算
        # 避免 entry_limit_price 與 fill_price 偏差導致 R 倍數失真
        use_structural_tp = getattr(signal, "tp_is_structural", False)
        if use_structural_tp:
            tp1_real = signal.take_profit_1
            tp2_real = signal.take_profit_2
        else:
            r = abs(fill_price - signal.stop_loss)
            if signal.direction == "BUY":
                tp1_real = round(fill_price + r * 2, 2)
                tp2_real = round(fill_price + r * 3, 2)
            else:
                tp1_real = round(fill_price - r * 2, 2)
                tp2_real = round(fill_price - r * 3, 2)

        tp_tiers = []
        for tier in self._tp_tiers_cfg:
            target = tier.get("target", "tp1")
            price  = tp1_real if target == "tp1" else tp2_real
            tp_tiers.append({
                "fraction": tier.get("fraction", 0.5),
                "price":    price,
                "filled":   False,
                "target":   target,
            })

        pos = Position(
            symbol              = symbol,
            side                = signal.direction,
            entry_price         = fill_price,
            qty                 = qty,
            notional            = notional,
            stop_loss           = signal.stop_loss,
            hard_sl_price       = signal.hard_sl_price,
            take_profit_1       = tp1_real,
            take_profit_2       = tp2_real,
            invalidation_level  = signal.invalidation_level,
            analysis_id         = analysis_id,
            trailing_pivot_bars = self._trailing_pivot_pre,
            structural_buffer   = self._struct_buffer,
            min_trail_pct       = self._min_trail_pct,
            hard_sl_buffer_pct  = self._hard_sl_buffer,
            tp_tiers            = tp_tiers,
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
        # P2-1: 開倉後 90 秒 proximity alert 靜默，避免進場即爆告警
        pos._proximity_silent_until = _time.monotonic() + 90
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
                # 以 pnl 判定勝負而非 reason 字串
                await self.circuit.record(reason, pnl=pos.last_pnl)

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

        # 浮動 PnL / MFE / MAE（T3）
        pos.last_pnl = pos.calc_float_pnl(price)
        if pos.last_pnl > pos.max_favorable_pnl:
            pos.max_favorable_pnl = pos.last_pnl
        if pos.last_pnl < pos.max_adverse_pnl:
            pos.max_adverse_pnl = pos.last_pnl

        # T2 Proximity alert（距離 TP/SL 0.2% 內推一次）
        # P2-2: 不 await 在 tick 主路徑上；fire-and-forget 避免 TG 退避卡 tick
        self._create_task(self._check_proximity_alert(pos, price))

        # X2 多段出場：依序檢查未 filled 的 tier
        if self._tp_enabled and pos.state in (PositionState.OPEN, PositionState.TRAILING):
            for idx, tier in enumerate(pos.tp_tiers):
                if tier["filled"]:
                    continue
                hit = (price >= tier["price"]) if pos.side == "BUY" else (price <= tier["price"])
                if hit:
                    # 第一段命中 → 進 TRAILING；最後一段命中 → 全倉 TP2 出場
                    if idx == len(pos.tp_tiers) - 1:
                        pos.state = PositionState.CLOSING
                        self._create_task(self._execute_final_tp(pos, price, tier))
                    else:
                        pos.state = PositionState.TRAILING
                        self._create_task(self._execute_partial_tp(pos, price, tier, idx))
                    return

        # X4 Hard SL 延遲確認
        if pos.is_hard_sl_triggered(price):
            now = _time.monotonic()
            if self._hard_sl_confirm_delay > 0:
                if pos.hard_sl_first_hit_at is None:
                    pos.hard_sl_first_hit_at = now
                    return  # 等下一個 tick 再判斷
                if now - pos.hard_sl_first_hit_at < self._hard_sl_confirm_delay:
                    return  # 延遲期間不動作
            # 超過延遲期仍觸發 → 出場
            pos.state = PositionState.CLOSING
            trigger = price
            self._create_task(
                self._execute_close(
                    pos, "HARD_SL", trigger,
                    extra_factory=lambda fill: (
                        f"插針觸發價：${trigger:,.0f}（延遲 {self._hard_sl_confirm_delay}s 確認）\n"
                        f"出場成交：${fill:,.0f}\n"
                        f"虧損：−${abs(pos.last_pnl):,.0f}　持倉 {pos.hold_duration_str()}"
                    ),
                    rollback_state=PositionState.OPEN,
                )
            )
            return
        else:
            # 價格回復 → 清除 hard SL first_hit flag
            pos.hard_sl_first_hit_at = None

        # Trailing SL tick 觸發
        if pos.state == PositionState.TRAILING and pos.is_trailing_sl_triggered(price):
            pos.state = PositionState.CLOSING
            self._create_task(
                self._execute_close(
                    pos, "TRAILING_SL", price,
                    rollback_state=PositionState.TRAILING,
                )
            )

    async def _check_proximity_alert(self, pos, price: float):
        """T2 + P2-1：接近 TP/SL 0.2% 內推一次；開倉 90 秒內靜默"""
        if _time.monotonic() < pos._proximity_silent_until:
            return
        threshold = 0.002
        checks = [
            ("TP1", pos.take_profit_1),
            ("TP2", pos.take_profit_2),
            ("SL",  pos.stop_loss),
        ]
        for tag, target in checks:
            if not target or tag in pos._proximity_alerted:
                continue
            dist = abs(price - target) / target if target else 1
            if dist <= threshold:
                pos._proximity_alerted.add(tag)
                side = "距" if tag != "SL" else "離"
                icon = "🎯" if tag != "SL" else "⚠️"
                diff_usd = abs(price - target)
                try:
                    await self.tg.alert(
                        f"{icon} {pos.symbol} {side} {tag} ${target:,.0f}"
                        f" 只剩 ${diff_usd:,.0f}（{dist*100:.2f}%）",
                        level="INFO",
                    )
                except Exception:
                    pass

    async def _execute_partial_tp(self, pos: Position, price: float, tier: dict, idx: int):
        """
        X2 單段部分出場：平 tier.fraction 的倉位；
        若是第一段，執行 BE+（X3）並把 trailing 改 tighter（X6）。
        """
        if not self.executor:
            return
        try:
            # 以 *原始* qty × fraction 計算要平掉多少
            base_qty  = pos.qty / max(1 - sum(t["fraction"] for t in pos.tp_tiers[:idx]), 0.01)
            close_qty = round(base_qty * tier["fraction"], 8)
            close_qty = min(close_qty, pos.qty)  # 避免超平
            if pos.side == "BUY":
                realized = (price - pos.entry_price) * close_qty
            else:
                realized = (pos.entry_price - price) * close_qty

            await self.executor.partial_close(pos.symbol, pos.side, close_qty)

            pos.qty          -= close_qty
            pos.notional      = round(pos.entry_price * pos.qty, 2)
            pos.realized_pnl += realized
            pos.last_pnl      = realized
            tier["filled"]    = True

            # X3 BE+：第一段命中後把 SL 推到 entry × (1 ± be_plus_pct)
            if idx == 0:
                be_pct = self._be_plus_pct
                pos.stop_loss = round(
                    pos.entry_price * (1 + be_pct) if pos.side == "BUY"
                    else pos.entry_price * (1 - be_pct),
                    2,
                )
                # X6 Adaptive：TP1 後改緊
                pos.trailing_pivot_bars = self._trailing_pivot_post
                pos._candle_buffer.clear()  # 重新累積 pivot window

            # Hard SL 跟著新 SL
            buf = self._hard_sl_buffer
            pos.hard_sl_price = round(
                pos.stop_loss * (1 - buf) if pos.side == "BUY" else pos.stop_loss * (1 + buf),
                2,
            )

            # Replace server stop
            new_stop_id = await self.executor.replace_server_side_stop(
                old_order_id  = pos.server_stop_order_id,
                side          = pos.side,
                qty           = pos.qty,
                hard_sl_price = pos.hard_sl_price,
                buffer_pct    = self._server_stop_buffer,
            )
            pos.server_stop_order_id = new_stop_id

            # DB 審計
            if pos.trade_id:
                try:
                    await self.db.update_trade_stop_loss(
                        pos.trade_id, pos.stop_loss, pos.hard_sl_price, new_stop_id,
                    )
                except Exception:
                    pass

            self.tg.mark_state_changed("SL_MOVED")
            self._create_task(self.tg.notify_tp1(pos.symbol, pos, realized))
        except Exception as e:
            pos.state = PositionState.OPEN
            await self.tg.alert(f"TP{idx+1} 減倉失敗：{e}", level="WARNING")

    async def _execute_final_tp(self, pos: Position, price: float, tier: dict):
        """最後一段 tier 命中 → 全倉出場，reason=TP2"""
        await self._execute_close(
            pos, "TP2", price,
            extra_factory=lambda fill: (
                f"TP2 全倉出場：${fill:,.0f}，"
                f"獲利 +${pos.last_pnl:,.0f}　持倉 {pos.hold_duration_str()}"
            ),
            rollback_state=PositionState.TRAILING,
        )

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

        # INVALIDATED check（連續 N 根收盤確認，減少 M1 噪音誤判）
        if pos.is_invalidated(candle["close"]):
            pos.inval_streak += 1
            if pos.inval_streak >= self._inval_confirm_bars:
                pos.state = PositionState.CLOSING
                self._create_task(
                    self._execute_close(
                        pos, "INVALIDATED", candle["close"],
                        extra_factory=lambda fill: (
                            f"結構收盤失效（連 {pos.inval_streak} 根確認）　"
                            f"持倉 {pos.hold_duration_str()}"
                        ),
                        rollback_state=PositionState.OPEN,
                    )
                )
                return
            # 未達確認門檻，繼續觀察
            await self.tg.alert(
                f"{pos.symbol} 首次 INVALIDATED 收盤觀察（{pos.inval_streak}/{self._inval_confirm_bars}）",
                level="INFO",
            )
        else:
            pos.inval_streak = 0  # 一根未破就重置計數

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

        # Trailing stop level 更新 + 同步 server stop
        if pos.state == PositionState.TRAILING:
            if pos.update_structural_trailing(candle):
                self.tg.mark_state_changed("SL_MOVED")
                if self.executor:
                    try:
                        new_stop_id = await self.executor.replace_server_side_stop(
                            old_order_id  = pos.server_stop_order_id,
                            side          = pos.side,
                            qty           = pos.qty,
                            hard_sl_price = pos.hard_sl_price,
                            buffer_pct    = self._server_stop_buffer,
                        )
                        pos.server_stop_order_id = new_stop_id
                        if pos.trade_id:
                            await self.db.update_trade_stop_loss(
                                pos.trade_id, pos.stop_loss,
                                pos.hard_sl_price, new_stop_id,
                            )
                    except Exception as e:
                        await self.tg.alert(
                            f"Trailing SL 同步 server stop 失敗：{e}",
                            level="WARNING",
                        )

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
        conflict = (pos.side == "BUY" and new_bias == "BEARISH") or \
                   (pos.side == "SELL" and new_bias == "BULLISH")

        if not conflict:
            await self.tg.alert(
                f"ℹ️ HTF 偏向翻轉為 {new_bias}，持倉方向一致，繼續持有",
                level="INFO",
            )
            return

        # P1-8: TRAILING 態（已過 TP1，TP1 獲利已鎖）→ 縮緊 trailing pivot 而非強平
        # 理由：HTF flip 當下通常已接近頭部，強平等於捐出 runner 收益
        if pos.state == PositionState.TRAILING:
            pos.trailing_pivot_bars = 1
            pos._candle_buffer.clear()
            self.tg.mark_state_changed("SL_MOVED")
            await self.tg.alert(
                f"⚠️ HTF 偏向翻轉 {new_bias}，Runner 保留但 trailing 縮緊為 1 pivot\n"
                f"（TP1 已鎖利 +${pos.realized_pnl:,.0f}，讓 trailing 自然處理）",
                level="WARNING",
            )
            return

        # OPEN 態（未達 TP1，尚未鎖利）→ 立即出場
        await self.tg.alert(
            f"⚠️ HTF 偏向翻轉為 {new_bias}，持倉方向衝突且未達 TP1，提前出場",
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
        """
        接管 Alpaca 持倉並寫入 DB。
        優先從 trade_log 還原原始 SL/TP；若無紀錄才退回緊急預設值並發 CRITICAL。
        """
        try:
            qty       = float(pos_data.qty)
            avg_entry = float(pos_data.avg_entry_price)
            side      = "BUY" if pos_data.side.value == "long" else "SELL"

            pos, trade_id = await self._build_adopted_position(
                symbol, side, avg_entry, qty, broker_source="alpaca_positions",
            )
            if trade_id:
                pos.trade_id = trade_id
            self.positions[symbol] = pos
        except Exception as e:
            await self.tg.alert(f"adopt_orphan 失敗：{e}", level="WARNING")

    async def _build_adopted_position(
        self, symbol: str, side: str, avg_entry: float, qty: float,
        broker_source: str,
    ):
        """還原或建立接管 Position。有 trade_log 紀錄時用原始 SL/TP，否則緊急預設。"""
        recovered = None
        try:
            recovered = await self.db.find_recent_open_trade(symbol)
        except Exception:
            pass

        if recovered:
            pos = Position(
                symbol              = symbol,
                side                = side,
                entry_price         = avg_entry,
                qty                 = qty,
                notional             = qty * avg_entry,
                stop_loss            = recovered.get("stop_loss") or avg_entry * (0.98 if side == "BUY" else 1.02),
                hard_sl_price        = recovered.get("hard_sl_price") or avg_entry * (0.977 if side == "BUY" else 1.023),
                take_profit_1        = recovered.get("take_profit") or avg_entry * (1.04 if side == "BUY" else 0.96),
                take_profit_2        = avg_entry * (1.06 if side == "BUY" else 0.94),
                invalidation_level   = avg_entry * (0.975 if side == "BUY" else 1.025),
                trailing_pivot_bars  = self._trailing_pivot,
                structural_buffer    = self._struct_buffer,
                min_trail_pct        = self._min_trail_pct,
                hard_sl_buffer_pct   = self._hard_sl_buffer,
                server_stop_order_id = recovered.get("server_stop_order_id"),
            )
            await self.tg.alert(
                f"Reconcile：{symbol} {side} 從 trade_log 還原 SL=${pos.stop_loss:,.0f} "
                f"TP1=${pos.take_profit_1:,.0f}",
                level="WARNING",
            )
            return pos, recovered.get("id")

        # 無 trade_log 紀錄 → 緊急預設值 + CRITICAL 告警
        await self.tg.alert(
            f"Reconcile 警告：{symbol} {side} 找不到 trade_log 記錄，"
            f"套用緊急預設 SL (avg ±2%)，請儘速手動確認！（來源：{broker_source}）",
            level="CRITICAL",
        )
        pos = Position(
            symbol              = symbol,
            side                = side,
            entry_price         = avg_entry,
            qty                 = qty,
            notional            = qty * avg_entry,
            stop_loss           = avg_entry * (0.98  if side == "BUY" else 1.02),
            hard_sl_price       = avg_entry * (0.977 if side == "BUY" else 1.023),
            take_profit_1       = avg_entry * (1.04  if side == "BUY" else 0.96),
            take_profit_2       = avg_entry * (1.06  if side == "BUY" else 0.94),
            invalidation_level  = avg_entry * (0.975 if side == "BUY" else 1.025),
            trailing_pivot_bars = self._trailing_pivot,
            structural_buffer   = self._struct_buffer,
            min_trail_pct       = self._min_trail_pct,
            hard_sl_buffer_pct  = self._hard_sl_buffer,
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
            return pos, trade_id
        except Exception as db_err:
            await self.tg.alert(
                f"adopt DB 寫入失敗：{db_err}", level="WARNING"
            )
            return pos, None

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
        """Ghost 訂單確認成交後，建立本地追蹤持倉。"""
        try:
            symbol     = order.symbol
            side_val   = str(order.side.value).lower()
            side       = "BUY" if side_val == "buy" else "SELL"
            fill_price = float(order.filled_avg_price or 0)
            qty        = float(order.filled_qty or 0)

            if not fill_price or not qty:
                await self.tg.alert(
                    f"reconcile_single：訂單 {order.id} 無有效成交資料，略過",
                    level="WARNING",
                )
                return

            if symbol in self.positions:
                return

            pos, trade_id = await self._build_adopted_position(
                symbol, side, fill_price, qty, broker_source=f"ghost_order_{order.id}",
            )
            if trade_id:
                pos.trade_id = trade_id

            self.positions[symbol] = pos
            await self.tg.alert(
                f"Ghost 訂單 {order.id} 已接管：{symbol} {side} "
                f"{qty:.6f} @ ${fill_price:,.0f}",
                level="WARNING",
            )
        except Exception as e:
            await self.tg.alert(f"reconcile_single 失敗：{e}", level="WARNING")

    def get_float_pnl(self, symbol: str, price: float) -> float:
        pos = self.positions.get(symbol)
        if not pos:
            return 0.0
        return pos.calc_float_pnl(price)
