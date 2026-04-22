import asyncio
import os
import time
import traceback

from alpaca.data.live import CryptoDataStream
from alpaca.trading.client import TradingClient

# ── Reconnect policy ─────────────────────────────────────────────────────────
# Normal errors: short exponential backoff.
RECONNECT_DELAYS  = [5, 10, 20, 40, 60, 90]

# Connection limit: longer backoff; Alpaca needs time to drop stale sessions.
# Values tuned so 10 retries ≈ 30 min total window.
CONN_LIMIT_DELAYS = [60, 120, 180, 180, 180, 180, 180, 180, 180, 180]

# After this many consecutive connection-limit errors, self-destruct so the
# container supervisor (Zeabur/Docker) restarts us with a fresh process state.
# A fresh process guarantees no leftover asyncio/sockets from our side.
MAX_CONN_LIMIT_RETRIES = int(os.getenv("WS_MAX_CONN_LIMIT_RETRIES", "10"))

# Only send Telegram alerts for the first N connection-limit errors, then a
# single summary. Prevents 30-minute alert spam.
ALERT_FIRST_N          = 2

# Wait on first connect so previous deployment's WebSocket expires.
STARTUP_DELAY          = int(os.getenv("WS_STARTUP_DELAY", "60"))

# Minimum interval between consecutive connects (even on success→disconnect→retry)
# so Alpaca doesn't mistake rapid reconnects for a second concurrent client.
MIN_RECONNECT_GAP_SEC  = 3


def _is_conn_limit(e: Exception) -> bool:
    return "connection limit" in str(e).lower()


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

        if STARTUP_DELAY > 0:
            print(f"[FEED] 啟動延遲 {STARTUP_DELAY}s，等待舊連線釋放…")
            await asyncio.sleep(STARTUP_DELAY)

        attempt        = 0
        conn_limit_cnt = 0
        alerted_quiet  = False
        while True:
            await self._rate_limit_reconnect()
            try:
                await self._connect_and_stream()
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
                        await self.tg.alert(
                            f"🔴 WebSocket 連線上限持續 {conn_limit_cnt} 次，"
                            f"疑似有其他程式佔用同一 API key。重啟容器以取得乾淨狀態。",
                            level="CRITICAL",
                        )
                        print("[FATAL] max connection-limit retries exceeded, exiting 1")
                        os._exit(1)

                    delay = CONN_LIMIT_DELAYS[min(conn_limit_cnt - 1, len(CONN_LIMIT_DELAYS) - 1)]
                    print(f"[WARN] connection limit (try {conn_limit_cnt}/{MAX_CONN_LIMIT_RETRIES}), waiting {delay}s")

                    if conn_limit_cnt <= ALERT_FIRST_N:
                        await self.tg.alert(
                            f"⚠️ WebSocket 連線數已達上限（第 {conn_limit_cnt} 次），{delay}s 後重連",
                            level="WARNING",
                        )
                    elif not alerted_quiet:
                        alerted_quiet = True
                        await self.tg.alert(
                            f"⚠️ 連線上限持續，後續重試靜默處理（最多 {MAX_CONN_LIMIT_RETRIES} 次後重啟容器）",
                            level="WARNING",
                        )
                else:
                    conn_limit_cnt = 0
                    alerted_quiet  = False
                    delay = RECONNECT_DELAYS[min(attempt, len(RECONNECT_DELAYS) - 1)]
                    print(f"[ERROR] CryptoDataStream crashed (attempt {attempt + 1}): {repr(e)}")
                    print(traceback.format_exc())
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

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _verify_credentials(self) -> bool:
        """REST check so we fail loudly if keys are invalid (before any WS noise)."""
        try:
            # R13 FIX: 使用 run_in_executor 避免阻塞 event loop
            loop = asyncio.get_running_loop()
            acct = await loop.run_in_executor(
                None, self.trading_client.get_account
            )
            print(f"[FEED] Alpaca 認證成功：account={acct.id}, status={acct.status}")
            return True
        except Exception as e:
            print(f"[FATAL] Alpaca 認證失敗：{e!r}")
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
        print("WebSocket BTC/USD bars+trades+quotes subscribed")
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
            alpaca_positions = self.trading_client.get_all_positions()
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

            print("Reconciliation complete: no drift detected")
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
