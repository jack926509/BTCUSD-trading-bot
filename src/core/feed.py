import asyncio
import os
import traceback

from alpaca.data.live import CryptoDataStream
from alpaca.trading.client import TradingClient
from alpaca.trading.stream import TradingStream

RECONNECT_DELAYS = [1, 2, 4, 8, 16, 30, 60]


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

    async def start(self):
        # TradingStream 在獨立 task 啟動，不受 CryptoDataStream 重連影響
        asyncio.create_task(self._run_trading_stream())

        attempt = 0
        while True:
            try:
                await self._connect_and_stream()
                attempt = 0  # 正常斷線後重置退避計數
            except Exception as e:
                delay = RECONNECT_DELAYS[min(attempt, len(RECONNECT_DELAYS) - 1)]
                print(f"[ERROR] CryptoDataStream crashed (attempt {attempt + 1}): {repr(e)}")
                print(traceback.format_exc())
                if self.position_mgr.has_open_positions():
                    pos_snapshot = self._build_pos_snapshot()
                    await self.tg.notify_ws_disconnect(
                        attempt + 1, delay, pos_snapshot
                    )
                else:
                    await self.tg.alert(
                        f"⚠️ CryptoDataStream 斷線（第 {attempt + 1} 次），{delay}s 後重連\n錯誤：{repr(e)}",
                        level="WARNING",
                    )
                await asyncio.sleep(delay)
                attempt += 1

    async def _run_trading_stream(self):
        """TradingStream 獨立重連迴圈。

        FIX: 使用 stream._run_forever()（內部 coroutine）而非 stream.run()。
        stream.run() 內部呼叫 asyncio.run()，在已有執行中 event loop 的環境
        （如 asyncio.create_task）裡會丟出
        RuntimeError: asyncio.run() cannot be called from a running event loop。
        """
        attempt = 0
        while True:
            try:
                stream = TradingStream(
                    api_key    = self.api_key,
                    secret_key = self.secret_key,
                    paper      = self.is_paper,
                )
                stream.subscribe_trade_updates(self.on_trade_update)
                print("WebSocket trade_updates connecting...")
                await stream._run_forever()
                # 正常結束（伺服器主動關閉）才重置退避
                attempt = 0
            except Exception as e:
                delay = RECONNECT_DELAYS[min(attempt, len(RECONNECT_DELAYS) - 1)]
                print(f"[ERROR] TradingStream crashed (attempt {attempt + 1}): {repr(e)}")
                print(traceback.format_exc())
                await asyncio.sleep(delay)
                attempt += 1

    async def _connect_and_stream(self):
        """FIX: 使用 stream._run_forever() 而非 stream.run()，原因同上。"""
        stream = CryptoDataStream(
            api_key    = self.api_key,
            secret_key = self.secret_key,
        )
        stream.subscribe_bars(self._on_bar, "BTC/USD")
        stream.subscribe_trades(self._on_trade, "BTC/USD")
        print("WebSocket BTC/USD bars+trades subscribed")

        # 先訂閱再 Reconcile，確保 tick/bar callback 在 reconcile 前已就緒
        asyncio.create_task(self._deferred_reconcile())
        await stream._run_forever()

    async def _deferred_reconcile(self):
        """等待一個 event loop cycle 確保訂閱完成，再執行 Reconciliation"""
        await asyncio.sleep(0)
        await self._reconcile_positions()

    async def _on_trade(self, trade):
        await self.on_tick(trade.symbol, float(trade.price))

    async def _on_bar(self, bar):
        await self.on_candle_close(bar.symbol, bar)

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
