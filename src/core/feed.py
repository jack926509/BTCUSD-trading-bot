import asyncio
import os
import traceback

from alpaca.data.live import CryptoDataStream
from alpaca.trading.client import TradingClient
from alpaca.trading.stream import TradingStream

RECONNECT_DELAYS = [1, 2, 4, 8, 16, 30, 60]
# When Alpaca rejects with "connection limit exceeded", the stale connection
# from a previous deployment typically expires within 60–90 seconds.
CONN_LIMIT_DELAY = 90


def _is_conn_limit(e: Exception) -> bool:
    return "connection limit" in str(e).lower()


class DataFeed:
    def __init__(self, on_tick, on_candle_close, on_trade_update,
                 position_mgr, tg):
        self.api_key    = os.getenv("ALPACA_API_KEY")
        self.secret_key = os.getenv("ALPACA_SECRET_KEY")
        self.is_paper   = os.getenv("ALPACA_PAPER_MODE", "true").lower() == "true"

        self.on_tick          = on_tick
        self.on_candle_close  = on_candle_close
        self.on_trade_update  = on_trade_update
        self.position_mgr     = position_mgr
        self.tg               = tg

        self.trading_client = TradingClient(
            api_key    = self.api_key,
            secret_key = self.secret_key,
            paper      = self.is_paper,
        )

        # Keep references so stop() can close them explicitly on SIGTERM
        self._crypto_stream:  CryptoDataStream | None = None
        self._trading_stream: TradingStream    | None = None
        self._trading_task:   asyncio.Task     | None = None

    # ── Public ────────────────────────────────────────────────────────────────

    async def start(self):
        # Delay TradingStream by 5 s so CryptoDataStream occupies its slot
        # first; reduces the chance of both hitting the server simultaneously
        # on a fresh deployment.
        self._trading_task = asyncio.create_task(self._run_trading_stream(initial_delay=5))

        attempt = 0
        while True:
            try:
                await self._connect_and_stream()
                attempt = 0
            except Exception as e:
                if _is_conn_limit(e):
                    delay = CONN_LIMIT_DELAY
                    print(
                        f"[WARN] CryptoDataStream: connection limit exceeded — "
                        f"waiting {delay}s for stale connection to expire"
                    )
                    await self.tg.alert(
                        f"⚠️ WebSocket 連線數已達上限，等待 {delay}s 後重連（舊連線過期中）",
                        level="WARNING",
                    )
                else:
                    delay = RECONNECT_DELAYS[min(attempt, len(RECONNECT_DELAYS) - 1)]
                    print(f"[ERROR] CryptoDataStream crashed (attempt {attempt + 1}): {repr(e)}")
                    print(traceback.format_exc())
                    if self.position_mgr.has_open_positions():
                        pos_snapshot = self._build_pos_snapshot()
                        await self.tg.notify_ws_disconnect(attempt + 1, delay, pos_snapshot)
                    else:
                        await self.tg.alert(
                            f"⚠️ CryptoDataStream 斷線（第 {attempt + 1} 次），{delay}s 後重連\n錯誤：{repr(e)}",
                            level="WARNING",
                        )
                await asyncio.sleep(delay)
                attempt += 1

    async def stop(self):
        """Explicitly close both WebSocket streams on graceful shutdown."""
        for stream, name in [
            (self._crypto_stream,  "CryptoDataStream"),
            (self._trading_stream, "TradingStream"),
        ]:
            if stream is not None:
                try:
                    await stream._stop_ws()
                    print(f"[FEED] {name} closed")
                except Exception as e:
                    print(f"[FEED] {name} close error (ignored): {e}")

        if self._trading_task and not self._trading_task.done():
            self._trading_task.cancel()

    # ── Internal streams ──────────────────────────────────────────────────────

    async def _run_trading_stream(self, initial_delay: int = 0):
        """TradingStream 獨立重連迴圈。"""
        if initial_delay:
            await asyncio.sleep(initial_delay)

        attempt = 0
        while True:
            try:
                stream = TradingStream(
                    api_key    = self.api_key,
                    secret_key = self.secret_key,
                    paper      = self.is_paper,
                )
                self._trading_stream = stream
                stream.subscribe_trade_updates(self.on_trade_update)
                print("WebSocket trade_updates connecting...")
                await stream._run_forever()
                attempt = 0
            except asyncio.CancelledError:
                print("[FEED] TradingStream task cancelled")
                return
            except Exception as e:
                if _is_conn_limit(e):
                    delay = CONN_LIMIT_DELAY
                    print(
                        f"[WARN] TradingStream: connection limit exceeded — "
                        f"waiting {delay}s for stale connection to expire"
                    )
                else:
                    delay = RECONNECT_DELAYS[min(attempt, len(RECONNECT_DELAYS) - 1)]
                    print(f"[ERROR] TradingStream crashed (attempt {attempt + 1}): {repr(e)}")
                    print(traceback.format_exc())
                await asyncio.sleep(delay)
                attempt += 1

    async def _connect_and_stream(self):
        stream = CryptoDataStream(
            api_key    = self.api_key,
            secret_key = self.secret_key,
        )
        self._crypto_stream = stream
        stream.subscribe_bars(self._on_bar, "BTC/USD")
        stream.subscribe_trades(self._on_trade, "BTC/USD")
        print("WebSocket BTC/USD bars+trades subscribed")

        asyncio.create_task(self._deferred_reconcile())
        await stream._run_forever()

    async def _deferred_reconcile(self):
        await asyncio.sleep(0)
        await self._reconcile_positions()

    # ── Callbacks ─────────────────────────────────────────────────────────────

    async def _on_trade(self, trade):
        await self.on_tick(trade.symbol, float(trade.price))

    async def _on_bar(self, bar):
        await self.on_candle_close(bar.symbol, bar)

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
