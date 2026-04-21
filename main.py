import asyncio
import os

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
        )
        self.executor = OrderExecutor(
            position_mgr = self.position_mgr,
            db           = self.db,
            tg           = self.tg,
        )
        # Wire executor into position_mgr
        self.position_mgr.executor = self.executor

        config_dir  = os.getenv("CONFIG_DIR", "/app/config")
        smc_path    = os.path.join(config_dir, "smc_config.yaml")
        with open(smc_path, "r") as f:
            smc_cfg = yaml.safe_load(f)

        self.claude   = ClaudeClient(config=smc_cfg)
        self.ai_queue = AnalysisQueue(max_size=50)

        # CandleBuilder bridges M1 bars → on_candle_close
        self.candle_builder = CandleBuilder(
            on_candle_close=self._on_aggregated_candle_close
        )

        self.feed = DataFeed(
            on_tick          = self._on_tick,
            on_candle_close  = self._on_raw_bar,
            on_trade_update  = self.executor.on_trade_update,
            position_mgr     = self.position_mgr,
            tg               = self.tg,
        )

    # ── Startup ───────────────────────────────────────────────────────────────

    async def startup(self):
        print("Trading System v7.0 starting...")

        await self._check_volume()
        await self.db.connect()
        await self.circuit.load_state(self.db)
        # Pass db and tg references for persist fallback
        self.circuit.db = self.db
        self.circuit.tg = self.tg

        await self.executor.scan_orphan_orders_on_startup()

        is_paper = os.getenv("ALPACA_PAPER_MODE", "true").lower() == "true"
        await self.tg.notify_startup(
            self.circuit.state, mode="PAPER" if is_paper else "LIVE"
        )

        # Seed historical bars for HTF / MTF structure
        await self._seed_historical_bars()

    async def _check_volume(self):
        """偵測 /app/data 是否在已知掛載點"""
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

            api_key    = os.getenv("ALPACA_API_KEY")
            secret_key = os.getenv("ALPACA_SECRET_KEY")
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

            # Seed H4 bars (HTF)
            req_h4 = CryptoBarsRequest(
                symbol_or_symbols = "BTC/USD",
                timeframe         = TimeFrame.Hour,
                start             = datetime.now(timezone.utc) - timedelta(days=60),
                limit             = 250,
            )
            bars_h4 = hist_client.get_crypto_bars(req_h4)
            df_h4   = bars_h4.df
            if not df_h4.empty:
                # Aggregate hourly into H4
                h4_bars = []
                hourly  = bars_to_dicts(df_h4)
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

            # Seed M15 bars (LTF) using 1-hour then downsample from M1 in live
            req_m15 = CryptoBarsRequest(
                symbol_or_symbols = "BTC/USD",
                timeframe         = TimeFrame.Minute,
                start             = datetime.now(timezone.utc) - timedelta(days=5),
                limit             = 500,
            )
            bars_m15 = hist_client.get_crypto_bars(req_m15)
            df_m15   = bars_m15.df
            if not df_m15.empty:
                m1_bars = bars_to_dicts(df_m15)
                m15_bars = []
                for i in range(0, len(m1_bars) - 14, 15):
                    chunk = m1_bars[i:i + 15]
                    m15_bars.append({
                        "open":      chunk[0]["open"],
                        "high":      max(b["high"] for b in chunk),
                        "low":       min(b["low"]  for b in chunk),
                        "close":     chunk[-1]["close"],
                        "volume":    sum(b["volume"] for b in chunk),
                        "timestamp": chunk[-1]["timestamp"],
                    })
                self.smc.seed_bars("BTC/USD", "M15", m15_bars[-100:])
                print(f"Seeded M15: {len(m15_bars)} bars")

        except Exception as e:
            print(f"[WARN] Historical bar seeding failed: {e}")

    # ── Tick and Bar Handlers ─────────────────────────────────────────────────

    async def _on_tick(self, symbol: str, price: float):
        """Fast Track: position SL/TP monitoring"""
        await self.position_mgr.on_tick(symbol, price)

    async def _on_raw_bar(self, symbol: str, bar):
        """
        Alpaca CryptoDataStream 推送的是 1-minute bars。
        驅動 CandleBuilder 聚合為 M15/H1/H4。
        """
        await self.candle_builder.on_m1_bar(symbol, bar)

    async def _on_aggregated_candle_close(self, symbol: str, tf: str, candle: dict):
        """CandleBuilder 聚合完成後呼叫"""
        print(f"Candle close: {symbol} {tf}")

        # Position manager candle close handler (trailing stop, invalidation, etc.)
        if tf == self.smc.ltf:
            await self.position_mgr.on_candle_close(symbol, candle)

        # SMC engine update
        ctx = self.smc.update(symbol, tf, candle)

        # Only process LTF signals for entry
        if tf != self.smc.ltf or not ctx.has_candidate():
            return

        if self.circuit.is_open():
            return

        if not self.risk.is_auto_trade_enabled():
            # Log signal without trading
            await self.ai_queue.enqueue(symbol, ctx)
            return

        await self.ai_queue.enqueue(symbol, ctx)

    # ── AI Queue Handler ──────────────────────────────────────────────────────

    async def _process_ai_job(self, job):
        """
        訊號確認 → 立即送限價單 → 進場成交後送伺服器端停損 → Claude 非同步補寫。
        """
        # Already have open position? Skip.
        if self.position_mgr.has_open_positions():
            return

        if not self.risk.is_auto_trade_enabled():
            # Log only mode: just do Claude description + DB save
            try:
                description = await self.claude.describe(job.symbol, job.signal)
                await self.db.save_analysis(job.symbol, job.signal, description)
            except Exception:
                pass
            return

        notional    = self.risk.calc_notional(job.signal)
        limit_price = job.signal.entry_limit_price

        try:
            # ① 立即送限價單（不等 Claude）
            order = await self.executor.place(
                job.signal.direction, notional, limit_price
            )
        except Exception as e:
            await self.tg.alert(f"⚠️ 下單失敗：{e}", level="WARNING")
            return

        if not order:
            return

        # ② 進場成交 → 開立持倉
        pos = await self.position_mgr.open(
            job.symbol, job.signal, order, analysis_id=None
        )

        # ③ 立即送伺服器端保底停損
        server_stop_id = await self.executor.place_server_side_stop(
            side           = job.signal.direction,
            qty            = float(order.filled_qty),
            hard_sl_price  = job.signal.hard_sl_price,
            buffer_pct     = self.risk.get_server_stop_buffer(),
        )
        if server_stop_id:
            pos.server_stop_order_id = server_stop_id

        # ④ Claude 描述 + DB + Telegram（非同步，不阻塞）
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
        if reason in ("INVALIDATED", "HARD_SL", "HTF_FLIP"):
            return  # 已由 position_manager 獨立處理
        await self.executor.close_position(pos)
        self.circuit.record(reason)
        await self.db.close_position(pos, reason)
        await self.tg.notify_close(pos, reason)

    # ── Heartbeat Loop ────────────────────────────────────────────────────────

    async def _heartbeat_loop(self):
        """
        差異化心跳：
        ① 持倉浮損變化 ±$50
        ② 熔斷器狀態改變
        ③ SL 被移動
        其餘靜默，每 4 小時強制推播一次。
        """
        last_forced = asyncio.get_event_loop().time()
        while True:
            await asyncio.sleep(300)  # 每 5 分鐘檢查一次
            now   = asyncio.get_event_loop().time()
            force = (now - last_forced) >= 14400  # 4 小時

            snapshot = self._build_snapshot()
            if force or self.tg.has_meaningful_state_change(snapshot):
                await self.tg.notify_heartbeat(snapshot)
                if force:
                    last_forced = now

    def _build_snapshot(self) -> dict:
        float_pnl = 0.0
        pos_info  = "無持倉"
        for symbol, pos in self.position_mgr.positions.items():
            # Use last known price (no live price in heartbeat)
            float_pnl = pos.last_pnl
            side_str  = "LONG" if pos.side == "BUY" else "SHORT"
            pos_info  = f"{symbol} {side_str} ${pos.entry_price:,.0f}"

        return {
            "float_pnl":     float_pnl,
            "breaker_state": self.circuit.state.value,
            "loss_streak":   self.circuit.loss_streak,
            "position_info": pos_info,
        }

    # ── Main Run ──────────────────────────────────────────────────────────────

    async def run(self):
        await self.startup()
        await asyncio.gather(
            self.feed.start(),
            self.ai_queue.worker(self._process_ai_job),
            self._heartbeat_loop(),
        )


if __name__ == "__main__":
    system = TradingSystem()
    asyncio.run(system.run())
