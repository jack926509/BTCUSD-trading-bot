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
from telegram.error import RetryAfter, TimedOut, NetworkError, Conflict
from telegram.ext import Application, CommandHandler, ContextTypes

import logging
log = logging.getLogger(__name__)


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
        self._cb_get_stats    = None   # T4: /stats

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
        get_stats   = None,
    ):
        """TradingSystem 注入命令回調函式，讓 Telegram 可控制系統"""
        self._cb_get_status  = get_status
        self._cb_close       = close
        self._cb_pause       = pause
        self._cb_resume      = resume
        self._cb_get_pnl     = get_pnl
        self._cb_get_signals = get_signals
        self._cb_get_stats   = get_stats

    # ── Rate-Limited Send (TG-H3) ─────────────────────────────────────────────

    async def _send(self, text: str, retries: int = 3, reply_markup=None):
        """訊息發送 + RetryAfter 退避重試 + 4096 字截斷"""
        text = _truncate(text)
        if not self._bot or not self._chat_id:
            log.info("(no bot) %s", text[:200])
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
                log.warning("rate-limited, sleeping %ds", wait)
                await asyncio.sleep(wait)
            except (TimedOut, NetworkError) as e:
                wait = 2 ** attempt
                log.warning("network error (attempt %d): %r, retry in %ds", attempt + 1, e, wait)
                await asyncio.sleep(wait)
            except Exception as e:
                log.error("send error: %r", e)
                return

    @staticmethod
    def _resolve_dashboard_url() -> str:
        """P2-4: 先讀 env；若未設則依 ALPACA_PAPER_MODE 自動推導"""
        url = os.getenv("ALPACA_DASHBOARD_URL", "").strip()
        if url:
            return url
        is_paper = os.getenv("ALPACA_PAPER_MODE", "true").lower() == "true"
        return ("https://app.alpaca.markets/paper/dashboard/overview"
                if is_paper else
                "https://app.alpaca.markets/live/dashboard/overview")

    def _dashboard_button(self) -> Optional[InlineKeyboardMarkup]:
        url = self._resolve_dashboard_url()
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
            "💬 可用命令：/status /pnl /signals /stats",
            "⚙️ 控制命令：/pause /resume /close /help",
        ]
        await self._send("\n".join(lines), reply_markup=self._dashboard_button())

    # ── Trade Open Notification ───────────────────────────────────────────────

    # ── T5 ASCII 結構圖 ───────────────────────────────────────────────────────

    @staticmethod
    def _ascii_chart(entry: float, sl: float, tp1: float, tp2: float,
                     ob: float = None, price_now: float = None,
                     rows: int = 7, direction: str = "BUY") -> str:
        """
        以固定 rows 行的 ASCII 圖顯示 entry / SL / TP1 / TP2 / 當前價 相對位置。
        高價在上、低價在下。
        """
        pts = {"TP2": tp2, "TP1": tp1, "Entry": entry, "SL": sl}
        if ob:        pts["OB"]  = ob
        if price_now: pts["Now"] = price_now
        vals = sorted(pts.values(), reverse=True)
        hi, lo = max(vals), min(vals)
        span   = hi - lo or 1

        lines = []
        for r in range(rows):
            row_price = hi - (span / (rows - 1)) * r
            tags = []
            # 誰最接近這條 line
            for name, v in pts.items():
                rel = (hi - v) / span * (rows - 1)
                if abs(rel - r) < 0.5:
                    tags.append(name)
            tag_str = "/".join(tags) if tags else ""
            bar     = "│" if not tags else "┤"
            lines.append(f"{row_price:>10,.0f} {bar} {tag_str}")
        return "<pre>" + _esc("\n".join(lines)) + "</pre>"

    async def notify_trade_open(self, symbol: str, signal, description: str = "",
                                 risk_usd_abs: float = None,
                                 risk_pct_account: float = None,
                                 trade_no_today: int = None):
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

        # E2/E6 extras
        confluence = getattr(signal, "confluence", False)
        tp_struct  = getattr(signal, "tp_is_structural", False)
        atr_val    = getattr(signal, "atr_value", None)
        retrace    = getattr(signal, "retrace_pct", None)

        tag_bits = []
        if confluence: tag_bits.append("✨ OB+FVG")
        if tp_struct:  tag_bits.append("🧱 結構 TP")
        if atr_val:    tag_bits.append(f"📊 ATR ${atr_val:,.0f}")
        if retrace is not None: tag_bits.append(f"📐 OTE {retrace*100:.0f}%")
        quality_line = "　｜　".join(tag_bits) if tag_bits else ""

        # T6 進場風險顯示
        risk_block = ""
        if risk_usd_abs is not None:
            pct_str = f"（帳戶 {risk_pct_account*100:.2f}%）" if risk_pct_account else ""
            nth     = f"　｜　今日第 {trade_no_today} 筆" if trade_no_today else ""
            risk_block = f"💸 本單風險　${risk_usd_abs:,.2f} {pct_str}{nth}\n"

        # T5 ASCII 結構圖
        ascii_block = self._ascii_chart(
            entry=entry, sl=sl, tp1=tp1, tp2=tp2, ob=ob_level,
            direction=direction,
        )

        text = (
            f"📊 <b>開倉 · {_esc(symbol)} {side_str}</b>\n"
            f"{'─' * 24}\n"
            f"⏰ {_now_utc()}\n"
            f"{bias_icon} HTF　{_esc(htf_bias)}　｜　{_esc(src_label)}\n"
            + (f"{quality_line}\n" if quality_line else "")
            + f"{'─' * 24}\n"
            f"💰 進場限價　<b>${entry:,.0f}</b>\n"
            f"🎯 TP1　${tp1:,.0f}　<i>({_pct(tp1_usd, entry)}  +${tp1_usd:,.0f})</i>\n"
            f"🏁 TP2　${tp2:,.0f}　<i>({_pct(tp2_usd, entry)}  +${tp2_usd:,.0f})</i>\n"
            f"🔻 SL　${sl:,.0f}　<i>({_pct(-risk_usd, entry)}  -${risk_usd:,.0f})</i>\n"
            f"⚡ Hard SL　${hard_sl:,.0f}　<i>盤中觸發</i>\n"
            f"{'─' * 24}\n"
            + risk_block
            + f"📐 RRR　1 : {rrr:.2f}\n"
            f"🔒 失效　收盤穿越 ${inval:,.0f}\n"
            + (f"{'─' * 24}\n{zone_line}" if zone_line else "")
            + f"{'─' * 24}\n{ascii_block}\n"
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

        # T3 MFE / MAE — 若 Position 有追蹤則顯示
        mfe = getattr(pos, "max_favorable_pnl", None)
        mae = getattr(pos, "max_adverse_pnl", None)
        if mfe is not None and mae is not None and (mfe or mae):
            entry_price = pos.entry_price
            sl_price    = getattr(pos, "stop_loss", 0) or 0
            risk_unit   = abs(entry_price - sl_price) * (getattr(pos, "qty", 0) or 0)
            mfe_r = mfe / risk_unit if risk_unit else 0
            mae_r = mae / risk_unit if risk_unit else 0
            captured = (pnl / mfe * 100) if mfe > 0 else 0
            lines.append(
                f"📈 MFE {_pnl(mfe)} ({mfe_r:+.1f}R)  📉 MAE {_pnl(mae)} ({mae_r:+.1f}R)"
            )
            if mfe > 0:
                lines.append(f"🎣 抓到　{captured:.0f}% 最大獲利")

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

    # ── T1 每日收盤結算 ───────────────────────────────────────────────────────

    async def notify_daily_recap(self, stats: dict):
        """
        stats:
          {"date": "2026-04-22", "total": 5, "win": 3, "loss": 2,
           "total_pnl": 87.2, "by_source": {"BOS": {...}, "CHOCH": {...}},
           "max_dd": -32.5, "avg_rrr": 2.1}
        """
        date     = stats.get("date", "今日")
        total    = stats.get("total", 0)
        win      = stats.get("win", 0)
        loss     = stats.get("loss", 0)
        pnl      = stats.get("total_pnl", 0.0)
        max_dd   = stats.get("max_dd", 0.0)
        avg_rrr  = stats.get("avg_rrr", 0.0)
        win_rate = (win / total * 100) if total else 0

        pnl_icon = "🟢" if pnl >= 0 else "🔴"
        lines = [
            f"📅 <b>日報　{_esc(date)}</b>",
            "─" * 24,
            f"📊 交易　{total} 筆　（勝 {win} / 負 {loss}）",
            f"🎯 勝率　{win_rate:.1f}%　｜　平均 RRR {avg_rrr:.2f}",
            f"{pnl_icon} 當日損益　<b>{_pnl(pnl)}</b>",
            f"📉 最大 DD　{_pnl(max_dd)}",
        ]
        by_src = stats.get("by_source") or {}
        if by_src:
            lines.append("─" * 24)
            lines.append("📡 <b>策略分解</b>")
            for src, s in by_src.items():
                s_pnl = s.get("pnl", 0)
                s_win = s.get("win", 0)
                s_tot = s.get("total", 0)
                rate  = (s_win / s_tot * 100) if s_tot else 0
                lines.append(f"  {_esc(src)}　{s_tot} 筆　勝率 {rate:.0f}%　{_pnl(s_pnl)}")
        lines.append(f"⏰ {_now_utc()}")
        await self._send("\n".join(lines))

    async def notify_weekly_recap(self, stats: dict):
        week_label = stats.get("week", "本週")
        total    = stats.get("total", 0)
        win      = stats.get("win", 0)
        loss     = stats.get("loss", 0)
        pnl      = stats.get("total_pnl", 0.0)
        best_day = stats.get("best_day", None)
        worst    = stats.get("worst_day", None)
        win_rate = (win / total * 100) if total else 0

        pnl_icon = "🟢" if pnl >= 0 else "🔴"
        lines = [
            f"📆 <b>週報　{_esc(week_label)}</b>",
            "─" * 24,
            f"📊 本週交易　{total} 筆（勝 {win} / 負 {loss}）",
            f"🎯 勝率　{win_rate:.1f}%",
            f"{pnl_icon} 本週損益　<b>{_pnl(pnl)}</b>",
        ]
        if best_day:
            lines.append(f"🟢 最佳日　{_esc(best_day.get('date'))}　{_pnl(best_day.get('pnl', 0))}")
        if worst:
            lines.append(f"🔴 最差日　{_esc(worst.get('date'))}　{_pnl(worst.get('pnl', 0))}")
        lines.append(f"⏰ {_now_utc()}")
        await self._send("\n".join(lines))

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
        # P2-4: URL 若環境變數缺失，由 _dashboard_button 自動推導
        text += f"{'─' * 24}\n🔗 {_esc(self._resolve_dashboard_url())}"
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

    async def _cmd_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """T4: /stats — 回傳近 7/30 天策略表現"""
        if not self._is_authorized(update):
            return
        if self._cb_get_stats:
            text = await self._cb_get_stats()
        else:
            text = "⚠️ 回測統計回調尚未初始化"
        await update.message.reply_html(text)

    async def _on_polling_error(self, update, context):
        """
        捕獲 Telegram polling 層級錯誤；Conflict 代表另一個 bot 實例也在 poll
        → 與 Alpaca WS 連線上限往往是同一根因（重複部署）
        """
        err = context.error
        if isinstance(err, Conflict):
            # 節流：同 10 分鐘內只推一次
            import time as _t
            now = _t.monotonic()
            if now - getattr(self, "_last_conflict_alert", 0) < 600:
                return
            self._last_conflict_alert = now
            await self.alert(
                "🔴 偵測到另一個 bot 實例在使用同一個 TELEGRAM_BOT_TOKEN!\n"
                "→ 通常也就是 Alpaca WS 連線上限的兇手（重複部署 / 本機 + 雲端同跑）\n"
                "排查：\n"
                "1) 本機：pkill -f 'python main.py'\n"
                "2) Zeabur：檢查 Projects 是否有同 repo 重複 deploy\n"
                "3) VS Code / IDE debugger 是否還掛著 main.py",
                level="CRITICAL",
            )
        else:
            log.warning("Telegram polling error: %r", err)

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
            f"/stats   ─ 近 7/30 天策略表現\n"
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
            log.info("命令介面跳過（TOKEN 或 CHAT_ID 未設定）")
            return

        self._app = Application.builder().token(self._token).build()
        self._app.add_handler(CommandHandler("status",  self._cmd_status))
        self._app.add_handler(CommandHandler("pause",   self._cmd_pause))
        self._app.add_handler(CommandHandler("resume",  self._cmd_resume))
        self._app.add_handler(CommandHandler("close",   self._cmd_close))
        self._app.add_handler(CommandHandler("pnl",     self._cmd_pnl))
        self._app.add_handler(CommandHandler("signals", self._cmd_signals))
        self._app.add_handler(CommandHandler("stats",   self._cmd_stats))
        self._app.add_handler(CommandHandler("help",    self._cmd_help))

        # 當另一個 bot 實例也在 poll 同一個 token 時 → 立即 CRITICAL 通知
        # （多實例是 Alpaca WS 連線上限 + 兩邊訊息重複的根因）
        self._app.add_error_handler(self._on_polling_error)

        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)

        # R8 FIX: 統一使用 Application 的 Bot 實例，釋放初始的獨立 Bot
        self._bot = self._app.bot

        log.info("命令介面已啟動（polling mode）")
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
