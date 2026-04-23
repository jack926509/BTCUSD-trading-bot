import asyncio
import os
import time
import traceback
from datetime import datetime, timezone

from alpaca.data.live import CryptoDataStream
from alpaca.data.historical import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest, CryptoLatestTradeRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient

import logging
log = logging.getLogger(__name__)

# ── Reconnect policy ─────────────────────────────────────────────────────────
RECONNECT_DELAYS  = [5, 10, 20, 40, 60, 90]

# Alpaca 釋放舊 session 最長 ~90s；用 90→180s 階梯避免頻繁嘗試反被延遲
CONN_LIMIT_DELAYS = [90, 120, 180, 180, 180, 180, 180, 180, 180, 180]

MAX_CONN_LIMIT_RETRIES = int(os.getenv("WS_MAX_CONN_LIMIT_RETRIES", "10"))

# First N alerts verbose，之後靜默避免洗版
ALERT_FIRST_N          = 2

# 預設啟動等待 180s。Alpaca session grace 通常 60-90s，但以下情境需要更久：
# - Zeabur rolling deploy 新舊容器重疊窗口
# - API key 剛 regenerate（propagate 約 5-10 分鐘）
# - 連線上限撞過一次後 Alpaca 端計數回復需要額外時間
# 可用 WS_STARTUP_DELAY env 覆寫
STARTUP_DELAY          = int(os.getenv("WS_STARTUP_DELAY", "180"))

# 兩次 connect 之間最小間隔
MIN_RECONNECT_GAP_SEC  = 3

# WS lockfile：成功連線時寫入 timestamp，下次啟動讀回判斷舊實例是否剛掛
WS_LOCKFILE = os.getenv("WS_LOCKFILE_PATH", "/app/data/ws_session.lock")

# 完全跳過 WebSocket，改為 REST polling（排障逃生閥）
WS_DISABLE  = os.getenv("WS_DISABLE", "false").lower() == "true"


def _is_conn_limit(e: Exception) -> bool:
    msg = str(e).lower()
    return "connection limit" in msg or "too many connections" in msg or "429" in msg


def _mask_key(key: str) -> str:
    if not key:
        return "(unset)"
    return f"{key[:6]}…{key[-2:]}" if len(key) > 10 else f"{key[:3]}…"


def _lockfile_age_sec() -> float | None:
    """回傳 lockfile 距今秒數；不存在或讀取失敗回 None"""
    try:
        mtime = os.path.getmtime(WS_LOCKFILE)
        return time.time() - mtime
    except Exception:
        return None


def _touch_lockfile():
    try:
        os.makedirs(os.path.dirname(WS_LOCKFILE), exist_ok=True)
        with open(WS_LOCKFILE, "w") as f:
            f.write(datetime.now(timezone.utc).isoformat())
    except Exception as e:
        log.warning("lockfile write failed: %s", e)


def _delete_lockfile():
    try:
        if os.path.exists(WS_LOCKFILE):
            os.remove(WS_LOCKFILE)
    except Exception:
        pass


