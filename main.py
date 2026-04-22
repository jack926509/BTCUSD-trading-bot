import asyncio
import os
import signal
import sys

from dotenv import load_dotenv

load_dotenv()

import yaml

from src.ai.analysis_queue import AnalysisQueue
from src.ai.claude_client import ClaudeClient
from src.core.candle_builder import CandleBuilder
from src.core.feed import DataFeed
from src.core.order_executor import OrderExecutor
from src.core.position_manager import PositionManager
from src.core.smc_engine import SMCEngine
from src.notification.telegram_bot import TelegramNotifier
from src.risk.circuit_breaker import BreakerState, CircuitBreaker
from src.risk.risk_manager import RiskManager
from src.storage.db import Database

KNOWN_VOLUME_PATHS = ["/app/data"]


class TradingSystem:
    def __init__(self):
        self.db      = Database()
        self.circuit = CircuitBreaker()
        self.tg      = TelegramNotifier()
        self.smc     = SMCEngine()
        self.risk    = RiskManager()

        self.position_mgr = PositionManager(
            on_close = self._on_position_close,
            db       = self.db,
            tg       = self.tg,
            smc      = self.smc,
            circuit  = self.circuit,
        )
        # T-1 FIX: 將 limit_order_timeout 從 risk_config 傳入 OrderExecutor
        self.executor = OrderExecutor(
            position_mgr        = self.position_mgr,
            db                  = self.db,
            tg                  = self.tg,
            limit_order_timeout = self.risk.get_limit_order_timeout(),
        )
        self.position_mgr.executor = self.executor

        config_dir = os.getenv("CONFIG_DIR", "/app/config")
        smc_path   = os.path.join(config_dir, "smc_config.yaml")
        try:
            with open(smc_path, "r") as f:
                smc_cfg = yaml.safe_load(f)
        except FileNotFoundError:
            print(f"[ERROR] smc_config.yaml not found at {smc_path}")
            sys.exit(1)

        self.claude   = ClaudeClient(config=smc_cfg)
        self.ai_queue = AnalysisQueue(max_size=50, tg=self.tg)

        self.candle_builder = CandleBuilder(
            on_candle_close=self._on_aggregated_candle_close
        )

        self.feed = DataFeed(
            on_tick         = self._on_tick,
            on_candle_close = self._on_raw_bar,
            on_trade_update = self.executor.on_trade_update,
            position_mgr    = self.position_mgr,
            tg              = self.tg,
        )

        self._shutdown = False

    # ── Startup ───────────────────────────────────────────────────────────────

    async def startup(self):
        print("Trading System v7.0 starting...")

        await self._check_volume()
        await self._connect_db_with_retry()

        await self.circuit.load_state(self.db)
        self.circuit.db = self.db
        self.circuit.tg = self.tg

        await self._refresh_equity()

        await self.executor.scan_orphan_orders_on_startup()

        is_paper = os.getenv("ALPACA_PAPER_MODE", "true").lower() == "true"
        await self.tg.notify_startup(
            self.circuit.state, mode="PAPER" if is_paper else "LIVE"
        )

        await self._seed_historical_bars()

    async def _connect_db_with_retry(self, max_attempts: int = 5):
        """DB 連線失敗時指數退避重試"""
        for attempt in range(max_attempts):
            try:
                await self.db.connect()
                print("[BOOT] Database connected")
                return
            except Exception as e:
                wait = 2 ** attempt
                print(f"[BOOT] DB connect failed (attempt {attempt+1}): {e}，{wait}s 後重試")
                if attempt == max_attempts - 1:
                    await self.tg.alert(
                        f"🔴 DB 連線失敗（{max_attempts} 次後放棄）：{e}", level="CRITICAL"
                    )
                    sys.exit(1)
                await asyncio.sleep(wait)

    async def _refresh_equity(self):
        """取得 Alpaca 帳戶淨值並注入 RiskManager"""
        try:
            account = self.executor.client.get_account()
            equity  = float(account.equity)
            buying  = float(account.buying_power)
            self.risk.set_equity(equity)
            auto_on = self.risk.is_auto_trade_enabled()
            print(
                f"[BOOT] Alpaca account: equity=${equity:,.2f}  "
                f"buying_power=${buying:,.2f}  "
                f"auto_trade={'ON' if auto_on else 'OFF'}"
            )
            if not auto_on:
                print("[BOOT] WARNING: auto_trade=false — signals will NOT place orders")
        except Exception as e:
            print(f"[WARN] 無法取得帳戶淨值：{e}，使用最低名目金額")

    async def _check_volume(self):
        db_path = os.getenv("DB_PATH", "/app/data/trading.db")
        if not any(db_path.startswith(p) for p in KNOWN_VOLUME_PATHS):
            await self.tg.alert(
                f"🔴 [BOOT] Volume 異常：DB 路徑 {db_path} 不在已知掛載點！\n"
                f"熔斷器強制設為 OPEN，請確認 Zeabur Volume 掛載。",
                level="CRITICAL",
            )
            self.circuit.state = BreakerState.OPEN
        else:
            print(f"[BOOT] Volume check OK: {os.path.dirname(db_path)} is mounted")

    async def _seed_historical_bars(self):
        """系統啟動時用歷史 K 棒初始化 SMC 結構"""
        try:
            from alpaca.data.historical import CryptoHistoricalDataClient
            from alpaca.data.requests import CryptoBarsRequest
            from alpaca.data.timeframe import TimeFrame
            from datetime import datetime, timezone, timedelta

            api_key     = os.getenv("ALPACA_API_KEY")
            secret_key  = os.getenv("ALPACA_SECRET_KEY")
            hist_client = CryptoHistoricalDataClient(
                api_key=api_key, secret_key=secret_key
            )

            def bars_to_dicts(df) -> list:
                result = []
                for _, row in df.iterrows():
                    result.append({
                        "open":      float(row["open"]),
                        "high":      float(row["high"]),
                        "low":       float(row["low"]),
                        "close":     float(row["close"]),
                        "volume":    float(row.get("volume", 0)),
                        "timestamp": row.name[1] if hasattr(row.name, "__len__") else row.name,
                    })
                return result

            # Seed H1 / H4
            req_h1 = CryptoBarsRequest(
                symbol_or_symbols = "BTC/USD",
                timeframe         = TimeFrame.Hour,
                start             = datetime.now(timezone.utc) - timedelta(days=60),
                limit             = 1500,
            )
            bars_h1 = hist_client.get_crypto_bars(req_h1)
            df_h1   = bars_h1.df
            if not df_h1.empty:
                hourly = bars_to_dicts(df_h1)
                h4_bars = []
                for i in range(0, len(hourly) - 3, 4):
                    chunk = hourly[i:i + 4]
                    h4_bars.append({
                        "open":      chunk[0]["open"],
                        "high":      max(b["high"] for b in chunk),
                        "low":       min(b["low"]  for b in chunk),
                        "close":     chunk[-1]["close"],
                        "volume":    sum(b["volume"] for b in chunk),
                        "timestamp": chunk[-1]["timestamp"],
                    })
                self.smc.seed_bars("BTC/USD", "H4", h4_bars[-200:])
                self.smc.seed_bars("BTC/USD", "H1", hourly[-200:])
                print(f"Seeded H4: {len(h4_bars)} bars, H1: {len(hourly)} bars")

            # Seed M5
            req_m1 = CryptoBarsRequest(
                symbol_or_symbols = "BTC/USD",
                timeframe         = TimeFrame.Minute,
                start             = datetime.now(timezone.utc) - timedelta(days=3),
                limit             = 4320,
            )
            bars_m1 = hist_client.get_crypto_bars(req_m1)
            df_m1   = bars_m1.df
            if not df_m1.empty:
                m1_bars = bars_to_dicts(df_m1)
                m5_bars = []
                for i in range(0, len(m1_bars) - 4, 5):
                    chunk = m1_bars[i:i + 5]
                    m5_bars.append({
                        "open":      chunk[0]["open"],
                        "high":      max(b["high"] for b in chunk),
                        "low":       min(b["low"]  for b in chunk),
                        "close":     chunk[-1]["close"],
                        "volume":    sum(b["volume"] for b in chunk),
                        "timestamp": chunk[-1]["timestamp"],
                    })
                self.smc.seed_bars("BTC/USD", "M5", m5_bars[-100:])
                print(f"Seeded M5: {len(m5_bars)} bars")

        except Exception as e:
            print(f"[WARN] Historical bar seeding failed: {e}")

    # ── Tick and Bar Handlers ─────────────────────────────────────────────────

    async def _on_tick(self, symbol: str, price: float):
        await self.position_mgr.on_tick(symbol, price)

    async def _on_raw_bar(self, symbol: str, bar):
        await self.candle_builder.on_m1_bar(symbol, bar)

    async def _on_aggregated_candle_close(self, symbol: str, tf: str, candle: dict):
        print(f"Candle close: {symbol} {tf}")

        if tf == self.smc.ltf:
            await self.position_mgr.on_candle_close(symbol, candle)

        ctx = self.smc.update(symbol, tf, candle)

        if tf != self.smc.ltf or not ctx.has_candidate():
            return

        if self.circuit.is_open():
            return

        # B-3 FIX: 每日損失超限時暫停下單
        if self.risk.is_daily_loss_exceeded():
            await self.tg.alert(
                "🔴 每日最大虧損上限已觸及，今日暫停新倉", level="WARNING"
            )
            return

        # T-8 FIX: RRR 最低門檻過濾
        signal = ctx.get_signal()
        if signal.rrr < self.risk.get_min_rrr():
            return

        await self.ai_queue.enqueue(symbol, ctx)

    # ── AI Queue Handler ──────────────────────────────────────────────────────

    async def _process_ai_job(self, job):
        if self.position_mgr.has_open_positions():
            return

        if not self.risk.is_auto_trade_enabled():
            try:
                description = await self.claude.describe(job.symbol, job.signal)
                await self.db.save_analysis(job.symbol, job.signal, description)
            except Exception:
                pass
            return

        # 每次下單前刷新淨值
        await self._refresh_equity()

        notional    = self.risk.calc_notional(job.signal)
        limit_price = job.signal.entry_limit_price
        direction   = job.signal.direction
        side_str    = "LONG 🔺" if direction == "BUY" else "SHORT 🔻"

        if self.risk.is_market_order_mode():
            await self.tg.alert(
                f"🛒 市價單（測試模式）　{job.symbol} {side_str}\n"
                f"訊號價　<b>${limit_price:,.0f}</b>　立即成交",
                level="INFO",
            )
            try:
                order = await self.executor.place_market(direction, notional)
            except Exception as e:
                await self.tg.alert(f"⚠️ 市價單失敗：{e}", level="WARNING")
                return
        else:
            timeout_min = self.risk.get_limit_order_timeout() // 60
            await self.tg.alert(
                f"📋 掛出限價單　{job.symbol} {side_str}\n"
                f"進場目標　<b>${limit_price:,.0f}</b>　等待最長 {timeout_min} 分鐘",
                level="INFO",
            )
            try:
                order = await self.executor.place(direction, notional, limit_price)
            except Exception as e:
                await self.tg.alert(f"⚠️ 下單失敗（API 錯誤）：{e}", level="WARNING")
                return
            if not order:
                await self.tg.alert(
                    f"❌ 限價單過期　{job.symbol} {side_str}\n"
                    f"${limit_price:,.0f} 在 {timeout_min} 分鐘內未成交，訊號作廢",
                    level="INFO",
                )
                return

        try:
            pos = await self.position_mgr.open(
                job.symbol, job.signal, order, analysis_id=None
            )
        except ValueError as e:
            await self.tg.alert(f"🔴 開倉失敗（數量異常）：{e}", level="CRITICAL")
            return

        server_stop_id = await self.executor.place_server_side_stop(
            side          = job.signal.direction,
            qty           = float(order.filled_qty),
            hard_sl_price = job.signal.hard_sl_price,
            buffer_pct    = self.risk.get_server_stop_buffer(),
        )
        if server_stop_id:
            pos.server_stop_order_id = server_stop_id

        asyncio.create_task(self._async_log_and_notify(job, pos))

    async def _async_log_and_notify(self, job, pos):
        """Slow Track：Claude 描述、DB 寫入、Telegram 推播"""
        try:
            description = await self.claude.describe(job.symbol, job.signal)
            analysis_id = await self.db.save_analysis(
                job.symbol, job.signal, description
            )
            await self.db.mark_analysis_executed(analysis_id)
            await self.db.update_position_analysis_id(pos.trade_id, analysis_id)
            pos.analysis_id = analysis_id
            await self.tg.notify_trade_open(job.symbol, job.signal, description)
        except Exception as e:
            await self.tg.alert(f"⚠️ Slow Track 記錄失敗：{e}", level="WARNING")

    # ── Position Close Callback ───────────────────────────────────────────────

    async def _on_position_close(self, pos, reason):
        """
        PositionManager 直接處理所有出場路徑（HARD_SL / INVALIDATED / HTF_FLIP /
        TRAILING_SL / TP2），此 callback 僅作為保底路徑。
        出場後更新每日 PnL 追蹤。
        """
        if reason in ("INVALIDATED", "HARD_SL", "HTF_FLIP", "TRAILING_SL", "TP2"):
            # 已在 PositionManager 完整處理，更新每日 PnL 即可
            self.risk.record_trade_pnl(pos.last_pnl)
            return
        await self.executor.close_position(pos)
        self.circuit.record(reason)
        await self.db.close_position(pos, reason)
        await self.tg.notify_close(pos, reason)
        self.risk.record_trade_pnl(pos.last_pnl)

    # ── Heartbeat Loop ────────────────────────────────────────────────────────

    async def _heartbeat_loop(self):
        """
        差異化心跳：浮損 ±$50 / 熔斷器變更 / SL 移動 → 立即推播
        其餘靜默，每 4 小時強制推播一次。
        """
        last_forced = asyncio.get_event_loop().time()
        while True:
            await asyncio.sleep(300)
            now   = asyncio.get_event_loop().time()
            force = (now - last_forced) >= 14400

            snapshot = self._build_snapshot()
            if force or self.tg.has_meaningful_state_change(snapshot):
                await self.tg.notify_heartbeat(snapshot)
                if force:
                    last_forced = now

    def _build_snapshot(self) -> dict:
        float_pnl = 0.0
        pos_info  = "無持倉"
        for symbol, pos in self.position_mgr.positions.items():
            float_pnl = pos.last_pnl
            side_str  = "LONG" if pos.side == "BUY" else "SHORT"
            pos_info  = f"{symbol} {side_str} ${pos.entry_price:,.0f}"
        return {
            "float_pnl":     float_pnl,
            "breaker_state": self.circuit.state.value,
            "loss_streak":   self.circuit.loss_streak,
            "position_info": pos_info,
            "daily_pnl":     self.risk._daily_pnl,
        }

    # ── Graceful Shutdown ─────────────────────────────────────────────────────

    def _setup_signal_handlers(self):
        """攔截 SIGTERM / SIGINT，優雅關閉並持久化熔斷器狀態"""
        loop = asyncio.get_event_loop()

        def _handle_shutdown(sig_name):
            if self._shutdown:
                return
            self._shutdown = True
            print(f"\n[SHUTDOWN] 收到 {sig_name}，開始優雅關閉...")
            asyncio.create_task(self._graceful_shutdown())

        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda s=sig: _handle_shutdown(s.name))

    async def _graceful_shutdown(self):
        await self.tg.alert("🔴 Trading System 正在關閉（SIGTERM）", level="WARNING")
        try:
            await self.feed.stop()
        except Exception:
            pass
        try:
            await self.circuit._persist(self.db)
        except Exception:
            pass
        try:
            await self.db.close()
        except Exception:
            pass
        print("[SHUTDOWN] 完成，退出")
        sys.exit(0)

    # ── Main Run ──────────────────────────────────────────────────────────────

    async def run(self):
        await self.startup()
        self._setup_signal_handlers()
        await asyncio.gather(
            self.feed.start(),
            self.ai_queue.worker(self._process_ai_job),
            self._heartbeat_loop(),
        )


if __name__ == "__main__":
    system = TradingSystem()
    asyncio.run(system.run())
