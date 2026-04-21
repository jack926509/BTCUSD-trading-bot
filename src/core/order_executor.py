import asyncio
import os
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    LimitOrderRequest,
    StopLimitOrderRequest,
    StopOrderRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, OrderStatus


class OrderError(Exception):
    pass


class OrderExecutor:
    def __init__(self, position_mgr, db, tg):
        self.client = TradingClient(
            api_key    = os.getenv("ALPACA_API_KEY"),
            secret_key = os.getenv("ALPACA_SECRET_KEY"),
            paper      = os.getenv("ALPACA_PAPER_MODE", "true").lower() == "true",
        )
        self.position_mgr = position_mgr
        self.db           = db
        self.tg           = tg

        # fill_events: order_id → asyncio.Event（由 trade_updates WS 觸發）
        self.fill_events:  dict[str, asyncio.Event] = {}
        self.fill_results: dict[str, object]        = {}

    # ── trade_updates 事件驅動 ────────────────────────────────────────────────

    async def on_trade_update(self, update):
        """由 TradingStream 回調，觸發對應的 fill_event"""
        order_id = str(update.order.id)
        if order_id in self.fill_events:
            self.fill_results[order_id] = update
            self.fill_events[order_id].set()

    async def _wait_fill_event(self, order_id: str, timeout: int):
        """等待 trade_updates WS 推送成交事件"""
        event = asyncio.Event()
        self.fill_events[order_id] = event
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
            update = self.fill_results.pop(order_id, None)
            if update and update.order.status == OrderStatus.FILLED:
                return update.order
            raise OrderError(
                f"訂單非成交狀態：{update.order.status if update else 'unknown'}"
            )
        except asyncio.TimeoutError:
            raise asyncio.TimeoutError(f"等待成交超時：{order_id}")
        finally:
            self.fill_events.pop(order_id, None)

    # ── 進場（預設限價單） ─────────────────────────────────────────────────────

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
                symbol          = "BTC/USD",
                notional        = str(round(notional, 2)),
                side            = OrderSide.BUY if side == "BUY" else OrderSide.SELL,
                time_in_force   = TimeInForce.GTC,
                stop_price      = str(round(stop_price, 2)),
                limit_price     = str(round(limit_price, 2)),
            )
        else:
            req = LimitOrderRequest(
                symbol          = "BTC/USD",
                notional        = str(round(notional, 2)),
                side            = OrderSide.BUY if side == "BUY" else OrderSide.SELL,
                time_in_force   = TimeInForce.GTC,
                limit_price     = str(round(limit_price, 2)),
            )

        for attempt in range(3):
            try:
                order = self.client.submit_order(req)
                await self.db.record_pending_order(order.id, side, notional)
                result = await self._wait_fill_event(str(order.id), timeout=10)
                await self.db.confirm_order_filled(str(order.id))
                return result
            except asyncio.TimeoutError:
                cancelled = await self._safe_cancel(str(order.id))
                if not cancelled:
                    await self._verify_and_handle_order(str(order.id))
                if attempt == 2:
                    raise OrderError(f"下單超時（3次重試後）: {order.id}")
                await asyncio.sleep(0.5 * (2 ** attempt))
            except Exception as e:
                if attempt == 2:
                    raise OrderError(f"下單失敗：{e}")
                await asyncio.sleep(0.5 * (2 ** attempt))

    # ── 平倉 ──────────────────────────────────────────────────────────────────

    async def close_position(self, pos) -> dict:
        """
        平倉前先取消伺服器端保底停損單。
        平倉訂單也記錄至 pending_orders，防止網路瞬斷造成幽靈持倉。
        """
        if hasattr(pos, "server_stop_order_id") and pos.server_stop_order_id:
            try:
                self.client.cancel_order_by_id(pos.server_stop_order_id)
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
                    raise OrderError(f"平倉失敗：{e}")
                await asyncio.sleep(0.5 * (2 ** attempt))

    # ── 伺服器端保底停損 ──────────────────────────────────────────────────────

    async def place_server_side_stop(
        self, side: str, qty: float, hard_sl_price: float, buffer_pct: float = 0.005
    ) -> str:
        """
        進場成交後立即呼叫。
        hard_sl_price 已含結構止損；server stop 再外加 buffer_pct（預設 0.5%）。
        回傳 order_id 供 pos.server_stop_order_id 儲存。
        """
        if side == "BUY":
            stop_price = round(hard_sl_price * (1 - buffer_pct), 2)
            stop_side  = OrderSide.SELL
        else:
            stop_price = round(hard_sl_price * (1 + buffer_pct), 2)
            stop_side  = OrderSide.BUY

        req = StopOrderRequest(
            symbol        = "BTC/USD",
            qty           = str(round(qty, 8)),
            side          = stop_side,
            time_in_force = TimeInForce.GTC,
            stop_price    = str(stop_price),
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
