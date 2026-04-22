import asyncio
import os
from datetime import datetime, timezone
from telegram import Bot
from telegram.constants import ParseMode


def _pnl(v: float) -> str:
    return f"+${v:,.0f}" if v >= 0 else f"−${abs(v):,.0f}"

def _pct(diff: float, base: float) -> str:
    if not base:
        return ""
    p = abs(diff / base * 100)
    sign = "+" if diff >= 0 else "−"
    return f"{sign}{p:.2f}%"

def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%m/%d %H:%M UTC")


class TelegramNotifier:
    def __init__(self):
        self._token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self._chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        self._bot     = Bot(token=self._token) if self._token else None

        self._last_float_pnl:      float = 0.0
        self._last_breaker_state:  str   = ""
        self._state_changed_flags: set   = set()

    async def _send(self, text: str):
        if not self._bot or not self._chat_id:
            print(f"[TG] {text}")
            return
        try:
            await self._bot.send_message(
                chat_id    = self._chat_id,
                text       = text,
                parse_mode = ParseMode.HTML,
            )
        except Exception as e:
            print(f"[TG] send error: {e}")

    async def alert(self, message: str, level: str = "INFO"):
        icons = {"CRITICAL": "🔴", "WARNING": "⚠️", "INFO": "ℹ️"}
        icon  = icons.get(level, "")
        # 純文字 alert 不用 HTML tag，直接送
        await self._send(f"{icon} {message}")

    # ── Startup ──────────────────────────────────────────────────────────────

    async def notify_startup(self, circuit_state, mode: str = "PAPER"):
        state_val = circuit_state.value if hasattr(circuit_state, "value") else str(circuit_state)
        mode_icon = "🧪" if mode == "PAPER" else "🔴"
        cb_icon   = {"CLOSED": "🟢", "HALF": "🟡", "OPEN": "🔴"}.get(state_val, "⚪")
        text = (
            f"🚀 <b>Trading System v7.0 啟動</b>\n"
            f"{'─' * 22}\n"
            f"{mode_icon} 模式　<b>{mode}</b>\n"
            f"{cb_icon} 熔斷器　{state_val}\n"
            f"📡 WebSocket　連線中…\n"
            f"⏰ {_now_utc()}"
        )
        await self._send(text)

    # ── Trade Open ────────────────────────────────────────────────────────────

    async def notify_trade_open(self, symbol: str, signal, description: str = ""):
        direction = getattr(signal, "direction", "?")
        side_str  = "LONG 🔺" if direction == "BUY" else "SHORT 🔻"
        entry     = getattr(signal, "entry_limit_price", 0) or 0
        sl        = getattr(signal, "stop_loss", 0) or 0
        hard_sl   = getattr(signal, "hard_sl_price", 0) or 0
        tp1       = getattr(signal, "take_profit_1", 0) or 0
        tp2       = getattr(signal, "take_profit_2", 0) or 0
        inval     = getattr(signal, "invalidation_level", 0) or 0
        rrr       = getattr(signal, "rrr", 0) or 0
        htf_bias  = getattr(signal, "htf_bias", "N/A")
        source    = getattr(signal, "source", "N/A")
        ob_level  = getattr(signal, "ob_level", None)
        fvg_range = getattr(signal, "fvg_range", None)

        risk_usd  = abs(entry - sl)
        tp1_usd   = abs(tp1 - entry)
        tp2_usd   = abs(tp2 - entry)

        bias_icon = "🟢" if htf_bias == "BULLISH" else ("🔴" if htf_bias == "BEARISH" else "⚪")
        src_map   = {"BOS": "BOS 突破", "CHOCH": "CHoCH 轉折", "SWEEP": "流動性掃盪"}
        src_label = src_map.get(source, source)

        zone_line = ""
        if ob_level:
            zone_line += f"📦 OB　${ob_level:,.0f}\n"
        if fvg_range:
            zone_line += f"🕳  FVG　{fvg_range}\n"

        text = (
            f"📊 <b>開倉 · {symbol} {side_str}</b>\n"
            f"{'─' * 24}\n"
            f"⏰ {_now_utc()}\n"
            f"{bias_icon} HTF　{htf_bias}　｜　{src_label}\n"
            f"{'─' * 24}\n"
            f"💰 進場限價　<b>${entry:,.0f}</b>\n"
            f"🎯 TP1　${tp1:,.0f}　<i>({_pct(tp1_usd, entry)}  +${tp1_usd:,.0f})</i>\n"
            f"🏁 TP2　${tp2:,.0f}　<i>({_pct(tp2_usd, entry)}  +${tp2_usd:,.0f})</i>\n"
            f"🔻 SL　${sl:,.0f}　<i>({_pct(-risk_usd, entry)}  −${risk_usd:,.0f})</i>\n"
            f"⚡ Hard SL　${hard_sl:,.0f}　<i>盤中觸發</i>\n"
            f"{'─' * 24}\n"
            f"📐 RRR　1 : {rrr:.2f}　｜　風險 ${risk_usd:,.0f}\n"
            f"🔒 失效　收盤穿越 ${inval:,.0f}\n"
            + (f"{'─' * 24}\n{zone_line}" if zone_line else "")
            + (f"<i>{description}</i>" if description else "")
        )
        await self._send(text)

    # ── TP1 ───────────────────────────────────────────────────────────────────

    async def notify_tp1(self, symbol: str, pos, realized_pnl: float):
        side_str = "LONG 🔺" if pos.side == "BUY" else "SHORT 🔻"
        tp2      = getattr(pos, "take_profit_2", 0) or 0
        realized = getattr(pos, "realized_pnl", realized_pnl) or realized_pnl
        text = (
            f"✅ <b>TP1 達成 · {symbol} {side_str}</b>\n"
            f"{'─' * 24}\n"
            f"💵 本次實現　<b>+${realized_pnl:,.0f}</b>\n"
            f"📦 已平倉 50%，剩餘 50% 持倉中\n"
            f"{'─' * 24}\n"
            f"🔒 止損移至　<b>${pos.entry_price:,.0f}</b>（成本，已鎖利）\n"
            f"🏁 追蹤目標　${tp2:,.0f}\n"
            f"📈 Trailing SL 啟動（M1 Swing Pivot 3 根確認）\n"
            f"⏰ {_now_utc()}"
        )
        await self._send(text)

    # ── Close ─────────────────────────────────────────────────────────────────

    async def notify_close(self, pos, reason: str, extra: str = ""):
        side_str = "LONG 🔺" if pos.side == "BUY" else "SHORT 🔻"
        symbol   = getattr(pos, "symbol", "BTC/USD")
        pnl      = getattr(pos, "last_pnl", 0) or 0
        realized = getattr(pos, "realized_pnl", 0) or 0
        total    = pnl + realized

        headers = {
            "HARD_SL":     ("⚡", "Hard SL 出場"),
            "INVALIDATED": ("🔄", "結構失效出場"),
            "TRAILING_SL": ("📉", "Trailing SL 出場"),
            "HTF_FLIP":    ("🔀", "HTF 偏向翻轉出場"),
            "TP2":         ("🏆", "TP2 全倉出場"),
            "SL":          ("🛑", "止損出場"),
        }
        icon, label = headers.get(reason, ("🚪", f"出場 ({reason})"))

        pnl_icon = "🟢" if pnl >= 0 else "🔴"
        tot_icon = "🟢" if total >= 0 else "🔴"

        lines = [
            f"{icon} <b>{label} · {symbol} {side_str}</b>",
            f"{'─' * 24}",
            f"💰 進場　${pos.entry_price:,.0f}",
            f"{pnl_icon} 本次損益　<b>{_pnl(pnl)}</b>",
        ]

        if realized:
            lines.append(f"{tot_icon} 本單合計　{_pnl(total)}  <i>(含 TP1 +${realized:,.0f})</i>")

        if extra:
            lines += [f"{'─' * 24}", extra]

        lines.append(f"⏰ {_now_utc()}")
        await self._send("\n".join(lines))

    # ── Circuit Breaker ───────────────────────────────────────────────────────

    async def notify_circuit_breaker(self, state: str, loss_streak: int):
        icons   = {"OPEN": "🔴", "HALF": "🟡", "CLOSED": "🟢"}
        descs   = {
            "OPEN":   f"連虧 {loss_streak} 次，暫停交易 4 小時",
            "HALF":   "暫停期滿，進入試單模式",
            "CLOSED": "恢復正常交易",
        }
        icon = icons.get(state, "⚪")
        text = (
            f"{icon} <b>熔斷器變更 → {state}</b>\n"
            f"{'─' * 24}\n"
            f"{descs.get(state, '')}\n"
            f"連虧計數：{loss_streak}\n"
            f"⏰ {_now_utc()}"
        )
        await self._send(text)
        self.mark_state_changed("BREAKER")

    # ── Heartbeat ─────────────────────────────────────────────────────────────

    def mark_state_changed(self, flag: str):
        self._state_changed_flags.add(flag)

    def has_meaningful_state_change(self, snapshot: dict) -> bool:
        if self._state_changed_flags:
            return True
        if abs(snapshot.get("float_pnl", 0.0) - self._last_float_pnl) >= 50:
            return True
        if snapshot.get("breaker_state", "") != self._last_breaker_state:
            return True
        return False

    async def notify_heartbeat(self, snapshot: dict):
        self._state_changed_flags.clear()
        float_pnl     = snapshot.get("float_pnl", 0.0)
        breaker_state = snapshot.get("breaker_state", "CLOSED")
        loss_streak   = snapshot.get("loss_streak", 0)
        position_info = snapshot.get("position_info", "無持倉")
        daily_pnl     = snapshot.get("daily_pnl", 0.0)

        self._last_float_pnl     = float_pnl
        self._last_breaker_state = breaker_state

        cb_icon   = {"CLOSED": "🟢", "HALF": "🟡", "OPEN": "🔴"}.get(breaker_state, "⚪")
        has_pos   = position_info != "無持倉"
        pnl_icon  = "🟢" if float_pnl >= 0 else "🔴"
        day_icon  = "🟢" if daily_pnl >= 0 else "🔴"

        pos_block = (
            f"₿ {position_info}\n"
            f"{pnl_icon} 浮動損益　<b>{_pnl(float_pnl)}</b>\n"
        ) if has_pos else "💤 無持倉\n"

        text = (
            f"💓 <b>系統心跳</b>  {_now_utc()}\n"
            f"{'─' * 24}\n"
            f"{pos_block}"
            f"{'─' * 24}\n"
            f"{cb_icon} 熔斷器　{breaker_state}　連虧 {loss_streak}\n"
            f"{day_icon} 今日損益　{_pnl(daily_pnl)}"
        )
        await self._send(text)

    # ── WebSocket Disconnect ──────────────────────────────────────────────────

    async def notify_ws_disconnect(self, attempt: int, delay: int, pos_snapshot: str = ""):
        text = (
            f"🚨 <b>WebSocket 斷線（第 {attempt} 次）</b>\n"
            f"{'─' * 24}\n"
            f"⏳ {delay}s 後重連…\n"
        )
        if pos_snapshot and pos_snapshot != "無持倉":
            text += f"{'─' * 24}\n📍 持倉快照\n{pos_snapshot}\n"
        dashboard = os.getenv("ALPACA_DASHBOARD_URL", "")
        if dashboard:
            text += f"{'─' * 24}\n🔗 {dashboard}"
        await self._send(text)