class DataFeed:
    """Single-WebSocket data feed (CryptoDataStream only).

    Alpaca paper trading enforces a 1-connection-per-API-key limit.
    TradingStream has been removed; order fill detection is handled via
    REST polling in OrderExecutor._wait_fill_event instead.
    """

    def __init__(
        self,
        on_tick,
        on_candle_close,
        on_trade_update,
        position_mgr,
        tg,
        trading_client: TradingClient = None,   # AT-C1: 共享 TradingClient
    ):
        self.api_key    = os.getenv("ALPACA_API_KEY")
        self.secret_key = os.getenv("ALPACA_SECRET_KEY")
        self.is_paper   = os.getenv("ALPACA_PAPER_MODE", "true").lower() == "true"

        self.on_tick         = on_tick
        self.on_candle_close = on_candle_close
        self.position_mgr    = position_mgr
        self.tg              = tg

        # AT-C1: 使用共享 client 或自建（向後相容）
        self.trading_client = trading_client or TradingClient(
            api_key    = self.api_key,
            secret_key = self.secret_key,
            paper      = self.is_paper,
        )

        self._crypto_stream: CryptoDataStream | None = None
        self._reconciled        = False
        self._reconcile_task    = None
        self._last_connect_at   = 0.0

        # P0-2: Spread filter 需要的最新 bid/ask/spread
        self.latest_bid:        float = 0.0
        self.latest_ask:        float = 0.0
        self.latest_spread_pct: float = 0.0
        self._spread_updated_at: float = 0.0

    # ── Public ────────────────────────────────────────────────────────────────

    async def start(self):
        if not await self._verify_credentials():
            return

        # 逃生閥：手動關閉 WS，改走 REST polling 模式
        if WS_DISABLE:
            log.warning("WS_DISABLE=true → 進入 REST-only 降級模式")
            await self.tg.alert(
                "ℹ️ WS_DISABLE=true 啟用，系統以 REST 輪詢模式運作\n"
                "（延遲較高，僅供排障使用）",
                level="WARNING",
            )
            await self._rest_polling_loop()
            return

        # Lockfile-based 協調：若舊 lockfile < 90 秒 → 舊實例剛退出，Alpaca
        # session 尚未釋放，額外再等 90 秒
        age = _lockfile_age_sec()
        extra_wait = 0
        if age is not None and age < 90:
            extra_wait = int(90 - age) + 10
            log.warning("偵測到 < 90s 的舊 lockfile（age=%ds），額外等待 %ds",
                        int(age), extra_wait)

        total_delay = STARTUP_DELAY + extra_wait
        if total_delay > 0:
            log.info("啟動延遲 %ds（base=%ds + lockfile=%ds），等待舊連線釋放…",
                     total_delay, STARTUP_DELAY, extra_wait)
            await asyncio.sleep(total_delay)

        attempt        = 0
        conn_limit_cnt = 0
        alerted_quiet  = False
        while True:
            await self._rate_limit_reconnect()
            try:
                await self._connect_and_stream()
                _touch_lockfile()   # 成功連線 → 寫 lockfile 供下次啟動協調
                if conn_limit_cnt > 0:
                    await self.tg.alert(
                        f"✅ WebSocket 已成功連線（經過 {conn_limit_cnt} 次等待）",
                        level="INFO",
                    )
                attempt        = 0
                conn_limit_cnt = 0
                alerted_quiet  = False
            except Exception as e:
                await self._force_close_stream()

                if _is_conn_limit(e):
                    conn_limit_cnt += 1
                    if conn_limit_cnt > MAX_CONN_LIMIT_RETRIES:
                        # 連續 N 次連線上限 → 真的有另一程式佔用 key
                        # 原本 os._exit(1) 會讓容器無限重啟仍打 API；改走 REST 降級
                        await self.tg.alert(
                            self._build_conn_limit_critical_msg(conn_limit_cnt),
                            level="CRITICAL",
                        )
                        log.critical("max connection-limit retries exceeded → REST fallback")
                        await self._rest_polling_loop()
                        return

                    delay = CONN_LIMIT_DELAYS[min(conn_limit_cnt - 1, len(CONN_LIMIT_DELAYS) - 1)]
                    log.warning("connection limit (try %d/%d), waiting %ds",
                                conn_limit_cnt, MAX_CONN_LIMIT_RETRIES, delay)

                    if conn_limit_cnt <= ALERT_FIRST_N:
                        await self.tg.alert(
                            self._build_conn_limit_diagnostic_msg(conn_limit_cnt, delay),
                            level="WARNING",
                        )
                    elif not alerted_quiet:
                        alerted_quiet = True
                        await self.tg.alert(
                            f"⚠️ 連線上限持續，後續重試靜默處理"
                            f"（{MAX_CONN_LIMIT_RETRIES} 次後轉 REST polling 降級模式）",
                            level="WARNING",
                        )
                else:
                    conn_limit_cnt = 0
                    alerted_quiet  = False
                    delay = RECONNECT_DELAYS[min(attempt, len(RECONNECT_DELAYS) - 1)]
                    log.error("CryptoDataStream crashed (attempt %d): %r\n%s",
                              attempt + 1, e, traceback.format_exc())
                    if self.position_mgr.has_open_positions():
                        pos_snapshot = self._build_pos_snapshot()
                        await self.tg.notify_ws_disconnect(attempt + 1, delay, pos_snapshot)
                    else:
                        await self.tg.alert(
                            f"⚠️ CryptoDataStream 斷線（第 {attempt + 1} 次），{delay}s 後重連\n"
                            f"錯誤：{repr(e)}",
                            level="WARNING",
                        )
                await asyncio.sleep(delay)
                attempt += 1

    async def stop(self):
        """Explicitly close WebSocket on graceful shutdown."""
        await self._force_close_stream()
        # 乾淨退出 → 刪 lockfile，讓下次啟動不用再等 90 秒
        _delete_lockfile()

    # ── 診斷訊息 ─────────────────────────────────────────────────────────────

    def _build_conn_limit_diagnostic_msg(self, attempt: int, delay: int) -> str:
        mode = "PAPER" if self.is_paper else "LIVE"
        return (
            f"⚠️ WebSocket 連線上限（第 {attempt} 次）\n"
            f"模式：{mode}　API key：{_mask_key(self.api_key)}\n"
            f"{delay}s 後重連。\n"
            f"─────\n"
            f"【第 1 次就撞到的常見原因】\n"
            f"A. Zeabur 有 2 份 replica / 重複 service（最常見！）\n"
            f"   → Dashboard 檢查 Services 是否有重複、Instances=1\n"
            f"B. 剛 regenerate API key（propagate 需 5-10 分）\n"
            f"   → 等 10 分鐘或改設 WS_STARTUP_DELAY=600\n"
            f"C. 上次 crash 的 zombie session（~5 min TTL）\n"
            f"─────\n"
            f"【立即繞道】Zeabur Variables 加 WS_DISABLE=true\n"
            f"→ 走 REST polling 模式，bot 可繼續交易\n"
            f"─────\n"
            f"10 次以上 → 自動降級 REST polling"
        )

    def _build_conn_limit_critical_msg(self, total: int) -> str:
        mode = "PAPER" if self.is_paper else "LIVE"
        return (
            f"🔴 WebSocket 連線上限持續 {total} 次失敗\n"
            f"模式：{mode}　API key：{_mask_key(self.api_key)}\n"
            f"─────\n"
            f"系統降級為 REST polling（延遲較高但可運作）。\n"
            f"排查步驟：\n"
            f"① 檢查是否有其他機器 / 本機在跑同一 key\n"
            f"② Alpaca dashboard 確認 active sessions\n"
            f"③ 重新產生 API key（最徹底）\n"
            f"④ 設 WS_DISABLE=true 永久關 WS（手動 REST）\n"
            f"⑤ 排除後重啟容器（預設 STARTUP_DELAY=90s）"
        )

    # ── REST Polling 降級模式 ────────────────────────────────────────────────

    async def _rest_polling_loop(self):
        """
        降級模式：完全跳過 WebSocket，改以 REST 輪詢：
        - 每 5 秒抓 latest_trade 觸發 on_tick（持倉管理）
        - 每 60 秒抓最新 M1 bar 觸發 on_candle_close（SMC 結構更新）
        延遲較高但可作為連線上限 / WS_DISABLE 的 fallback
        """
        hist = CryptoHistoricalDataClient(
            api_key=self.api_key, secret_key=self.secret_key,
        )
        # 先跑一次 reconcile
        self._trigger_reconcile_once()

        last_bar_ts  = 0
        last_tick_ts = 0
        loop = asyncio.get_running_loop()

        while True:
            now = loop.time()

            # Latest trade（tick）— 每 5 秒
            if now - last_tick_ts >= 5:
                last_tick_ts = now
                try:
                    req = CryptoLatestTradeRequest(symbol_or_symbols="BTC/USD")
                    latest = await asyncio.to_thread(hist.get_crypto_latest_trade, req)
                    trade  = latest["BTC/USD"]
                    price  = float(trade.price)
                    await self.on_tick("BTC/USD", price)
                    # spread 估算（REST 沒 BBO，近似為 0.02%）
                    self.latest_spread_pct  = 0.0002
                    self._spread_updated_at = now
                except Exception as e:
                    log.warning("REST latest_trade failed: %s", e)

            # Latest M1 bar — 每 60 秒
            if now - last_bar_ts >= 60:
                last_bar_ts = now
                try:
                    from datetime import timedelta
                    req = CryptoBarsRequest(
                        symbol_or_symbols="BTC/USD",
                        timeframe=TimeFrame.Minute,
                        start=datetime.now(timezone.utc) - timedelta(minutes=3),
                        limit=3,
                    )
                    bars = await asyncio.to_thread(hist.get_crypto_bars, req)
                    df = bars.df
                    if not df.empty:
                        # 取最新一根已收盤的 M1 bar
                        row = df.iloc[-2] if len(df) >= 2 else df.iloc[-1]
                        class _FakeBar:
                            pass
                        b = _FakeBar()
                        b.symbol    = "BTC/USD"
                        b.open      = float(row["open"])
                        b.high      = float(row["high"])
                        b.low       = float(row["low"])
                        b.close     = float(row["close"])
                        b.volume    = float(row.get("volume", 0))
                        b.timestamp = row.name[1] if hasattr(row.name, "__len__") else row.name
                        await self.on_candle_close("BTC/USD", b)
                except Exception as e:
                    log.warning("REST bar poll failed: %s", e)

            await asyncio.sleep(1)

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _verify_credentials(self) -> bool:
        """REST check so we fail loudly if keys are invalid (before any WS noise)."""
        try:
            # R13 FIX: 使用 run_in_executor 避免阻塞 event loop
            loop = asyncio.get_running_loop()
            acct = await loop.run_in_executor(
                None, self.trading_client.get_account
            )
            log.info("Alpaca 認證成功：account=%s status=%s", acct.id, acct.status)
            return True
        except Exception as e:
            log.error("Alpaca 認證失敗：%r", e)
            await self.tg.alert(
                f"🔴 Alpaca API 認證失敗，無法啟動 WebSocket：{e!r}",
                level="CRITICAL",
            )
            return False

    async def _rate_limit_reconnect(self):
        """Guarantee minimum gap between connects so Alpaca doesn't see overlap."""
        elapsed = time.monotonic() - self._last_connect_at
        if elapsed < MIN_RECONNECT_GAP_SEC:
            await asyncio.sleep(MIN_RECONNECT_GAP_SEC - elapsed)
        self._last_connect_at = time.monotonic()

    async def _connect_and_stream(self):
        stream = CryptoDataStream(
            api_key    = self.api_key,
            secret_key = self.secret_key,
        )
        self._crypto_stream = stream
        self._reconciled    = False
        stream.subscribe_bars(self._on_bar, "BTC/USD")
        stream.subscribe_trades(self._on_trade, "BTC/USD")
        stream.subscribe_quotes(self._on_quote, "BTC/USD")   # P0-2 spread filter
        log.info("WebSocket BTC/USD bars+trades+quotes subscribed")
        # _start_ws() instead of _run_forever() so connection limit exceptions
        # propagate immediately to our retry handler (otherwise _run_forever's
        # internal retry loop would swallow them).
        await stream._start_ws()

    async def _force_close_stream(self):
        """Best-effort cleanup so no client-side socket lingers between retries."""
        if self._crypto_stream is None:
            return
        try:
            await self._crypto_stream._stop_ws()
        except Exception:
            pass
        self._crypto_stream = None

    def _trigger_reconcile_once(self):
        if not self._reconciled:
            self._reconciled = True
            # R3 FIX: 保留 task 引用，防止 GC 回收安全關鍵的 reconciliation
            self._reconcile_task = asyncio.create_task(self._reconcile_positions())

    async def _on_trade(self, trade):
        self._trigger_reconcile_once()
        await self.on_tick(trade.symbol, float(trade.price))

    async def _on_bar(self, bar):
        self._trigger_reconcile_once()
        await self.on_candle_close(bar.symbol, bar)

    async def _on_quote(self, quote):
        """P0-2 更新最新 BBO，供 spread filter 使用"""
        bid = float(getattr(quote, "bid_price", 0) or 0)
        ask = float(getattr(quote, "ask_price", 0) or 0)
        if bid <= 0 or ask <= 0 or ask < bid:
            return
        mid = (bid + ask) / 2
        self.latest_bid         = bid
        self.latest_ask         = ask
        self.latest_spread_pct  = (ask - bid) / mid
        self._spread_updated_at = asyncio.get_running_loop().time()

    def get_spread_pct(self, max_age_sec: float = 10.0) -> float | None:
        """回傳最新 spread 比例；過期回 None 讓呼叫者決定（降級放行 / 阻擋）"""
        if self._spread_updated_at <= 0:
            return None
        now = asyncio.get_running_loop().time()
        if now - self._spread_updated_at > max_age_sec:
            return None
        return self.latest_spread_pct

    # ── Reconciliation ────────────────────────────────────────────────────────

    async def _reconcile_positions(self):
        try:
            alpaca_positions = await asyncio.to_thread(self.trading_client.get_all_positions)
            alpaca_symbols   = {p.symbol for p in alpaca_positions}
            local_symbols    = set(self.position_mgr.positions.keys())

            for symbol in alpaca_symbols - local_symbols:
                pos_data = next(p for p in alpaca_positions if p.symbol == symbol)
                await self.position_mgr.adopt_orphan(symbol, pos_data)
                await self.tg.alert(
                    f"⚠️ Reconcile：發現未追蹤持倉 {symbol}，已接管", level="WARNING"
                )

            for symbol in local_symbols - alpaca_symbols:
                await self.position_mgr.force_close_missing(symbol)
                await self.tg.alert(
                    f"⚠️ Reconcile：{symbol} 持倉已消失，已同步清除", level="WARNING"
                )

            log.info("Reconciliation complete: no drift detected")
        except Exception as e:
            await self.tg.alert(f"🔴 Reconcile 失敗：{e}", level="CRITICAL")

    def _build_pos_snapshot(self) -> str:
        lines = []
        for symbol, pos in self.position_mgr.positions.items():
            side_str = "LONG" if pos.side == "BUY" else "SHORT"
            lines.append(
                f"{symbol} {side_str} ${pos.entry_price:,.0f}\n"
                f"止損：${pos.stop_loss:,.0f} | Hard SL 已在伺服器：${pos.hard_sl_price:,.0f}"
            )
        return "\n".join(lines) if lines else "無持倉"
