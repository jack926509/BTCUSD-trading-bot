import asyncio
import json
import os
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    GetOrdersRequest,
    LimitOrderRequest,
    MarketOrderRequest,
    StopLimitOrderRequest,
    StopOrderRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, OrderStatus

import logging
log = logging.getLogger(__name__)


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

    # ── Sync-to-async helpers ─────────────────────────────────────────────────
    # alpaca-py 的 REST method 是同步的；直接在 async 函式內呼叫會阻塞 event
    # loop。以下 helper 統一把它們丟進執行緒池，避免撞到 WebSocket 處理。

    async def _submit(self, request):
        return await asyncio.to_thread(self.client.submit_order, request)

    async def _get_order(self, order_id: str):
        return await asyncio.to_thread(self.client.get_order_by_id, order_id)

    async def _cancel_order(self, order_id: str):
        return await asyncio.to_thread(self.client.cancel_order_by_id, order_id)

    async def _close_symbol(self, symbol: str):
        return await asyncio.to_thread(self.client.close_position, symbol)

    async def get_account_async(self):
        return await asyncio.to_thread(self.client.get_account)

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
                order = await self._get_order(order_id)
            except Exception as e:
                log.warning("get_order_by_id(%s): %s", order_id, e)
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
                order  = await self._submit(req)
                await self.db.record_pending_order(str(order.id), side, notional)
                result = await self._wait_fill_event(
                    str(order.id), timeout=30,
                    poll_intervals=_MARKET_POLL_INTERVALS,
                )

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
                    if order:
                        await self.db.dismiss_pending_order(str(order.id))
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
                order  = await self._submit(req)
                await self.db.record_pending_order(str(order.id), side, notional)
                result = await self._wait_fill_event(str(order.id), timeout=self._limit_order_timeout)
                await self.db.confirm_order_filled(str(order.id))
                return result
            except asyncio.TimeoutError:
                if order:
                    await self._safe_cancel(str(order.id))
                    await self.db.dismiss_pending_order(str(order.id))
                log.info(
                    "Limit order expired: %s $%s not reached in %ds — signal discarded",
                    side, f"{limit_price:,.0f}", self._limit_order_timeout,
                )
                return None
            except OrderError:
                raise
            except Exception as e:
                if attempt == 2:
                    if order:
                        await self.db.dismiss_pending_order(str(order.id))
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
                order  = await self._submit(req)
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
                await self._cancel_order(pos.server_stop_order_id)
                server_stop_cancelled = True
                pos.server_stop_order_id = None
            except Exception:
                pass

        close_order_id = None
        for attempt in range(3):
            try:
                order          = await self._close_symbol("BTC/USD")
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
        進場成交後立即呼叫。Alpaca crypto 不支援純 Stop Order，用 Stop-Limit。
        失敗重試 3 次，全部失敗回傳 None（上層可據此強制平倉）。
        """
        # P0-3: stop-limit inner buffer 1% → 0.3%；若快速行情 limit 跳過，
        # 由 client-side on_tick 的 Hard SL 延遲確認作為最後兜底（3 秒內強平）
        if side == "BUY":
            stop_price  = round(hard_sl_price * (1 - buffer_pct), 2)
            limit_price = round(stop_price * 0.997, 2)
            stop_side   = OrderSide.SELL
        else:
            stop_price  = round(hard_sl_price * (1 + buffer_pct), 2)
            limit_price = round(stop_price * 1.003, 2)
            stop_side   = OrderSide.BUY

        req = StopLimitOrderRequest(
            symbol        = "BTC/USD",
            qty           = round(qty, 8),
            side          = stop_side,
            time_in_force = TimeInForce.GTC,
            stop_price    = stop_price,
            limit_price   = limit_price,
        )

        attempt              = 0
        wash_trade_done      = False   # wash trade 只清一次，避免無限迴圈
        balance_cleanup_done = False   # insufficient balance 清理只做一次
        while attempt < 3:
            try:
                order = await self._submit(req)
                return str(order.id)
            except Exception as e:
                err_str      = str(e)
                is_wash_trade = (
                    "40310000" in err_str
                    or "wash trade"  in err_str.lower()
                    or "wash_trade"  in err_str.lower()
                )
                is_insuf_balance = (
                    "insufficient balance" in err_str.lower()
                    or "insufficient_balance" in err_str.lower()
                )

                if is_wash_trade and not wash_trade_done:
                    # 清除衝突委託單後重試，不消耗 attempt 配額
                    wash_trade_done = True
                    # 嘗試從 Alpaca 錯誤 JSON 取得精確的衝突委託單 ID
                    conflicting_id: str | None = None
                    try:
                        err_data       = json.loads(err_str)
                        conflicting_id = err_data.get("existing_order_id")
                    except Exception:
                        pass

                    log.warning(
                        "server stop rejected (wash trade 40310000), "
                        "conflicting_order=%s, cancelling and retrying",
                        conflicting_id or "unknown",
                    )
                    await self.tg.alert(
                        "⚠️ Server Stop 遭 Wash Trade 拒絕（前次殘留委託單衝突）\n"
                        f"衝突單 ID：{conflicting_id or '未知，將掃描所有衝突委託'}\n"
                        "正在清除後重試…",
                        level="WARNING",
                    )
                    if conflicting_id:
                        # 精確取消，快速且無副作用
                        try:
                            await self._cancel_order(conflicting_id)
                            n = 1
                        except Exception as ce:
                            log.warning("cancel conflicting order %s failed: %s", conflicting_id, ce)
                            n = await self._cancel_conflicting_open_orders(stop_side)
                    else:
                        n = await self._cancel_conflicting_open_orders(stop_side)
                    log.info("wash-trade cleanup: cancelled %d conflicting orders", n)
                    await asyncio.sleep(1)  # 等 Alpaca 處理取消
                    continue                # 重試，attempt 不遞增

                if is_insuf_balance and not balance_cleanup_done:
                    # 餘額不足：通常是舊的同向止損單仍佔用 BTC，清除後重試
                    balance_cleanup_done = True
                    log.warning(
                        "server stop insufficient balance — cancelling stale same-side stops and retrying"
                    )
                    await self.tg.alert(
                        "⚠️ Server Stop 餘額不足（舊止損單仍佔用），清除後重試…",
                        level="WARNING",
                    )
                    n = await self._cancel_same_side_stops(stop_side)
                    log.info("balance-recovery: cancelled %d stale same-side stop(s)", n)
                    await asyncio.sleep(1)
                    continue  # 重試，attempt 不遞增

                log.warning("server-side stop attempt %d/3 failed: %s", attempt + 1, e)
                attempt += 1
                if attempt >= 3:
                    # 解析錯誤訊息，避免 Telegram 顯示原始 JSON
                    try:
                        err_data = json.loads(err_str)
                        readable = (
                            f"code={err_data.get('code', '?')}  "
                            f"reason={err_data.get('reject_reason') or err_data.get('message', '?')}"
                        )
                    except Exception:
                        readable = err_str[:300]
                    await self.tg.alert(
                        f"伺服器端停損單送出失敗（3 次重試後放棄）\n{readable}",
                        level="CRITICAL",
                    )
                    return None
                await asyncio.sleep(0.5 * (2 ** (attempt - 1)))

    async def replace_server_side_stop(
        self, old_order_id: str, side: str, qty: float,
        hard_sl_price: float, buffer_pct: float = 0.005,
    ) -> str:
        """
        Trailing SL 上移時呼叫：cancel 舊 server stop，送新的。
        cancel 失敗（可能已觸發）仍嘗試送新單。回傳新 order_id 或 None。
        """
        if old_order_id:
            try:
                await self._cancel_order(old_order_id)
            except Exception as e:
                log.info("cancel old server stop %s failed (may have triggered): %s", old_order_id, e)
        return await self.place_server_side_stop(
            side=side, qty=qty, hard_sl_price=hard_sl_price, buffer_pct=buffer_pct,
        )

    # ── Open-Order Helpers（Wash-Trade 防護用）────────────────────────────────

    async def _get_open_orders(self, symbol: str = "BTC/USD") -> list:
        """取得 Alpaca 上該 symbol 所有開放委託單（非阻塞）。"""
        try:
            req    = GetOrdersRequest(status="open")
            orders = await asyncio.to_thread(self.client.get_orders, req)
            return [o for o in (orders or []) if getattr(o, "symbol", "") == symbol]
        except Exception as e:
            log.warning("get_open_orders failed: %s", e)
            return []

    async def _cancel_open_limit_orders(self, symbol: str = "BTC/USD") -> int:
        """取消所有非 stop 類型的開放委託單（啟動清理，防 wash trade）。
        Stop / stop-limit 委託單保留（可能是上一個 session 的 server stop）。"""
        orders    = await self._get_open_orders(symbol)
        cancelled = 0
        for o in orders:
            order_type = str(getattr(o, "order_type", "") or "").lower()
            if "stop" in order_type:
                continue
            try:
                await self._cancel_order(str(o.id))
                cancelled += 1
                log.info("startup cleanup: cancelled stale %s %s order %s",
                         order_type, getattr(o, "side", "?"), o.id)
            except Exception as e:
                log.warning("cancel stale order %s failed: %s", o.id, e)
        return cancelled

    async def _cancel_conflicting_open_orders(
        self, stop_side: OrderSide, symbol: str = "BTC/USD"
    ) -> int:
        """取消造成 wash trade 的衝突委託單：
        下 SELL stop 時清除 open BUY 委託；下 BUY stop 時清除 open SELL 委託。"""
        conflict_side = OrderSide.BUY if stop_side == OrderSide.SELL else OrderSide.SELL
        orders        = await self._get_open_orders(symbol)
        cancelled     = 0
        for o in orders:
            if getattr(o, "side", None) != conflict_side:
                continue
            try:
                await self._cancel_order(str(o.id))
                cancelled += 1
                log.info("wash-trade cleanup: cancelled %s order %s limit=%s",
                         conflict_side, o.id, getattr(o, "limit_price", "?"))
            except Exception as e:
                log.warning("cancel conflicting order %s failed: %s", o.id, e)
        return cancelled

    async def find_existing_server_stop(
        self, stop_side: OrderSide, symbol: str = "BTC/USD"
    ) -> str | None:
        """Scan open orders for a stop/stop-limit of the same side as we want to place.
        Returns the order ID if found, else None.  Used to adopt orphan server stops
        instead of placing duplicates that would fail with 'insufficient balance'.
        """
        orders = await self._get_open_orders(symbol)
        for o in orders:
            if getattr(o, "side", None) != stop_side:
                continue
            order_type = str(getattr(o, "order_type", "") or "").lower()
            if "stop" in order_type:
                log.info("found existing server stop %s side=%s type=%s", o.id, stop_side, order_type)
                return str(o.id)
        return None

    async def _cancel_same_side_stops(
        self, stop_side: OrderSide, symbol: str = "BTC/USD"
    ) -> int:
        """Cancel all stop/stop-limit orders of the SAME side.
        Called when 'insufficient balance' means a stale stop is already holding the funds.
        """
        orders    = await self._get_open_orders(symbol)
        cancelled = 0
        for o in orders:
            if getattr(o, "side", None) != stop_side:
                continue
            order_type = str(getattr(o, "order_type", "") or "").lower()
            if "stop" not in order_type:
                continue
            try:
                await self._cancel_order(str(o.id))
                cancelled += 1
                log.info("cancelled stale same-side stop %s (balance recovery)", o.id)
            except Exception as e:
                log.warning("cancel stale stop %s failed: %s", o.id, e)
        return cancelled

    # ── Ghost Order 防護 ──────────────────────────────────────────────────────

    async def scan_orphan_orders_on_startup(self):
        log.info("Scanning orphan orders on startup...")

        # 1. 處理 DB 記錄的 pending orders（原有邏輯）
        pending = await self.db.get_unconfirmed_orders()
        if not pending:
            log.info("No DB-recorded pending orders found.")
        for order_id in pending:
            await self._verify_and_handle_order(order_id)

        # 2. 主動清除 Alpaca 上殘留的限價委託單（非 stop 類型）
        #    這些是前次 session 的進場單，在 Reconcile 接管倉位後若不清除，
        #    下 server-side stop 時會觸發 wash trade 拒絕（error 40310000）
        n = await self._cancel_open_limit_orders()
        if n:
            log.info("startup: cancelled %d stale open limit orders (wash-trade prevention)", n)
            await self.tg.alert(
                f"🧹 啟動清理：取消 {n} 張上次殘留的限價委託單\n"
                "（防止後續下 Server Stop 時觸發 Wash Trade 拒絕）",
                level="WARNING",
            )
        else:
            log.info("startup: no stale open limit orders found")

    async def _safe_cancel(self, order_id: str) -> bool:
        try:
            await self._cancel_order(order_id)
            await asyncio.sleep(0.5)
            order = await self._get_order(order_id)
            return order.status in (OrderStatus.CANCELED, OrderStatus.EXPIRED)
        except Exception:
            return False

    async def _verify_and_handle_order(self, order_id: str):
        try:
            order = await self._get_order(order_id)
            if order.status == OrderStatus.FILLED:
                await self.tg.alert(
                    f"訂單 {order_id} cancel 失敗但已成交，觸發 Reconciliation",
                    level="WARNING",
                )
                await self.position_mgr.reconcile_single(order)
                await self.db.confirm_order_filled(order_id)
            elif order.status in (
                OrderStatus.PARTIALLY_FILLED,
                OrderStatus.PENDING_NEW,
                OrderStatus.ACCEPTED,
            ):
                await self.tg.alert(
                    f"訂單 {order_id} 狀態不確定（{order.status}），請手動確認",
                    level="CRITICAL",
                )
            else:
                # CANCELED / EXPIRED / REJECTED — 明確標記，startup 掃描不重處理
                await self.db.dismiss_pending_order(order_id)
        except Exception as e:
            await self.tg.alert(
                f"訂單查詢失敗：{order_id} / {e}", level="CRITICAL"
            )
