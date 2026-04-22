"""
TelegramNotifier v7.1
──────────────────────
TG-C1 FIX: 雙向命令介面 (/status /pause /resume /close /pnl /signals)
TG-H3 FIX: 訊息 rate limiting + RetryAfter 退避重試
TG-M2 FIX: heartbeat 加入 TradingView 連結
TG-M3 FIX: 出場通知加入持倉時間
"""
import asyncio
import html
import os
from datetime import datetime, timezone
from typing import Optional

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TimedOut, NetworkError
from telegram.ext import Application, CommandHandler, ContextTypes


# Telegram 4096-char 上限；預留 safety 餘裕
_TG_MAX_LEN = 3900


# ── Formatting Helpers ───────────────────────────────────────────────────────

def _pnl(v: float) -> str:
    return f"+${v:,.0f}" if v >= 0 else f"-${abs(v):,.0f}"

def _pct(diff: float, base: float) -> str:
    if not base:
        return ""
    p    = abs(diff / base * 100)
    sign = "+" if diff >= 0 else "-"
    return f"{sign}{p:.2f}%"

def _now_utc() -> str:
    """依 TZ env 顯示本地時間（預設 UTC）"""
    tz_name = os.getenv("TZ", "UTC").strip()
    now = datetime.now(timezone.utc)
    suffix = "UTC"
    if tz_name and tz_name != "UTC":
        try:
            import zoneinfo
            tz = zoneinfo.ZoneInfo(tz_name)
            now = now.astimezone(tz)
            suffix = tz_name
        except Exception:
            pass
    return now.strftime(f"%m/%d %H:%M {suffix}")


def _esc(s) -> str:
    """HTML-escape 任意值；None / numeric 也能處理"""
    if s is None:
        return ""
    return html.escape(str(s), quote=False)


def _truncate(text: str) -> str:
    if len(text) <= _TG_MAX_LEN:
        return text
    return text[:_TG_MAX_LEN - 1] + "…"


# ─────────────────────────────────────────────────────────────────────────────

