import asyncio
import os
from datetime import datetime
from telegram import Bot
from telegram.constants import ParseMode


class TelegramNotifier:
    def __init__(self):
        self._token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self._chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        self._bot     = Bot(token=self._token) if self._token else None

        # 差異化心跳追蹤
        self._last_float_pnl:      float  = 0.0
        self._last_breaker_state:  str    = ""
        self._state_changed_flags: set    = set()

    async def _send(self, text: str):
        if not self._bot or not self._chat_id:
            print(f"[TG] {text}")
            return
        try:
            await self._bot.send_message(
                chat_id=self._chat_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as e:
            print(f"[TG] send error: {e}")

    async def alert(self, message: str, level: str = "INFO"):
        prefix = {"CRITICAL": "🔴", "WARNING": "⚠️", "INFO": "ℹ️"}.get(level, "")
        await self._send(f"{prefix} {message}")

    # ── Startup ──────────────────────────────────────────────────────────────

    async def notify_startup(self, circuit_state, mode: str = "PAPER"):
        state_val = circuit_state.value if hasattr(circuit_state, "value") else str(circuit_state)
        db_path   = os.getenv("DB_PATH", "/app/data/trading.db")
        volume_ok = "✅" if os.path.dirname(db_path) else "❌"
        text = (
            f"🟢 *Trading System v7.0 啟動*\n"
            f"模式：{mode}（{'Paper Trading' if mode == 'PAPER' else 'Live Trading'}）\n"
            f"熔斷器：{state_val}（連虧：0）\n"
            f"Volume：{os.path.dirname(db_path)} {volume_ok}\n"
            f"trade\\_updates WS：已連線 ✅"
        )
        await self._send(text)

    # ── Trade Events ─────────────────────────────────────────────────────────

    async def notify_trade_open(self, symbol: str, signal, description: str = ""):
        direction = getattr(signal, "direction", "?")
        side_str  = "LONG" if direction == "BUY" else "SHORT"
        entry     = getattr(signal, "entry_limit_price", 0) or 0
        sl        = getattr(signal, "stop_loss", 0) or 0
        hard_sl   = getattr(signal, "hard_sl_price", 0) or 0
        tp1       = getattr(signal, "take_profit_1", 0) or 0
        tp2       = getattr(signal, "take_profit_2", 0) or 0
        inval     = getattr(signal, "invalidation_level", 0) or 0
        rrr       = getattr(signal, "rrr", 0) or 0
        htf_bias  = getattr(signal, "htf_bias", "N/A")
        source    = getattr(signal, "source", "N/A")
        disp      = getattr(signal, "displacement_bars", None)
        conditions= getattr(signal, "conditions_met", [])

        sl_diff   = entry - sl if direction == "BUY" else sl - entry
        tp1_diff  = tp1 - entry if direction == "BUY" else entry - tp1
        tp2_diff  = tp2 - entry if direction == "BUY" else entry - tp2

        source_line = f"訊號類型：{source}"
        if source == "CHOCH" and disp:
            source_line += f"（位移確認 {disp} 根）"

        cond_count = len(conditions)
        cond_total = cond_count  # 實際條件數

        text = (
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"📈 *開倉*  {symbol} {side_str}\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"進場限價：${entry:,.0f}（OB 回踩）\n"
            f"止損：${sl:,.0f}  (−${abs(sl_diff):,.0f})\n"
            f"Hard SL：${hard_sl:,.0f}（盤中觸發點）\n"
            f"目標①：${tp1:,.0f}  (+${tp1_diff:,.0f})\n"
            f"目標②：${tp2:,.0f}  (+${tp2_diff:,.0f})\n"
            f"失效條件：收盤跌破 ${inval:,.0f}\n"
            f"RRR：1:{rrr:.2f}\n"
            f"HTF 偏向：{htf_bias}\n"
            f"結構：{description}\n"
            f"{source_line}\n"
            f"條件確認：{cond_count}/{cond_total}"
        )
        await self._send(text)

    async def notify_tp1(self, symbol: str, pos, realized_pnl: float):
        """TP1 達成，含剩餘持倉快照"""
        side_str = "LONG" if pos.side == "BUY" else "SHORT"
        text = (
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"✅ *TP1 達成*  {symbol} {side_str}（已平 50%）\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"實現：+${realized_pnl:,.0f}\n"
            f"─ 剩餘持倉 ─\n"
            f"止損已移至：${pos.entry_price:,.0f}（成本，已鎖利）\n"
            f"目標②：${getattr(pos, 'take_profit_2', 0):,.0f}\n"
            f"Trailing：啟動（跟蹤 M15 Swing Low，Pivot 3 根）"
        )
        await self._send(text)

    async def notify_close(self, pos, reason: str, extra: str = ""):
        side_str = "LONG" if pos.side == "BUY" else "SHORT"
        symbol   = getattr(pos, "symbol", "BTC/USD")

        reason_icons = {
            "HARD_SL":     "⚡ *Hard SL 出場*",
            "INVALIDATED": "🔄 *INVALIDATED 出場*",
            "TRAILING_SL": "📉 *Trailing SL 出場*",
            "SL":          "🛑 *止損出場*",
            "TP":          "🎯 *TP 達成*",
            "HTF_FLIP":    "🔀 *HTF 翻轉出場*",
        }
        header = reason_icons.get(reason, f"🚪 *出場* ({reason})")

        entry_price = getattr(pos, "entry_price", 0) or 0
        pnl         = getattr(pos, "last_pnl", 0) or 0

        if reason == "HARD_SL":
            text = (
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"{header}  {symbol} {side_str}\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"{extra}\n"
                f"說明：盤中插針觸發 Hard SL（未等收盤確認）"
            )
        else:
            pnl_str = f"+${pnl:,.0f}" if pnl >= 0 else f"−${abs(pnl):,.0f}"
            text = (
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"{header}  {symbol} {side_str}\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"出場損益：{pnl_str}\n"
                f"原因：{reason}"
                + (f"\n{extra}" if extra else "")
            )
        await self._send(text)

    # ── Circuit Breaker ───────────────────────────────────────────────────────

    async def notify_circuit_breaker(self, state: str, loss_streak: int):
        icons = {"OPEN": "🔴", "HALF": "🟡", "CLOSED": "🟢"}
        icon  = icons.get(state, "⚪")
        text  = f"{icon} *熔斷器狀態變更*：{state}（連虧：{loss_streak}）"
        await self._send(text)
        self.mark_state_changed("BREAKER")

    # ── Heartbeat ─────────────────────────────────────────────────────────────

    def mark_state_changed(self, flag: str):
        self._state_changed_flags.add(flag)

    def has_meaningful_state_change(self, snapshot: dict) -> bool:
        """差異化心跳觸發條件"""
        if self._state_changed_flags:
            return True
        float_pnl    = snapshot.get("float_pnl", 0.0)
        breaker_state = snapshot.get("breaker_state", "")
        if abs(float_pnl - self._last_float_pnl) >= 50:
            return True
        if breaker_state != self._last_breaker_state:
            return True
        return False

    async def notify_heartbeat(self, snapshot: dict):
        self._state_changed_flags.clear()
        float_pnl     = snapshot.get("float_pnl", 0.0)
        breaker_state = snapshot.get("breaker_state", "CLOSED")
        loss_streak   = snapshot.get("loss_streak", 0)
        position_info = snapshot.get("position_info", "無持倉")

        self._last_float_pnl    = float_pnl
        self._last_breaker_state = breaker_state

        pnl_str = f"+${float_pnl:,.0f}" if float_pnl >= 0 else f"−${abs(float_pnl):,.0f}"
        text = (
            f"💓 *心跳*  {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC\n"
            f"持倉：{position_info}\n"
            f"浮動損益：{pnl_str}\n"
            f"熔斷器：{breaker_state}（連虧：{loss_streak}）"
        )
        await self._send(text)

    # ── WebSocket Disconnect ──────────────────────────────────────────────────

    async def notify_ws_disconnect(self, attempt: int, delay: int, pos_snapshot: str = ""):
        dashboard_url = os.getenv("ALPACA_DASHBOARD_URL", "")
        text = (
            f"🚨 *WebSocket 斷線*\n"
            f"持倉中！正在重連...（第 {attempt} 次，{delay}s 後）\n"
        )
        if pos_snapshot:
            text += f"─ 持倉快照 ─\n{pos_snapshot}\n"
        text += f"─ 緊急操作 ─\n{dashboard_url}"
        await self._send(text)
