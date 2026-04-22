import asyncio
import os
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    LimitOrderRequest,
    MarketOrderRequest,
    StopLimitOrderRequest,
    StopOrderRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, OrderStatus


class OrderError(Exception):
    pass


class OrderExecutor:
    def __init__(self, position_mgr, db, tg, limit_order_timeout: int = 300):
        self.client = TradingClient(
            api_key    = os.getenv("ALPACA_API_KEY"),
            secret_key = os.getenv("ALPACA_SECRET_KEY"),
            paper      = os.getenv("ALPACA_PAPER_MODE", "true").lower() == "true",
        )
        self.position_mgr         = position_mgr
        self.db                   = db
        self.tg                   = tg
        self._limit_order_timeout = limit_order_timeout

    # ── Fill detection (REST polling) ─────────────────────────────────────────

    async def on_trade_update(self, update):
        """Kept as no-op for API compatibility; fill detection uses REST polling."""
        pass

    async def _wait_fill_event(self, order_id: str, timeout: int):
        """Poll REST API every 2 s until the order fills or timeout expires.

        Replaces the previous TradingStream WebSocket approach, which required
        a second WebSocket connection that exceeds Alpaca paper trading's
        1-connection-per-API-key limit.
        """
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(2)
            try:
                order = self.client.get_order_by_id(order_id)
            except Exception as e:
                print(f"[WARN] get_order_by_id({order_id}): {e}")
                continue
            if order.status == OrderStatus.FILLED:
                return order
            if order.status in (OrderStatus.CANCELED, OrderStatus.EXPIRED,
                                OrderStatus.REJECTED):
                raise OrderError(f"訂單非成交狀態：{order.status}")
        raise asyncio.TimeoutError(f"等待成交超時：{order_id}")

    # ── 進場（預設限價單） ─────────────────────────────────────────────────────

    async def place_market(self, side: str, notional: float) -> object:
        """Market order for testing — fills immediately at current price."""
        req = MarketOrderRequest(
            symbol        = "BTC/USD",
            notional      = round(notional, 2),
            side          = OrderSide.BUY if side == "BUY" else OrderSide.SELL,
            time_in_force = TimeInForce.IOC,
        )
        order = None
        for attempt in range(3):
            try:
                order = self.client.submit_order(req)
                await self.db.record_pending_order(str(order.id), side, notional)
                result = await self._wait_fill_event(str(order.id), timeout=30)
                await self.db.confirm_order_filled(str(order.id))
                return result
            except asyncio.TimeoutError:
                if order:
                    await self.db.dismiss_pending_order(str(order.id))
                raise OrderError(f"市價單成交超時（不應發生）: {order.id if order else 'N/A'}")
            except Exception as e:
                if attempt == 2:
                    raise OrderError(f"市價單失敗：{e}")
                await asyncio.sleep(0.5 * (2 ** attempt))

    async def place(
        self,
        side: str,
        notional: float,
        limit_price: float,
        stop_price: float = None,
    ) -> dict:
        """
        訊號確認後立即呼叫，不等 Claude 描述。
        limit_price：OB/FVG 回踩目標價。
        stop_price：若提供則改用 Stop-Limit（備援模式）。
        """
        if stop_price:
            req = StopLimitOrderRequest(
                symbol        = "BTC/USD",
                notional      = round(notional, 2),
                side          = OrderSide.BUY if side == "BUY" else OrderSide.SELL,
                time_in_force = TimeInForce.GTC,
                stop_price    = round(stop_price, 2),
                limit_price   = round(limit_price, 2),
            )
        else:
            req = LimitOrderRequest(
                symbol        = "BTC/USD",
                notional      = round(notional, 2),
                side          = OrderSide.BUY if side == "BUY" else OrderSide.SELL,
                time_in_force = TimeInForce.GTC,
                limit_price   = round(limit_price, 2),
            )

        order = None
        for attempt in range(3):
            try:
                order = self.client.submit_order(req)
                await self.db.record_pending_order(str(order.id), side, notional)
                result = await self._wait_fill_event(str(order.id), timeout=self._limit_order_timeout)
                await self.db.confirm_order_filled(str(order.id))
                return result
            except asyncio.TimeoutError:
                # Price didn't reach the limit zone — expected SMC behaviour, not an error.
                # Cancel the GTC order, clean up DB, and return None to signal "no fill".
                if order:
                    await self._safe_cancel(str(order.id))
                    await self.db.dismiss_pending_order(str(order.id))
                print(
                    f"[INFO] Limit order expired: {side} ${limit_price:,.0f} "
                    f"not reached in {self._limit_order_timeout}s — signal discarded"
                )
                return None  # caller checks for None and moves on silently
            except Exception as e:
                if attempt == 2:
                    raise OrderError(f"下單失敗：{e}")
                await asyncio.sleep(0.5 * (2 ** attempt))

    # ── TP1 減倉（市價，FIX P1-1） ────────────────────────────────────────────

    async def partial_close(self, symbol: str, pos_side: str, qty: float) -> object:
        """
        FIX P1-1: TP1 部分平倉，送市價單減倉。
        pos_side: 持倉方向（BUY/SELL），減倉方向相反。
        """
        close_side = OrderSide.SELL if pos_side == "BUY" else OrderSide.BUY
        req = MarketOrderRequest(
            symbol        = "BTC/USD",
            qty           = round(qty, 8),
            side          = close_side,
            time_in_force = TimeInForce.IOC,
        )
        order = None
        for attempt in range(3):
            try:
                order = self.client.submit_order(req)
                await self.db.record_pending_order(str(order.id), "TP1_PARTIAL", 0)
                result = await self._wait_fill_event(str(order.id), timeout=15)
                await self.db.confirm_order_filled(str(order.id))
                return result
            except asyncio.TimeoutError:
                if order:
                    await self._verify_and_handle_order(str(order.id))
                if attempt == 2:
                    raise OrderError(f"TP1 減倉超時: {order.id if order else 'N/A'}")
                await asyncio.sleep(0.5 * (2 ** attempt))
            except Exception as e:
                if attempt == 2:
                    raise OrderError(f"TP1 減倉失敗：{e}")
                await asyncio.sleep(0.5 * (2 ** attempt))

    # ── 平倉 ──────────────────────────────────────────────────────────────────

    async def close_position(self, pos) -> object:
        """
        平倉前先取消伺服器端保底停損單。
        平倉訂單也記錄至 pending_orders，防止網路瞬斷造成幽靈持倉。
        """
        server_stop_cancelled = False
        if hasattr(pos, "server_stop_order_id") and pos.server_stop_order_id:
            try:
                self.client.cancel_order_by_id(pos.server_stop_order_id)
                server_stop_cancelled = True
            except Exception:
                pass  # 若已被觸發則忽略

        close_order_id = None
        for attempt in range(3):
            try:
                order          = self.client.close_position("BTC/USD")
                close_order_id = str(order.id)
                await self.db.record_pending_order(close_order_id, "CLOSE", 0)
                result = await self._wait_fill_event(close_order_id, timeout=15)
                await self.db.confirm_order_filled(close_order_id)
                return result
            except asyncio.TimeoutError:
                if close_order_id:
                    cancelled = await self._safe_cancel(close_order_id)
                    if not cancelled:
                        await self._verify_and_handle_order(close_order_id)
                if attempt == 2:
                    raise OrderError(f"平倉超時（3次重試後）: {close_order_id}")
                await asyncio.sleep(0.5 * (2 ** attempt))
            except Exception as e:
                if attempt == 2:
                    # FIX P2-5: 若平倉超時且 server stop 已取消，補送新的 server stop
                    if server_stop_cancelled and hasattr(pos, "hard_sl_price"):
                        await self.tg.alert(
                            "🔴 平倉失敗且 server stop 已取消，嘗試補送 server stop",
                            level="CRITICAL",
                        )
                        try:
                            new_stop_id = await self.place_server_side_stop(
                                side          = pos.side,
                                qty           = pos.qty,
                                hard_sl_price = pos.hard_sl_price,
                            )
                            if new_stop_id:
                                pos.server_stop_order_id = new_stop_id
                        except Exception:
                            pass
                    raise OrderError(f"平倉失敗：{e}")
                await asyncio.sleep(0.5 * (2 ** attempt))

    # ── 伺服器端保底停損 ──────────────────────────────────────────────────────

    async def place_server_side_stop(
        self, side: str, qty: float, hard_sl_price: float, buffer_pct: float = 0.005
    ) -> str:
        """
        進場成交後立即呼叫。
        Alpaca crypto 不支援純 Stop Order，改用 Stop-Limit。
        stop_price 為觸發點（含 buffer），limit_price 再向外留 1% 滑價空間確保成交。
        """
        if side == "BUY":
            stop_price  = round(hard_sl_price * (1 - buffer_pct), 2)
            limit_price = round(stop_price * 0.99, 2)   # sell stop-limit：觸發後 limit 再低 1%
            stop_side   = OrderSide.SELL
        else:
            stop_price  = round(hard_sl_price * (1 + buffer_pct), 2)
            limit_price = round(stop_price * 1.01, 2)   # buy stop-limit：觸發後 limit 再高 1%
            stop_side   = OrderSide.BUY

        req = StopLimitOrderRequest(
            symbol        = "BTC/USD",
            qty           = round(qty, 8),
            side          = stop_side,
            time_in_force = TimeInForce.GTC,
            stop_price    = stop_price,
            limit_price   = limit_price,
        )
        try:
            order = self.client.submit_order(req)
            return str(order.id)
        except Exception as e:
            await self.tg.alert(f"⚠️ 伺服器端停損單送出失敗：{e}", level="WARNING")
            return None

    # ── Ghost Order 防護 ──────────────────────────────────────────────────────

    async def scan_orphan_orders_on_startup(self):
        print("Scanning orphan orders on startup...")
        pending = await self.db.get_unconfirmed_orders()
        if not pending:
            print("Scanning orphan orders on startup... none found.")
        for order_id in pending:
            await self._verify_and_handle_order(order_id)

    async def _safe_cancel(self, order_id: str) -> bool:
        try:
            self.client.cancel_order_by_id(order_id)
            await asyncio.sleep(0.5)
            order = self.client.get_order_by_id(order_id)
            return order.status in (OrderStatus.CANCELED, OrderStatus.EXPIRED)
        except Exception:
            return False

    async def _verify_and_handle_order(self, order_id: str):
        try:
            order = self.client.get_order_by_id(order_id)
            if order.status == OrderStatus.FILLED:
                await self.tg.alert(
                    f"⚠️ 訂單 {order_id} cancel 失敗但已成交，觸發 Reconciliation",
                    level="WARNING",
                )
                await self.position_mgr.reconcile_single(order)
            elif order.status in (
                OrderStatus.PARTIALLY_FILLED,
                OrderStatus.PENDING_NEW,
                OrderStatus.ACCEPTED,
            ):
                await self.tg.alert(
                    f"🔴 訂單 {order_id} 狀態不確定（{order.status}），請手動確認",
                    level="CRITICAL",
                )
        except Exception as e:
            await self.tg.alert(
                f"🔴 訂單查詢失敗：{order_id} / {e}", level="CRITICAL"
            )