class TelegramNotifier:
    def __init__(self):
        # R6 FIX: strip 避免空白字元導致 InvalidToken
        self._token   = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self._chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        self._bot     = Bot(token=self._token) if self._token else None

        # Heartbeat state
        self._last_float_pnl:      float = 0.0
        self._last_breaker_state:  str   = ""
        self._state_changed_flags: set   = set()

        # TG-C1: Command callbacks injected by TradingSystem
        self._cb_get_status   = None
        self._cb_close        = None
        self._cb_pause        = None
        self._cb_resume       = None
        self._cb_get_pnl      = None
        self._cb_get_signals  = None

        # Command Application
        self._app: Optional[Application] = None

    # ── Callback Registration (TG-C1) ─────────────────────────────────────────

    def register_callbacks(
        self,
        get_status  = None,
        close       = None,
        pause       = None,
        resume      = None,
        get_pnl     = None,
        get_signals = None,
    ):
        """TradingSystem 注入命令回調函式，讓 Telegram 可控制系統"""
        self._cb_get_status  = get_status
        self._cb_close       = close
        self._cb_pause       = pause
        self._cb_resume      = resume
        self._cb_get_pnl     = get_pnl
        self._cb_get_signals = get_signals

    # ── Rate-Limited Send (TG-H3) ─────────────────────────────────────────────

    async def _send(self, text: str, retries: int = 3, reply_markup=None):
        """訊息發送 + RetryAfter 退避重試 + 4096 字截斷"""
        text = _truncate(text)
        if not self._bot or not self._chat_id:
            print(f"[TG] {text[:200]}")
            return

        for attempt in range(retries):
            try:
                await self._bot.send_message(
                    chat_id    = self._chat_id,
                    text       = text,
                    parse_mode = ParseMode.HTML,
                    disable_web_page_preview = True,
                    reply_markup = reply_markup,
                )
                return
            except RetryAfter as e:
                wait = e.retry_after + 1
                print(f"[TG] rate-limited, sleeping {wait}s")
                await asyncio.sleep(wait)
            except (TimedOut, NetworkError) as e:
                wait = 2 ** attempt
                print(f"[TG] network error (attempt {attempt + 1}): {e!r}, retry in {wait}s")
                await asyncio.sleep(wait)
            except Exception as e:
                print(f"[TG] send error: {e!r}")
                return

    def _dashboard_button(self) -> Optional[InlineKeyboardMarkup]:
        url = os.getenv("ALPACA_DASHBOARD_URL", "").strip()
        if not url:
            return None
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("Alpaca Dashboard", url=url)
        ]])

    async def alert(self, message: str, level: str = "INFO"):
        icons = {"CRITICAL": "🔴", "WARNING": "⚠️", "INFO": "ℹ️"}
        icon  = icons.get(level, "")
        # escape 動態訊息；防止 exception 字串含 < > & 破壞 HTML 解析
        await self._send(f"{icon} {_esc(message)}")

    async def shutdown(self):
        """graceful shutdown 時呼叫：關閉 httpx client + polling application"""
        try:
            if self._app is not None:
                await self._app.updater.stop()
                await self._app.stop()
                await self._app.shutdown()
        except Exception:
            pass
        try:
            if self._bot is not None and self._app is None:
                await self._bot.shutdown()
        except Exception:
            pass

    # ── Startup Notification ──────────────────────────────────────────────────

    async def notify_startup(
        self, circuit_state, mode: str = "PAPER",
        loss_streak: int = 0, auto_trade: bool = True,
        market_order: bool = False,
    ):
        state_val = circuit_state.value if hasattr(circuit_state, "value") else str(circuit_state)
        mode_icon = "🧪" if mode == "PAPER" else "🔴"
        cb_icon   = {"CLOSED": "🟢", "HALF": "🟡", "OPEN": "🔴"}.get(state_val, "⚪")

        flags = []
        if not auto_trade:
            flags.append("🚫 <b>auto_trade = false（僅記錄訊號，不下單）</b>")
        if market_order:
            flags.append("⚠️ <b>use_market_order = true（測試模式：市價打，略過 OB/FVG 回踩）</b>")

        lines = [
            "🚀 <b>Trading System v7.1 啟動</b>",
            "─" * 22,
            f"{mode_icon} 模式　<b>{_esc(mode)}</b>",
            f"{cb_icon} 熔斷器　{_esc(state_val)}　連虧 {loss_streak}",
            "📡 WebSocket　連線中…",
        ]
        if flags:
            lines.append("─" * 22)
            lines.extend(flags)
        lines += [
            f"⏰ {_now_utc()}",
            "",
            "💬 可用命令：/status /pause /resume /close /pnl /signals",
        ]
        await self._send("\n".join(lines), reply_markup=self._dashboard_button())

    # ── Trade Open Notification ───────────────────────────────────────────────

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

        risk_usd = abs(entry - sl)
        tp1_usd  = abs(tp1 - entry)
        tp2_usd  = abs(tp2 - entry)

        bias_icon = "🟢" if htf_bias == "BULLISH" else ("🔴" if htf_bias == "BEARISH" else "⚪")
        src_map   = {"BOS": "BOS 突破", "CHOCH": "CHoCH 轉折", "SWEEP": "流動性掃盪"}
        src_label = src_map.get(source, source)

        zone_line = ""
        if ob_level:
            zone_line += f"📦 OB　${ob_level:,.0f}\n"
        if fvg_range:
            zone_line += f"🕳  FVG　{fvg_range}\n"

        text = (
            f"📊 <b>開倉 · {_esc(symbol)} {side_str}</b>\n"
            f"{'─' * 24}\n"
            f"⏰ {_now_utc()}\n"
            f"{bias_icon} HTF　{_esc(htf_bias)}　｜　{_esc(src_label)}\n"
            f"{'─' * 24}\n"
            f"💰 進場限價　<b>${entry:,.0f}</b>\n"
            f"🎯 TP1　${tp1:,.0f}　<i>({_pct(tp1_usd, entry)}  +${tp1_usd:,.0f})</i>\n"
            f"🏁 TP2　${tp2:,.0f}　<i>({_pct(tp2_usd, entry)}  +${tp2_usd:,.0f})</i>\n"
            f"🔻 SL　${sl:,.0f}　<i>({_pct(-risk_usd, entry)}  -${risk_usd:,.0f})</i>\n"
            f"⚡ Hard SL　${hard_sl:,.0f}　<i>盤中觸發</i>\n"
            f"{'─' * 24}\n"
            f"📐 RRR　1 : {rrr:.2f}　｜　風險 ${risk_usd:,.0f}\n"
            f"🔒 失效　收盤穿越 ${inval:,.0f}\n"
            + (f"{'─' * 24}\n{zone_line}" if zone_line else "")
            + (f"<i>{_esc(description)}</i>" if description else "")
        )
        await self._send(text, reply_markup=self._dashboard_button())

    # ── TP1 Notification ──────────────────────────────────────────────────────

    async def notify_tp1(self, symbol: str, pos, realized_pnl: float):
        side_str = "LONG 🔺" if pos.side == "BUY" else "SHORT 🔻"
        tp2      = getattr(pos, "take_profit_2", 0) or 0
        hold_str = pos.hold_duration_str() if hasattr(pos, "hold_duration_str") else ""
        text = (
            f"✅ <b>TP1 達成 · {symbol} {side_str}</b>\n"
            f"{'─' * 24}\n"
            f"💵 本次實現　<b>+${realized_pnl:,.0f}</b>\n"
            f"📦 已平倉 50%，剩餘 50% 持倉中\n"
            f"{'─' * 24}\n"
            f"🔒 止損移至　<b>${pos.entry_price:,.0f}</b>（成本）\n"
            f"⚡ Server Stop 已更新為剩餘倉位\n"
            f"🏁 追蹤目標　${tp2:,.0f}\n"
            f"📈 Trailing SL 啟動（M1 Swing Pivot {pos.trailing_pivot_bars} 根確認）\n"
            + (f"⏱ 持倉　{hold_str}\n" if hold_str else "")
            + f"⏰ {_now_utc()}"
        )
        await self._send(text)

    # ── Close Notification ────────────────────────────────────────────────────

    async def notify_close(self, pos, reason: str, fill_price: float = None, extra: str = ""):
        side_str = "LONG 🔺" if pos.side == "BUY" else "SHORT 🔻"
        symbol   = getattr(pos, "symbol", "BTC/USD")
        pnl      = getattr(pos, "last_pnl", 0) or 0
        realized = getattr(pos, "realized_pnl", 0) or 0
        total    = pnl + realized
        hold_str = pos.hold_duration_str() if hasattr(pos, "hold_duration_str") else ""

        headers = {
            "HARD_SL":              ("⚡", "Hard SL 出場"),
            "INVALIDATED":          ("🔄", "結構失效出場"),
            "TRAILING_SL":          ("📉", "Trailing SL 出場"),
            "HTF_FLIP":             ("🔀", "HTF 偏向翻轉出場"),
            "TP2":                  ("🏆", "TP2 全倉出場"),
            "SL":                   ("🛑", "止損出場"),
            "SERVER_STOP_TRIGGERED":("⚠️", "伺服器端停損觸發"),
            "MAX_HOLD":             ("⏰", "超時強制出場"),
            "MANUAL_CLOSE":         ("🔧", "手動平倉"),
        }
        icon, label = headers.get(reason, ("🚪", f"出場 ({_esc(reason)})"))

        pnl_icon = "🟢" if pnl >= 0 else "🔴"
        tot_icon = "🟢" if total >= 0 else "🔴"

        lines = [
            f"{icon} <b>{_esc(label)} · {_esc(symbol)} {side_str}</b>",
            f"{'─' * 24}",
            f"💰 進場　${pos.entry_price:,.0f}",
        ]
        if fill_price:
            slip = abs(fill_price - pos.entry_price) / pos.entry_price * 100 if pos.entry_price else 0
            lines.append(f"🚪 出場　${fill_price:,.0f}　<i>滑點 {slip:.2f}%</i>")
        lines.append(f"{pnl_icon} 本次損益　<b>{_pnl(pnl)}</b>")

        if realized:
            lines.append(
                f"{tot_icon} 本單合計　{_pnl(total)}  <i>(含 TP1 +${realized:,.0f})</i>"
            )

        if hold_str:
            lines.append(f"⏱ 持倉時間　{hold_str}")

        if extra:
            # extra 內可能含 <b> 等已格式化標籤；若不含 < 就當純文字 escape
            safe_extra = extra if "<" in extra else _esc(extra)
            lines += [f"{'─' * 24}", safe_extra]

        lines.append(f"⏰ {_now_utc()}")
        await self._send("\n".join(lines))

    # ── Circuit Breaker Notification ──────────────────────────────────────────

    async def notify_circuit_breaker(self, state: str, loss_streak: int):
        icons = {"OPEN": "🔴", "HALF": "🟡", "CLOSED": "🟢"}
        descs = {
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
        weekly_pnl    = snapshot.get("weekly_pnl", 0.0)
        signal_stats  = snapshot.get("signal_stats", {})

        self._last_float_pnl     = float_pnl
        self._last_breaker_state = breaker_state

        cb_icon  = {"CLOSED": "🟢", "HALF": "🟡", "OPEN": "🔴"}.get(breaker_state, "⚪")
        has_pos  = position_info != "無持倉"
        pnl_icon = "🟢" if float_pnl >= 0 else "🔴"
        day_icon = "🟢" if daily_pnl >= 0 else "🔴"

        pos_block = (
            f"₿ {position_info}\n"
            f"{pnl_icon} 浮動損益　<b>{_pnl(float_pnl)}</b>\n"
        ) if has_pos else "💤 無持倉\n"

        # 訊號統計區塊
        sig_block = ""
        if signal_stats:
            total = signal_stats.get("total", 0)
            counts = signal_stats.get("counts", {})
            traded = counts.get("BUY", 0) + counts.get("SELL", 0)
            reject_reasons = {k: v for k, v in counts.items()
                              if k not in ("BUY", "SELL", "HOLD")}
            top_rejects = sorted(reject_reasons.items(), key=lambda x: -x[1])[:3]
            reject_str  = "  ".join(f"{r}:{c}" for r, c in top_rejects) or "─"

            sig_block = (
                f"{'─' * 24}\n"
                f"📡 M1 訊號（本期）　共 {total} 根\n"
                f"  ✅ 有效訊號：{traded}　❌ 拒絕：{total - traded}\n"
                f"  主因：{reject_str}\n"
            )

        # TG-M2 FIX: TradingView 連結
        tv_link  = "https://www.tradingview.com/chart/?symbol=BTCUSD&interval=1"
        text = (
            f"💓 <b>系統心跳</b>  {_now_utc()}\n"
            f"{'─' * 24}\n"
            f"{pos_block}"
            f"{'─' * 24}\n"
            f"{cb_icon} 熔斷器　{breaker_state}　連虧 {loss_streak}\n"
            f"{day_icon} 今日損益　{_pnl(daily_pnl)}　｜　本週 {_pnl(weekly_pnl)}\n"
            f"{sig_block}"
            f"{'─' * 24}\n"
            f"📈 <a href='{tv_link}'>TradingView  BTC/USD M1</a>"
        )
        await self._send(text)

    # ── WS Disconnect Notification ────────────────────────────────────────────

    async def notify_ws_disconnect(self, attempt: int, delay: int, pos_snapshot: str = ""):
        text = (
            f"🚨 <b>WebSocket 斷線（第 {attempt} 次）</b>\n"
            f"{'─' * 24}\n"
            f"⏳ {delay}s 後重連…\n"
        )
        if pos_snapshot and pos_snapshot != "無持倉":
            safe_snap = pos_snapshot if "<" in pos_snapshot else _esc(pos_snapshot)
            text += f"{'─' * 24}\n📍 持倉快照\n{safe_snap}\n"
        await self._send(text, reply_markup=self._dashboard_button())

    # ── TG-C1: Bidirectional Commands ─────────────────────────────────────────

    def _is_authorized(self, update: Update) -> bool:
        """只允許 TELEGRAM_CHAT_ID 指定的聊天室使用命令"""
        return str(update.effective_chat.id) == str(self._chat_id)

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        if self._cb_get_status:
            text = await self._cb_get_status()
        else:
            text = "⚠️ 狀態回調尚未初始化"
        await update.message.reply_html(text)

    async def _cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        if self._cb_pause:
            await self._cb_pause()
            await update.message.reply_text("⏸ 已暫停自動交易（auto_trade = false）")
        else:
            await update.message.reply_text("⚠️ 暫停回調尚未初始化")

    async def _cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        if self._cb_resume:
            await self._cb_resume()
            await update.message.reply_text("▶️ 已恢復自動交易（auto_trade = true）")
        else:
            await update.message.reply_text("⚠️ 恢復回調尚未初始化")

    async def _cmd_close(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        await update.message.reply_text("🔄 正在執行緊急平倉，請稍候…")
        if self._cb_close:
            result = await self._cb_close()
            await update.message.reply_html(result)
        else:
            await update.message.reply_text("⚠️ 平倉回調尚未初始化")

    async def _cmd_pnl(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        if self._cb_get_pnl:
            text = await self._cb_get_pnl()
        else:
            text = "⚠️ PnL 回調尚未初始化"
        await update.message.reply_html(text)

    async def _cmd_signals(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        if self._cb_get_signals:
            text = await self._cb_get_signals()
        else:
            text = "⚠️ 訊號統計回調尚未初始化"
        await update.message.reply_html(text)

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        # R5 FIX: 改為 f-string，否則 {'─' * 24} 不會展開
        sep = "─" * 24
        text = (
            f"📋 <b>可用命令</b>\n"
            f"{sep}\n"
            f"/status  ─ 當前持倉 + 帳戶狀態\n"
            f"/pause   ─ 暫停自動交易\n"
            f"/resume  ─ 恢復自動交易\n"
            f"/close   ─ 緊急全倉平倉\n"
            f"/pnl     ─ 今日/本週損益\n"
            f"/signals ─ 訊號統計\n"
            f"/help    ─ 顯示此說明"
        )
        await update.message.reply_html(text)

    # ── Command Handler Lifecycle ─────────────────────────────────────────────

    async def start_command_handler(self):
        """
        TG-C1 FIX: 在 asyncio.gather 中並行運行 Telegram polling
        Bot.Application 以 start_polling 方式長跑，收到命令後呼叫對應 handler。
        """
        if not self._token or not self._chat_id:
            print("[TG] 命令介面跳過（TOKEN 或 CHAT_ID 未設定）")
            return

        self._app = Application.builder().token(self._token).build()
        self._app.add_handler(CommandHandler("status",  self._cmd_status))
        self._app.add_handler(CommandHandler("pause",   self._cmd_pause))
        self._app.add_handler(CommandHandler("resume",  self._cmd_resume))
        self._app.add_handler(CommandHandler("close",   self._cmd_close))
        self._app.add_handler(CommandHandler("pnl",     self._cmd_pnl))
        self._app.add_handler(CommandHandler("signals", self._cmd_signals))
        self._app.add_handler(CommandHandler("help",    self._cmd_help))

        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)

        # R8 FIX: 統一使用 Application 的 Bot 實例，釋放初始的獨立 Bot
        self._bot = self._app.bot

        print("[TG] 命令介面已啟動（polling mode）")
        try:
            # 無限等待，直到外部 task 取消
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass
        finally:
            try:
                await self._app.updater.stop()
                await self._app.stop()
                await self._app.shutdown()
            except Exception:
                pass
