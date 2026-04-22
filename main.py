import asyncio
import logging
import os
import signal
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

# P2-5: 統一 logging 以 LOG_LEVEL env 控制；子模組用 logging.getLogger(__name__)
logging.basicConfig(
    level   = os.getenv("LOG_LEVEL", "INFO").upper(),
    format  = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
    datefmt = "%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("main")

from alpaca.trading.client import TradingClient

from src.ai.analysis_queue import AnalysisQueue
from src.ai.claude_client import ClaudeClient
from src.config.loader import get_smc_config
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
        # ── 基礎服務 ──────────────────────────────────────────────────────────
        self.db      = Database()
        self.circuit = CircuitBreaker()
        self.tg      = TelegramNotifier()
        self.smc     = SMCEngine()
        self.risk    = RiskManager()

        # BE-C3 FIX: 統一背景 task 集合，防止 GC 回收 + 靜默丟棄異常
        self._background_tasks: set = set()

        # AT-C1 FIX: 建立單一共享 TradingClient，注入所有需要者
        self._trading_client = TradingClient(
            api_key    = os.getenv("ALPACA_API_KEY"),
            secret_key = os.getenv("ALPACA_SECRET_KEY"),
            paper      = os.getenv("ALPACA_PAPER_MODE", "true").lower() == "true",
        )

        # ── 持倉管理 ──────────────────────────────────────────────────────────
        self.position_mgr = PositionManager(
            on_close = self._on_position_close,
            db       = self.db,
            tg       = self.tg,
            smc      = self.smc,
            circuit  = self.circuit,
        )
        # BE-C3: 注入背景 task registry
        self.position_mgr._set_task_registry(self._background_tasks)

        # T-1 FIX: 傳入共享 TradingClient
        self.executor = OrderExecutor(
            position_mgr        = self.position_mgr,
            db                  = self.db,
            tg                  = self.tg,
            limit_order_timeout = self.risk.get_limit_order_timeout(),
            trading_client      = self._trading_client,    # AT-C1
        )
        self.position_mgr.executor = self.executor

        # ── Config ────────────────────────────────────────────────────────────
        smc_cfg = get_smc_config()

        # ── AI ────────────────────────────────────────────────────────────────
        self.claude   = ClaudeClient(config=smc_cfg)
        self.ai_queue = AnalysisQueue(max_size=50, tg=self.tg)

        # ── Feed ──────────────────────────────────────────────────────────────
        self.candle_builder = CandleBuilder(
            on_candle_close=self._on_aggregated_candle_close
        )

        self.feed = DataFeed(
            on_tick         = self._on_tick,
            on_candle_close = self._on_raw_bar,
            on_trade_update = self.executor.on_trade_update,
            position_mgr    = self.position_mgr,
            tg              = self.tg,
            trading_client  = self._trading_client,    # AT-C1
        )

        self._shutdown  = False
        self._paused    = False  # runtime pause flag for /pause command
        # P2-6 /close 二次確認：第一次收到命令只記錄時間戳，15 秒內再次觸發才真正平倉
        self._close_confirm_at: float = 0.0

        # ── TG-C1: 注入命令回調 ───────────────────────────────────────────────
        self.tg.register_callbacks(
            get_status  = self._tg_status,
            close       = self._tg_close,
            pause       = self._tg_pause,
            resume      = self._tg_resume,
            get_pnl     = self._tg_pnl,
            get_signals = self._tg_signals,
            get_stats   = self._tg_stats,   # T4
        )

    # ── BE-C3: Task Factory ───────────────────────────────────────────────────

    def _create_task(self, coro):
        """統一建立 task 並保留引用"""
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    # ── Startup ───────────────────────────────────────────────────────────────

    async def startup(self):
        log.info("Trading System v7.1 starting...")

        await self._check_volume()
        await self._connect_db_with_retry()

        await self.circuit.load_state(self.db)
        self.circuit.db = self.db
        self.circuit.tg = self.tg

        # 啟動時從 DB 恢復每日/每週 PnL（跨重啟保護風控上限）
        try:
            await self.risk.load_state(self.db)
        except Exception as e:
            log.warning("risk state load failed: %s", e)

        await self._refresh_equity()
        await self.executor.scan_orphan_orders_on_startup()

        is_paper = os.getenv("ALPACA_PAPER_MODE", "true").lower() == "true"
        await self.tg.notify_startup(
            self.circuit.state,
            mode         = "PAPER" if is_paper else "LIVE",
            loss_streak  = self.circuit.loss_streak,
            auto_trade   = self.risk.is_auto_trade_enabled(),
            market_order = self.risk.is_market_order_mode(),
        )

        await self._seed_historical_bars()

    async def _connect_db_with_retry(self, max_attempts: int = 5):
        """DB 連線失敗時指數退避重試"""
        for attempt in range(max_attempts):
            try:
                await self.db.connect()
                log.info("Database connected")
                return
            except Exception as e:
                wait = 2 ** attempt
                log.warning("DB connect failed (attempt %d): %s, retry in %ds", attempt+1, e, wait)
                if attempt == max_attempts - 1:
                    await self.tg.alert(
                        f"🔴 DB 連線失敗（{max_attempts} 次後放棄）：{e}", level="CRITICAL"
                    )
                    os._exit(1)
                await asyncio.sleep(wait)

    async def _refresh_equity(self):
        """取得 Alpaca 帳戶淨值並注入 RiskManager"""
        try:
            # AT-M1 NOTE: get_account() 為同步 call，在 startup 時影響較小
            # 若需完全 async 可用 loop.run_in_executor
            loop    = asyncio.get_running_loop()
            account = await loop.run_in_executor(
                None, self._trading_client.get_account
            )
            equity  = float(account.equity)
            buying  = float(account.buying_power)
            self.risk.set_equity(equity)
            auto_on = self.risk.is_auto_trade_enabled()
            log.info(
                "Alpaca account: equity=$%.2f buying_power=$%.2f auto_trade=%s",
                equity, buying, "ON" if auto_on else "OFF",
            )
            if not auto_on:
                log.warning("auto_trade=false, signals will NOT place orders")
        except Exception as e:
            log.warning("cannot fetch account equity: %s, using min notional", e)

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
            log.info("Volume check OK: %s is mounted", os.path.dirname(db_path))

    async def _seed_historical_bars(self):
        """系統啟動時用歷史 K 棒初始化 SMC 結構。
        改用 Alpaca 原生 H4 timeframe，避免 H1 chunk 聚合時 UTC 邊界漂移。"""
        try:
            from alpaca.data.historical import CryptoHistoricalDataClient
            from alpaca.data.requests import CryptoBarsRequest
            from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
            from datetime import timedelta

            hist_client = CryptoHistoricalDataClient(
                api_key    = os.getenv("ALPACA_API_KEY"),
                secret_key = os.getenv("ALPACA_SECRET_KEY"),
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

            loop = asyncio.get_running_loop()

            # H4 native（UTC 00/04/08/12/16/20 對齊）
            req_h4 = CryptoBarsRequest(
                symbol_or_symbols = "BTC/USD",
                timeframe         = TimeFrame(4, TimeFrameUnit.Hour),
                start             = datetime.now(timezone.utc) - timedelta(days=60),
                limit             = 400,
            )
            bars_h4 = await loop.run_in_executor(None, hist_client.get_crypto_bars, req_h4)
            df_h4   = bars_h4.df
            if not df_h4.empty:
                h4_bars = bars_to_dicts(df_h4)
                self.smc.seed_bars("BTC/USD", "H4", h4_bars[-200:])
                log.info("Seeded H4: %d bars (native 4h)", len(h4_bars))

            # H1
            req_h1 = CryptoBarsRequest(
                symbol_or_symbols = "BTC/USD",
                timeframe         = TimeFrame.Hour,
                start             = datetime.now(timezone.utc) - timedelta(days=20),
                limit             = 500,
            )
            bars_h1 = await loop.run_in_executor(None, hist_client.get_crypto_bars, req_h1)
            df_h1   = bars_h1.df
            if not df_h1.empty:
                h1_bars = bars_to_dicts(df_h1)
                self.smc.seed_bars("BTC/USD", "H1", h1_bars[-200:])
                log.info("Seeded H1: %d bars", len(h1_bars))

            # M1
            req_m1 = CryptoBarsRequest(
                symbol_or_symbols = "BTC/USD",
                timeframe         = TimeFrame.Minute,
                start             = datetime.now(timezone.utc) - timedelta(hours=4),
                limit             = 240,
            )
            bars_m1 = await loop.run_in_executor(None, hist_client.get_crypto_bars, req_m1)
            df_m1   = bars_m1.df
            if not df_m1.empty:
                m1_bars = bars_to_dicts(df_m1)
                self.smc.seed_bars("BTC/USD", "M1", m1_bars[-200:])
                log.info("Seeded M1: %d bars", len(m1_bars))

        except Exception as e:
            log.warning("Historical bar seeding failed: %s", e)

    # ── Tick and Bar Handlers ─────────────────────────────────────────────────

    async def _on_tick(self, symbol: str, price: float):
        await self.position_mgr.on_tick(symbol, price)

    async def _on_raw_bar(self, symbol: str, bar):
        await self.candle_builder.on_m1_bar(symbol, bar)

    async def _on_aggregated_candle_close(self, symbol: str, tf: str, candle: dict):
        log.debug("Candle close: %s %s", symbol, tf)

        if tf == self.smc.ltf:
            await self.position_mgr.on_candle_close(symbol, candle)

        ctx = self.smc.update(symbol, tf, candle)

        if tf != self.smc.ltf or not ctx.has_candidate():
            return

        if self.circuit.is_open():
            return

        if self._paused:
            return

        if self.risk.is_daily_loss_exceeded():
            await self.tg.alert("每日最大虧損上限已觸及，今日暫停新倉", level="WARNING")
            # 現有持倉一併強制平倉，避免虧損繼續擴大
            await self._force_close_all("DAILY_LOSS_LIMIT")
            return

        if self.risk.is_weekly_loss_exceeded():
            await self.tg.alert("本週最大虧損上限已觸及，本週暫停新倉", level="WARNING")
            await self._force_close_all("WEEKLY_LOSS_LIMIT")
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

        await self._refresh_equity()

        # P0-2 Spread filter：快速行情時 spread 擴大，跳過進場
        spread = self.feed.get_spread_pct() if hasattr(self.feed, "get_spread_pct") else None
        if spread is not None:
            max_spread = self.risk.get_spread_filter_pct()
            if spread > max_spread:
                await self.tg.alert(
                    f"Spread {spread*100:.3f}% > 門檻 {max_spread*100:.3f}%，跳過訊號",
                    level="INFO",
                )
                return

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

        self._create_task(self._async_log_and_notify(job, pos))

    async def _async_log_and_notify(self, job, pos):
        """Slow Track：Claude 描述、DB 寫入、Telegram 推播（含 T6 風險顯示）"""
        try:
            description = await self.claude.describe(job.symbol, job.signal)
            analysis_id = await self.db.save_analysis(job.symbol, job.signal, description)
            await self.db.mark_analysis_executed(analysis_id)
            await self.db.update_position_analysis_id(pos.trade_id, analysis_id)
            pos.analysis_id = analysis_id

            # T6 進場風險顯示
            risk_usd_abs = abs(pos.entry_price - pos.stop_loss) * pos.qty
            equity       = self.risk.get_equity() if hasattr(self.risk, "get_equity") else 0
            risk_pct     = (risk_usd_abs / equity) if equity else None
            # 當日第幾筆（含本倉位自己）
            today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            stats     = await self.db.get_daily_stats(today_str)
            trade_no  = stats.get("total", 0) + 1

            await self.tg.notify_trade_open(
                job.symbol, job.signal, description,
                risk_usd_abs     = risk_usd_abs,
                risk_pct_account = risk_pct,
                trade_no_today   = trade_no,
            )
        except Exception as e:
            await self.tg.alert(f"Slow Track 記錄失敗：{e}", level="WARNING")

    async def _verify_server_stops(self):
        """
        P0-4: 每次 heartbeat 驗證所有持倉的 server_stop 仍在 Alpaca。
        若被外部取消 / 已觸發 / 狀態異常 → 補送一張新的 server stop。
        """
        from alpaca.trading.enums import OrderStatus
        for symbol, pos in list(self.position_mgr.positions.items()):
            if not pos.server_stop_order_id:
                # 本地已知沒 stop → 補一張
                try:
                    new_id = await self.executor.place_server_side_stop(
                        side          = pos.side,
                        qty           = pos.qty,
                        hard_sl_price = pos.hard_sl_price,
                        buffer_pct    = self.risk.get_server_stop_buffer(),
                    )
                    if new_id:
                        pos.server_stop_order_id = new_id
                        await self.tg.alert(
                            f"{symbol} 偵測 server stop 缺失，已補送（order={new_id}）",
                            level="WARNING",
                        )
                except Exception:
                    pass
                continue

            try:
                order = await self.executor._get_order(pos.server_stop_order_id)
            except Exception:
                continue
            if order.status in (OrderStatus.CANCELED, OrderStatus.EXPIRED, OrderStatus.REJECTED):
                await self.tg.alert(
                    f"{symbol} server stop 已 {order.status}，重送保底",
                    level="CRITICAL",
                )
                try:
                    new_id = await self.executor.place_server_side_stop(
                        side          = pos.side,
                        qty           = pos.qty,
                        hard_sl_price = pos.hard_sl_price,
                        buffer_pct    = self.risk.get_server_stop_buffer(),
                    )
                    pos.server_stop_order_id = new_id
                except Exception:
                    pass

    async def _force_close_all(self, reason: str):
        """風控上限觸發時強制平倉所有持倉（MANUAL_CLOSE reason 不計入熔斷器）"""
        if not self.position_mgr.has_open_positions():
            return
        for symbol, pos in list(self.position_mgr.positions.items()):
            try:
                await self.position_mgr._execute_close(
                    pos, "MANUAL_CLOSE", pos.entry_price,
                    extra_factory=lambda fill, r=reason: f"風控觸發強制平倉：{r}",
                    rollback_state=pos.state,
                )
            except Exception as e:
                await self.tg.alert(
                    f"風控強制平倉失敗（{symbol}）：{e}", level="CRITICAL"
                )

    # ── Position Close Callback ───────────────────────────────────────────────

    async def _on_position_close(self, pos, reason):
        """
        BE-C1 FIX: 所有出場路徑已在 PositionManager._execute_close 中完整處理
        （circuit.record, db, tg 通知）。此 callback 只負責 risk PnL 追蹤。
        """
        self.risk.record_trade_pnl(pos.last_pnl)

    # ── Heartbeat Loop ────────────────────────────────────────────────────────

    async def _heartbeat_loop(self):
        """
        差異化心跳：浮損 ±$50 / 熔斷器變更 / SL 移動 → 立即推播
        其餘靜默，每 4 小時強制推播一次。每次推播後重置訊號統計。
        """
        last_forced = asyncio.get_running_loop().time()
        while True:
            await asyncio.sleep(300)
            now   = asyncio.get_running_loop().time()
            force = (now - last_forced) >= 14400

            # 熔斷器 OPEN 到期自動轉 HALF
            try:
                await self.circuit.check_auto_recovery()
            except Exception:
                pass

            # P0-4 伺服器端停損備援：若持倉的 server stop 意外消失，重送
            try:
                await self._verify_server_stops()
            except Exception as e:
                log.warning("verify server stops failed: %s", e)

            snapshot = self._build_snapshot()
            if force or self.tg.has_meaningful_state_change(snapshot):
                await self.tg.notify_heartbeat(snapshot)
                self.smc.reset_signal_stats()
                if force:
                    last_forced = now

    def _build_snapshot(self) -> dict:
        # R15 FIX: 累加所有持倉的浮動 PnL（支援未來多 symbol）
        float_pnl = 0.0
        pos_parts = []
        for symbol, pos in self.position_mgr.positions.items():
            float_pnl += pos.last_pnl
            side_str   = "LONG" if pos.side == "BUY" else "SHORT"
            pos_parts.append(f"{symbol} {side_str} ${pos.entry_price:,.0f}")
        pos_info = "  |  ".join(pos_parts) if pos_parts else "無持倉"

        return {
            "float_pnl":     float_pnl,
            "breaker_state": self.circuit.state.value,
            "loss_streak":   self.circuit.loss_streak,
            "position_info": pos_info,
            "daily_pnl":     self.risk.daily_pnl,       # BE-H4 FIX: 用 property
            "weekly_pnl":    self.risk.weekly_pnl,
            "signal_stats":  self.smc.get_signal_stats(),
        }

    # ── T1 / T7 每日 & 每週結算 ──────────────────────────────────────────────

    async def _recap_scheduler(self):
        """
        P2-3: 輪詢式 scheduler — 每 30 秒檢查一次是否跨過 UTC 00:00 尚未推播
        比起 sleep_to_midnight 更能抵抗時間漂移 / 容器暫停 / 系統休眠
        """
        last_daily_date  = None   # 最後一次推過日報的日期（UTC）
        last_weekly_iso  = None   # 最後一次推過週報的 ISO week key
        while True:
            await asyncio.sleep(30)
            now = datetime.now(timezone.utc)
            today_iso = now.date().isoformat()
            week_key  = f"{now.isocalendar().year}-W{now.isocalendar().week:02d}"

            # 日報：每天 UTC 00:00-00:05 視窗內，若今日尚未推過 → 推昨日統計
            if now.hour == 0 and now.minute < 5 and last_daily_date != today_iso:
                last_daily_date = today_iso
                try:
                    from datetime import timedelta
                    y = (now - timedelta(days=1)).strftime("%Y-%m-%d")
                    stats = await self._build_daily_recap(y)
                    await self.tg.notify_daily_recap(stats)
                except Exception as e:
                    log.warning("daily recap failed: %s", e)

            # 週報：週一 UTC 00:00-00:10 視窗，若本週尚未推過 → 推上週
            if now.weekday() == 0 and now.hour == 0 and now.minute < 10 \
                    and last_weekly_iso != week_key:
                last_weekly_iso = week_key
                try:
                    w_stats = await self._build_weekly_recap()
                    await self.tg.notify_weekly_recap(w_stats)
                except Exception as e:
                    log.warning("weekly recap failed: %s", e)

    async def _build_daily_recap(self, date_str: str) -> dict:
        stats = await self.db.get_daily_stats(date_str)
        # by_source 從 trade_log 查
        rows  = await self.db._fetchall(
            """SELECT analysis_id, pnl_usd FROM trade_log
               WHERE close_time LIKE ? AND close_time IS NOT NULL""",
            (f"{date_str}%",),
        )
        by_source = {}
        if rows:
            # 反向查 analysis_log 以取得 signal_source
            for r in rows:
                aid = r.get("analysis_id")
                if not aid:
                    continue
                arow = await self.db._fetchone(
                    "SELECT signal_source FROM analysis_log WHERE id=?", (aid,),
                )
                src = (arow or {}).get("signal_source") or "UNKNOWN"
                d = by_source.setdefault(src, {"total": 0, "win": 0, "pnl": 0.0})
                d["total"] += 1
                pnl = r.get("pnl_usd") or 0
                d["pnl"]   += pnl
                if pnl > 0:
                    d["win"] += 1

        # max drawdown 簡易計算：cumulative PnL 低點
        pnls = [(r.get("pnl_usd") or 0) for r in rows]
        cum, peak, max_dd = 0.0, 0.0, 0.0
        for p in pnls:
            cum += p
            peak = max(peak, cum)
            max_dd = min(max_dd, cum - peak)

        stats["by_source"] = by_source
        stats["max_dd"]    = round(max_dd, 2)
        stats["avg_rrr"]   = 0  # 可進一步用 analysis.rrr 計算；暫 0
        return stats

    async def _build_weekly_recap(self) -> dict:
        from datetime import timedelta
        today = datetime.now(timezone.utc).date()
        last_sunday = today - timedelta(days=today.weekday() + 1)  # 上個週日
        daily_stats = []
        for i in range(7):
            d = (last_sunday - timedelta(days=6 - i)).strftime("%Y-%m-%d")
            ds = await self.db.get_daily_stats(d)
            daily_stats.append(ds)

        agg = {
            "total":     sum(d["total"]     for d in daily_stats),
            "win":       sum(d["win"]       for d in daily_stats),
            "loss":      sum(d["loss"]      for d in daily_stats),
            "total_pnl": round(sum(d["total_pnl"] for d in daily_stats), 2),
            "week":      f"{daily_stats[0]['date']} ~ {daily_stats[-1]['date']}",
        }
        if any(d["total"] for d in daily_stats):
            best  = max(daily_stats, key=lambda d: d["total_pnl"])
            worst = min(daily_stats, key=lambda d: d["total_pnl"])
            agg["best_day"]  = {"date": best["date"],  "pnl": best["total_pnl"]}
            agg["worst_day"] = {"date": worst["date"], "pnl": worst["total_pnl"]}
        return agg

    # ── TG-C1: Telegram Command Callbacks ────────────────────────────────────

    async def _tg_status(self) -> str:
        """回傳持倉 + 帳戶狀態字串給 /status 命令"""
        try:
            loop    = asyncio.get_running_loop()
            account = await loop.run_in_executor(None, self._trading_client.get_account)
            equity  = float(account.equity)
            buying  = float(account.buying_power)
        except Exception as e:
            equity  = -1
            buying  = -1

        cb_icon = {"CLOSED": "🟢", "HALF": "🟡", "OPEN": "🔴"}.get(
            self.circuit.state.value, "⚪"
        )
        pause_str = "⏸ 手動暫停" if self._paused else "▶️ 運行中"

        pos_lines = []
        for symbol, pos in self.position_mgr.positions.items():
            float_pnl = pos.last_pnl   # R7 FIX: 由 on_tick 持續更新
            side_str  = "LONG 🔺" if pos.side == "BUY" else "SHORT 🔻"
            pnl_sign  = "+" if float_pnl >= 0 else ""
            pos_lines.append(
                f"  ₿ {symbol} {side_str} ${pos.entry_price:,.0f}\n"
                f"  💰 浮動 {pnl_sign}${float_pnl:,.0f}\n"
                f"  SL ${pos.stop_loss:,.0f} | TP2 ${pos.take_profit_2:,.0f}\n"
                f"  持倉 {pos.hold_duration_str()}"
            )

        pos_str = "\n".join(pos_lines) if pos_lines else "  💤 無持倉"

        return (
            f"📊 <b>系統狀態</b>　{datetime.now(timezone.utc).strftime('%m/%d %H:%M UTC')}\n"
            f"{'─' * 24}\n"
            f"{pause_str}  {cb_icon} 熔斷器 {self.circuit.state.value}\n"
            f"💰 淨值 ${equity:,.2f}　可用 ${buying:,.2f}\n"
            f"📅 今日 {'+' if self.risk.daily_pnl >= 0 else ''}{self.risk.daily_pnl:,.0f}  "
            f"本週 {'+' if self.risk.weekly_pnl >= 0 else ''}{self.risk.weekly_pnl:,.0f}\n"
            f"{'─' * 24}\n"
            f"{pos_str}"
        )

    async def _tg_close(self) -> str:
        """
        /close 需二次確認：
        第一次呼叫 → 記錄時間戳並回傳確認訊息；
        15 秒內第二次呼叫 → 真正執行平倉。
        """
        import time as _time
        if not self.position_mgr.has_open_positions():
            self._close_confirm_at = 0.0
            return "ℹ️ 目前無持倉，無需平倉"

        now = _time.monotonic()
        if now - self._close_confirm_at > 15:
            self._close_confirm_at = now
            pos_list = []
            for symbol, pos in self.position_mgr.positions.items():
                pnl_str = f"{'+' if pos.last_pnl >= 0 else ''}{pos.last_pnl:,.0f}"
                pos_list.append(f"  ₿ {symbol} {pos.side} ${pos.entry_price:,.0f}  PnL {pnl_str}")
            return (
                f"⚠️ <b>確認緊急平倉？</b>\n"
                f"{'─' * 24}\n"
                + "\n".join(pos_list)
                + f"\n{'─' * 24}\n"
                + "15 秒內再輸入 <code>/close</code> 確認執行"
            )

        # 第二次 /close → 執行
        self._close_confirm_at = 0.0
        results = []
        for symbol, pos in list(self.position_mgr.positions.items()):
            try:
                success = await self.position_mgr._execute_close(
                    pos, "MANUAL_CLOSE", pos.entry_price,
                    rollback_state=pos.state,
                )
                results.append(
                    f"✅ {symbol} 平倉成功" if success else f"❌ {symbol} 平倉失敗"
                )
            except Exception as e:
                results.append(f"❌ {symbol} 平倉失敗：{e}")
        return "\n".join(results)

    async def _tg_pause(self):
        """暫停自動交易（不影響已持倉的管理）"""
        self._paused = True
        log.info("auto trade paused via /pause")

    async def _tg_resume(self):
        """恢復自動交易"""
        self._paused = False
        log.info("auto trade resumed via /resume")

    async def _tg_pnl(self) -> str:
        """回傳損益摘要"""
        today     = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        stats     = await self.db.get_daily_stats(today)
        day_icon  = "🟢" if self.risk.daily_pnl >= 0 else "🔴"
        week_icon = "🟢" if self.risk.weekly_pnl >= 0 else "🔴"
        return (
            f"💰 <b>損益摘要</b>　{today}\n"
            f"{'─' * 24}\n"
            f"{day_icon} 今日　${self.risk.daily_pnl:+,.2f}\n"
            f"{week_icon} 本週　${self.risk.weekly_pnl:+,.2f}\n"
            f"{'─' * 24}\n"
            f"今日交易：{stats.get('total', 0)} 筆  "
            f"勝 {stats.get('win', 0)} / 負 {stats.get('loss', 0)}"
        )

    async def _tg_stats(self) -> str:
        """T4: 近 7 / 30 天統計（by source 勝率、平均 RRR、總損益）"""
        from datetime import timedelta
        lines = ["📈 <b>策略統計</b>", "─" * 24]
        for days in (7, 30):
            total_pnl = 0.0
            total     = 0
            win       = 0
            by_source = {}
            for i in range(days):
                d = (datetime.now(timezone.utc).date() - timedelta(days=i)).strftime("%Y-%m-%d")
                ds = await self.db.get_daily_stats(d)
                total     += ds.get("total", 0)
                win       += ds.get("win", 0)
                total_pnl += ds.get("total_pnl", 0.0)
            rate = (win / total * 100) if total else 0
            icon = "🟢" if total_pnl >= 0 else "🔴"
            lines.append(
                f"<b>近 {days} 天</b>　{total} 筆　勝率 {rate:.1f}%　"
                f"{icon} {'+' if total_pnl >= 0 else ''}{total_pnl:,.2f}"
            )
        return "\n".join(lines)

    async def _tg_signals(self) -> str:
        """回傳訊號統計"""
        stats  = self.smc.get_signal_stats()
        total  = stats.get("total", 0)
        counts = stats.get("counts", {})
        traded = counts.get("BUY", 0) + counts.get("SELL", 0)
        htf    = self.smc.get_htf_bias("BTC/USD")
        bias_icon = "🟢" if htf == "BULLISH" else ("🔴" if htf == "BEARISH" else "⚪")

        lines = [
            f"📡 <b>訊號統計（自上次 reset）</b>",
            f"{'─' * 24}",
            f"{bias_icon} HTF 偏向　{htf}",
            f"M1 總評估：{total} 根",
            f"✅ 有效訊號：{traded}　❌ 拒絕：{total - traded}",
            f"{'─' * 24}",
        ]
        for reason, cnt in sorted(counts.items(), key=lambda x: -x[1]):
            lines.append(f"  {reason}：{cnt}")

        return "\n".join(lines)

    # ── Graceful Shutdown ─────────────────────────────────────────────────────

    def _setup_signal_handlers(self):
        loop = asyncio.get_running_loop()

        def _handle_shutdown(sig_name):
            if self._shutdown:
                return
            self._shutdown = True
            log.info("received %s, graceful shutdown start", sig_name)
            self._create_task(self._graceful_shutdown())

        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda s=sig: _handle_shutdown(s.name))

    async def _graceful_shutdown(self):
        await self.tg.alert("Trading System 正在關閉（SIGTERM）", level="WARNING")
        for task in list(self._background_tasks):
            task.cancel()
        try:
            await self.feed.stop()
        except Exception:
            pass
        try:
            await self.circuit._persist(self.db)
        except Exception:
            pass
        try:
            await self.risk._persist()
        except Exception:
            pass
        try:
            await self.db.close()
        except Exception:
            pass
        try:
            await self.tg.shutdown()
        except Exception:
            pass
        log.info("shutdown complete, exiting")
        os._exit(0)

    # ── Main Run ──────────────────────────────────────────────────────────────

    async def run(self):
        await self.startup()
        self._setup_signal_handlers()
        await asyncio.gather(
            self.feed.start(),
            self.ai_queue.worker(self._process_ai_job),
            self._heartbeat_loop(),
            self._recap_scheduler(),            # T1 / T7 每日 / 每週結算
            self.tg.start_command_handler(),    # TG-C1: 雙向命令介面
        )


if __name__ == "__main__":
    system = TradingSystem()
    asyncio.run(system.run())
