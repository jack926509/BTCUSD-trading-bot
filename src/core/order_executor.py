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


# 市價單 polling 間隔（快速）
_MARKET_POLL_INTERVALS = [0.3, 0.5, 0.5, 1.0, 1.0, 2.0]
# 限價單 polling 間隔（漸進退避節省 API call）
_LIMIT_POLL_INTERVALS  = [1.0, 1.0, 2.0, 2.0, 5.0, 5.0, 10.0]


class OrderExecutor:
    def __init__(
        self,
        position_mgr,
        db,
        tg,
        limit_order_timeout: int = 300,
        trading_client: TradingClient = None,   # AT-C1: 共享 TradingClient
    ):
        # AT-C1: 若未傳入共享 client 則自行建立（向後相容）
        self.client = trading_client or TradingClient(
            api_key    = os.getenv("ALPACA_API_KEY"),
            secret_key = os.getenv("ALPACA_SECRET_KEY"),
            paper      = os.getenv("ALPACA_PAPER_MODE", "true").lower() == "true",
        )
        self.position_mgr         = position_mgr
        self.db                   = db
        self.tg                   = tg
        self._limit_order_timeout = limit_order_timeout

    # ── Fill Detection (REST Polling) ─────────────────────────────────────────

    async def on_trade_update(self, update):
        """Kept as no-op for API compatibility; fill detection uses REST polling."""
        pass

    async def _wait_fill_event(
        self, order_id: str, timeout: int, poll_intervals: list = None
    ):
        """
        AT-H1 FIX: 漸進退避 polling，市價單快（0.3s），限價單慢（max 10s）。
        AT-H1 FIX: 使用 asyncio.get_running_loop() 取代已棄用的 get_event_loop()。
        """
        loop     = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        intervals = poll_intervals or _LIMIT_POLL_INTERVALS
        idx = 0

        while loop.time() < deadline:
            delay = intervals[min(idx, len(intervals) - 1)]
            await asyncio.sleep(delay)
            idx += 1

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

    # ── 進場 ──────────────────────────────────────────────────────────────────

    _ALPACA_CRYPTO_MIN_NOTIONAL = 10.0

    async def place_market(self, side: str, notional: float) -> object:
        """Market order — 測試模式使用，立即市價成交。"""
        if notional < self._ALPACA_CRYPTO_MIN_NOTIONAL:
            raise OrderError(f"市價單金額 ${notional:.2f} 低於最低限額 ${self._ALPACA_CRYPTO_MIN_NOTIONAL}")
        req = MarketOrderRequest(
            symbol        = "BTC/USD",
            notional      = round(notional, 2),
            side          = OrderSide.BUY if side == "BUY" else OrderSide.SELL,
            time_in_force = TimeInForce.IOC,
        )
        order = None
        for attempt in range(3):
            try:
                order  = self.client.submit_order(req)
                await self.db.record_pending_order(str(order.id), side, notional)
                result = await self._wait_fill_event(
                    str(order.id), timeout=30,
                    poll_intervals=_MARKET_POLL_INTERVALS,
                )

                # AT-C3 FIX: IOC 可能部分成交，確認 filled_qty > 0
                filled_qty = float(getattr(result, "filled_qty", 0) or 0)
                if filled_qty <= 0:
                    if order:
                        await self.db.dismiss_pending_order(str(order.id))
                    raise OrderError(f"市價單部分成交或未成交：filled_qty={filled_qty}")

                await self.db.confirm_order_filled(str(order.id))
                return result
            except asyncio.TimeoutError:
                if order:
                    await self.db.dismiss_pending_order(str(order.id))
                raise OrderError(f"市價單成交超時: {order.id if order else 'N/A'}")
            except OrderError:
                raise
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
        訊號確認後立即呼叫，掛限價單等待回踩。
        AT-H2 NOTE: Alpaca crypto limit order 以 notional 下單時，
        實際 qty = notional / fill_price（可能與預期略有差異）。
        """
        if notional < self._ALPACA_CRYPTO_MIN_NOTIONAL:
            raise OrderError(f"限價單金額 ${notional:.2f} 低於最低限額 ${self._ALPACA_CRYPTO_MIN_NOTIONAL}")

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
                order  = self.client.submit_order(req)
                await self.db.record_pending_order(str(order.id), side, notional)
                result = await self._wait_fill_event(str(order.id), timeout=self._limit_order_timeout)
                await self.db.confirm_order_filled(str(order.id))
                return result
            except asyncio.TimeoutError:
                if order:
                    await self._safe_cancel(str(order.id))
                    await self.db.dismiss_pending_order(str(order.id))
                print(
                    f"[INFO] Limit order expired: {side} ${limit_price:,.0f} "
                    f"not reached in {self._limit_order_timeout}s — signal discarded"
                )
                return None
            except OrderError:
                raise
            except Exception as e:
                if attempt == 2:
                    raise OrderError(f"下單失敗：{e}")
                await asyncio.sleep(0.5 * (2 ** attempt))

    # ── TP1 減倉 ──────────────────────────────────────────────────────────────

    async def partial_close(self, symbol: str, pos_side: str, qty: float) -> object:
        """TP1 部分平倉，送市價單減倉。"""
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
                order  = self.client.submit_order(req)
                await self.db.record_pending_order(str(order.id), "TP1_PARTIAL", 0)
                result = await self._wait_fill_event(
                    str(order.id), timeout=15,
                    poll_intervals=_MARKET_POLL_INTERVALS,
                )
                # AT-C3 FIX: 驗證部分成交
                filled_qty = float(getattr(result, "filled_qty", 0) or 0)
                if filled_qty <= 0:
                    raise OrderError(f"TP1 減倉未成交：filled_qty={filled_qty}")
                await self.db.confirm_order_filled(str(order.id))
                return result
            except asyncio.TimeoutError:
                if order:
                    await self._verify_and_handle_order(str(order.id))
                if attempt == 2:
                    raise OrderError(f"TP1 減倉超時: {order.id if order else 'N/A'}")
                await asyncio.sleep(0.5 * (2 ** attempt))
            except OrderError:
                raise
            except Exception as e:
                if attempt == 2:
                    raise OrderError(f"TP1 減倉失敗：{e}")
                await asyncio.sleep(0.5 * (2 ** attempt))

    # ── 平倉 ──────────────────────────────────────────────────────────────────

    async def close_position(self, pos) -> object:
        """平倉前先取消伺服器端保底停損單。"""
        server_stop_cancelled = False
        if hasattr(pos, "server_stop_order_id") and pos.server_stop_order_id:
            try:
                self.client.cancel_order_by_id(pos.server_stop_order_id)
                server_stop_cancelled = True
            except Exception:
                pass

        close_order_id = None
        for attempt in range(3):
            try:
                order          = self.client.close_position("BTC/USD")
                close_order_id = str(order.id)
                await self.db.record_pending_order(close_order_id, "CLOSE", 0)
                result = await self._wait_fill_event(
                    close_order_id, timeout=15,
                    poll_intervals=_MARKET_POLL_INTERVALS,
                )
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
                    # 平倉失敗且 server stop 已取消，補送新的 stop
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
        """
        if side == "BUY":
            stop_price  = round(hard_sl_price * (1 - buffer_pct), 2)
            limit_price = round(stop_price * 0.99, 2)
            stop_side   = OrderSide.SELL
        else:
            stop_price  = round(hard_sl_price * (1 + buffer_pct), 2)
            limit_price = round(stop_price * 1.01, 2)
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
