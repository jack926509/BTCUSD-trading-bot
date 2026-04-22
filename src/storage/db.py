import aiosqlite
import asyncio
import os
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self):
        self.path = os.getenv("DB_PATH", "/app/data/trading.db")
        self._conn: aiosqlite.Connection = None
        self._lock = asyncio.Lock()

    async def connect(self):
        """
        持久連線，避免每次 CRUD 重新 open/close。
        timeout=5.0 防止高頻寫入時 database is locked 錯誤。
        """
        self._conn = await aiosqlite.connect(
            self.path,
            timeout=5.0,
            isolation_level=None,  # autocommit，配合 asyncio.Lock 手動管理
        )
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.row_factory = aiosqlite.Row

    async def close(self):
        if self._conn:
            await self._conn.close()

    async def _execute(self, sql: str, params: tuple = ()):
        """asyncio.Lock 序列化所有寫入，防止並發競態"""
        async with self._lock:
            await self._conn.execute(sql, params)
            await self._conn.commit()

    async def _fetchone(self, sql: str, params: tuple = ()):
        async with self._lock:
            cur = await self._conn.execute(sql, params)
            row = await cur.fetchone()
            return dict(row) if row else None

    async def _fetchall(self, sql: str, params: tuple = ()):
        async with self._lock:
            cur = await self._conn.execute(sql, params)
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    # ── Circuit Breaker ─────────────────────────────────────────────────────

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

    # ── Pending Orders (Ghost Order Protection) ─────────────────────────────

    async def record_pending_order(self, order_id: str, side: str, notional: float):
        await self._execute(
            """INSERT OR IGNORE INTO pending_orders
               (order_id, side, notional_usd, submitted_at)
               VALUES (?, ?, ?, ?)""",
            (order_id, side, notional, _now()),
        )

    async def get_unconfirmed_orders(self) -> list:
        rows = await self._fetchall(
            "SELECT order_id FROM pending_orders WHERE confirmed=0"
        )
        return [r["order_id"] for r in rows]

    async def confirm_order_filled(self, order_id: str):
        await self._execute(
            "UPDATE pending_orders SET confirmed=1, confirmed_at=? WHERE order_id=?",
            (_now(), order_id),
        )

    async def dismiss_pending_order(self, order_id: str):
        """Mark a cancelled/expired order as handled so startup scan ignores it."""
        await self._execute(
            "UPDATE pending_orders SET confirmed=1, confirmed_at=? WHERE order_id=?",
            (_now(), order_id),
        )

    # ── Analysis Log ─────────────────────────────────────────────────────────

    async def save_analysis(self, symbol: str, signal, description: str) -> int:
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
                    getattr(signal, "timeframe", "M5"),
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
                    "7.0",
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

    # ── Trade Log ────────────────────────────────────────────────────────────

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
        close_price = float(getattr(order, "filled_avg_price", 0) or 0) if order else 0.0
        broker_order_id = str(getattr(order, "id", "")) if order else None
        pnl = 0.0
        if pos.entry_price and close_price:
            qty = getattr(pos, "qty", 0) or 0
            if pos.side == "BUY":
                pnl = (close_price - pos.entry_price) * qty
            else:
                pnl = (pos.entry_price - close_price) * qty
        if hasattr(pos, "trade_id") and pos.trade_id:
            await self.close_trade(pos.trade_id, close_price, pnl, reason, broker_order_id)

    async def update_position_analysis_id(self, trade_id: int, analysis_id: int):
        await self._execute(
            "UPDATE trade_log SET analysis_id=? WHERE id=?",
            (analysis_id, trade_id),
        )

    # ── System Log ───────────────────────────────────────────────────────────

    async def log_event(self, event_type: str, message: str):
        await self._execute(
            "INSERT INTO system_log (timestamp, event_type, message) VALUES (?, ?, ?)",
            (_now(), event_type, message),
        )
