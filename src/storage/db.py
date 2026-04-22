import aiosqlite
import asyncio
import os
from datetime import datetime, timezone

# schema.sql 與此文件同目錄
_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self):
        self.path  = os.getenv("DB_PATH", "/app/data/trading.db")
        self._conn: aiosqlite.Connection = None
        self._lock = asyncio.Lock()

    # ── Connection ────────────────────────────────────────────────────────────

    async def connect(self):
        """
        持久連線 + 自動執行 schema migration (BE-C4)
        WAL mode 配合 asyncio.Lock 防止並發競態。
        """
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._conn = await aiosqlite.connect(
            self.path,
            timeout=5.0,
            isolation_level=None,   # autocommit; 寫入由 _lock 序列化
        )
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.row_factory = aiosqlite.Row
        # BE-C4: 自動 schema migration（冪等 CREATE IF NOT EXISTS）
        with open(_SCHEMA_PATH, "r") as f:
            await self._conn.executescript(f.read())
        # 舊版 DB 可能沒有 pending_orders.status 欄位 → 手動補上
        cur = await self._conn.execute("PRAGMA table_info(pending_orders)")
        cols = {row[1] for row in await cur.fetchall()}
        if "status" not in cols:
            await self._conn.execute(
                "ALTER TABLE pending_orders ADD COLUMN status TEXT DEFAULT 'PENDING'"
            )
            await self._conn.execute(
                "UPDATE pending_orders SET status='FILLED' WHERE confirmed=1"
            )
            await self._conn.commit()

    async def close(self):
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def _ensure_connected(self):
        """BE-C2: 連線斷線時自動重連（health check）"""
        if self._conn is None:
            await self.connect()
            return
        try:
            await self._conn.execute("SELECT 1")
        except Exception:
            try:
                await self._conn.close()
            except Exception:
                pass
            self._conn = None
            await self.connect()

    # ── Core CRUD Helpers ─────────────────────────────────────────────────────

    async def _execute(self, sql: str, params: tuple = ()):
        """asyncio.Lock 序列化所有寫入，防止並發競態"""
        await self._ensure_connected()
        async with self._lock:
            await self._conn.execute(sql, params)
            await self._conn.commit()

    async def _fetchone(self, sql: str, params: tuple = ()):
        await self._ensure_connected()
        async with self._lock:
            cur = await self._conn.execute(sql, params)
            row = await cur.fetchone()
            return dict(row) if row else None

    async def _fetchall(self, sql: str, params: tuple = ()):
        await self._ensure_connected()
        async with self._lock:
            cur = await self._conn.execute(sql, params)
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    # ── Circuit Breaker ───────────────────────────────────────────────────────

    async def save_circuit_breaker_state(self, state: str, loss_streak: int, opened_at):
        await self._execute(
            """INSERT OR REPLACE INTO circuit_breaker_state
               (id, state, loss_streak, opened_at, updated_at)
               VALUES (1, ?, ?, ?, ?)""",
            (
                state,
                loss_streak,
                opened_at.isoformat() if opened_at else None,
                _now(),
            ),
        )

    async def get_circuit_breaker_state(self) -> dict:
        return await self._fetchone("SELECT * FROM circuit_breaker_state WHERE id=1")

    # ── Pending Orders ────────────────────────────────────────────────────────

    async def record_pending_order(self, order_id: str, side: str, notional: float):
        await self._execute(
            """INSERT OR IGNORE INTO pending_orders
               (order_id, side, notional_usd, submitted_at, status)
               VALUES (?, ?, ?, ?, 'PENDING')""",
            (order_id, side, notional, _now()),
        )

    async def get_unconfirmed_orders(self) -> list:
        """Startup 需重掃的訂單（仍為 PENDING 狀態；FILLED / DISMISSED 都跳過）"""
        rows = await self._fetchall(
            "SELECT order_id FROM pending_orders WHERE status='PENDING'"
        )
        return [r["order_id"] for r in rows]

    async def confirm_order_filled(self, order_id: str):
        await self._execute(
            """UPDATE pending_orders
               SET confirmed=1, confirmed_at=?, status='FILLED'
               WHERE order_id=?""",
            (_now(), order_id),
        )

    async def dismiss_pending_order(self, order_id: str):
        """取消/超時訂單：與 FILLED 區隔，保留審計軌跡，startup 掃描不再重處理"""
        await self._execute(
            """UPDATE pending_orders
               SET confirmed_at=?, status='DISMISSED'
               WHERE order_id=?""",
            (_now(), order_id),
        )

    # ── Risk State Persistence ───────────────────────────────────────────────

    async def load_risk_state(self) -> dict:
        return await self._fetchone("SELECT * FROM risk_state WHERE id=1") or {}

    async def save_risk_state(self, daily_pnl: float, daily_date: str,
                              weekly_pnl: float, weekly_iso: str):
        await self._execute(
            """INSERT OR REPLACE INTO risk_state
               (id, daily_pnl, daily_date, weekly_pnl, weekly_iso, updated_at)
               VALUES (1, ?, ?, ?, ?, ?)""",
            (daily_pnl, daily_date, weekly_pnl, weekly_iso, _now()),
        )

    # ── Analysis Log ──────────────────────────────────────────────────────────

    async def save_analysis(self, symbol: str, signal, description: str) -> int:
        await self._ensure_connected()
        async with self._lock:
            cur = await self._conn.execute(
                """INSERT INTO analysis_log
                   (timestamp, symbol, timeframe, signal, signal_source, htf_bias,
                    entry_limit_price, entry_low, entry_high, stop_loss, hard_sl_price,
                    take_profit_1, take_profit_2, invalidation_level, rrr,
                    structure_description, price_at_signal, config_version)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    _now(),
                    symbol,
                    getattr(signal, "timeframe", "M1"),
                    getattr(signal, "direction", "HOLD"),
                    getattr(signal, "source", None),
                    getattr(signal, "htf_bias", None),
                    getattr(signal, "entry_limit_price", None),
                    getattr(signal, "entry_low", None),
                    getattr(signal, "entry_high", None),
                    getattr(signal, "stop_loss", None),
                    getattr(signal, "hard_sl_price", None),
                    getattr(signal, "take_profit_1", None),
                    getattr(signal, "take_profit_2", None),
                    getattr(signal, "invalidation_level", None),
                    getattr(signal, "rrr", None),
                    description,
                    getattr(signal, "price_at_signal", None),
                    "7.1",
                ),
            )
            await self._conn.commit()
            return cur.lastrowid

    async def mark_analysis_executed(self, analysis_id: int):
        await self._execute(
            "UPDATE analysis_log SET executed=1 WHERE id=?", (analysis_id,)
        )

    async def mark_analysis_skipped(self, analysis_id: int, reason: str):
        await self._execute(
            "UPDATE analysis_log SET skip_reason=? WHERE id=?",
            (reason, analysis_id),
        )

    # ── Trade Log ─────────────────────────────────────────────────────────────

    async def open_trade(
        self,
        analysis_id: int,
        symbol: str,
        side: str,
        notional: float,
        fill_price: float,
        limit_price: float,
        stop_loss: float,
        hard_sl_price: float,
        take_profit: float,
        server_stop_order_id: str = None,
    ) -> int:
        await self._ensure_connected()
        async with self._lock:
            cur = await self._conn.execute(
                """INSERT INTO trade_log
                   (analysis_id, symbol, side, notional_usd, fill_price, limit_price,
                    stop_loss, hard_sl_price, server_stop_order_id, take_profit, open_time)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    analysis_id,
                    symbol,
                    side,
                    notional,
                    fill_price,
                    limit_price,
                    stop_loss,
                    hard_sl_price,
                    server_stop_order_id,
                    take_profit,
                    _now(),
                ),
            )
            await self._conn.commit()
            return cur.lastrowid

    async def update_trade_stop_loss(self, trade_id: int, stop_loss: float,
                                      hard_sl_price: float,
                                      server_stop_order_id: str = None):
        """Trailing SL 移動時同步更新 DB（保留審計軌跡）"""
        await self._execute(
            """UPDATE trade_log
               SET stop_loss=?, hard_sl_price=?,
                   server_stop_order_id=COALESCE(?, server_stop_order_id)
               WHERE id=?""",
            (stop_loss, hard_sl_price, server_stop_order_id, trade_id),
        )

    async def find_recent_open_trade(self, symbol: str) -> dict:
        """Reconcile 接管孤兒持倉時，回頭找最近未關閉的 trade_log 還原 SL/TP"""
        return await self._fetchone(
            """SELECT * FROM trade_log
               WHERE symbol=? AND close_time IS NULL
               ORDER BY id DESC LIMIT 1""",
            (symbol,),
        )

    async def close_trade(
        self,
        trade_id: int,
        close_price: float,
        pnl_usd: float,
        close_reason: str,
        broker_order_id: str = None,
    ):
        await self._execute(
            """UPDATE trade_log
               SET close_time=?, close_price=?, pnl_usd=?, close_reason=?, broker_order_id=?
               WHERE id=?""",
            (
                _now(),
                close_price,
                pnl_usd,
                close_reason,
                broker_order_id,
                trade_id,
            ),
        )

    async def close_position(self, pos, reason: str, order=None):
        """Convenience wrapper called by PositionManager."""
        close_price     = float(getattr(order, "filled_avg_price", 0) or 0) if order else 0.0
        broker_order_id = str(getattr(order, "id", "")) if order else None
        pnl = 0.0
        if pos.entry_price and close_price:
            qty = getattr(pos, "qty", 0) or 0
            pnl = (close_price - pos.entry_price) * qty if pos.side == "BUY" \
                  else (pos.entry_price - close_price) * qty
        if hasattr(pos, "trade_id") and pos.trade_id:
            await self.close_trade(pos.trade_id, close_price, pnl, reason, broker_order_id)

    async def update_position_analysis_id(self, trade_id: int, analysis_id: int):
        await self._execute(
            "UPDATE trade_log SET analysis_id=? WHERE id=?",
            (analysis_id, trade_id),
        )

    # ── Stats Query ───────────────────────────────────────────────────────────

    async def get_daily_stats(self, date_str: str) -> dict:
        """取得指定日期的統計資料（格式：YYYY-MM-DD）"""
        rows = await self._fetchall(
            """SELECT close_reason, pnl_usd FROM trade_log
               WHERE close_time LIKE ? AND close_time IS NOT NULL""",
            (f"{date_str}%",),
        )
        total_pnl  = sum(r["pnl_usd"] or 0 for r in rows)
        win_count  = sum(1 for r in rows if (r["pnl_usd"] or 0) > 0)
        loss_count = sum(1 for r in rows if (r["pnl_usd"] or 0) < 0)
        return {
            "date":       date_str,
            "total_pnl":  round(total_pnl, 2),
            "win":        win_count,
            "loss":       loss_count,
            "total":      len(rows),
        }

    # ── System Log ────────────────────────────────────────────────────────────

    async def log_event(self, event_type: str, message: str):
        await self._execute(
            "INSERT INTO system_log (timestamp, event_type, message) VALUES (?, ?, ?)",
            (_now(), event_type, message),
        )
