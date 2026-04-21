# DEVELOPMENT.md
# BTC/USD 全自動交易系統 — 開發文件

> 架構版本 v7.0 · Pure Price Action / SMC · Alpaca × Claude AI × Telegram  
> 開發階段月費約 **$2–5**（Haiku 4.5 + Prompt Cache + Zeabur Dev Plan）  
> **測試環境：Zeabur 雲端部署（無需本機環境）**
>
> **v7.0 主要變更：**  
> ① 進場解耦：訊號確認立即送限價單，Claude 描述以 `asyncio.create_task` 非同步補寫  
> ② 訂單類型升級：預設 `LimitOrderRequest`（OB/FVG 回踩），消除市價單滑點風險  
> ③ 雙層止損：收盤 INVALIDATED ＋ 盤中 Hard SL 插針防護  
> ④ 雲端伺服器端停損單：進場成交後立即在 Alpaca 送保命底線停損  
> ⑤ 成交確認改為 `trade_updates` WebSocket 事件驅動，取代 REST 輪詢  
> ⑥ Sweep 流動性掠奪辨識（EQH/EQL 假突破 + 長影線）  
> ⑦ BOS / CHoCH 獨立 entry model 設定  
> ⑧ 多項穩定性修正：Reconciliation 窗口、HALF 態熔斷器、_persist 補償、Volume 偵測、DB 持久連線

---

## 目錄

1. [系統架構](#1-系統架構)
2. [雙軌設計原則](#2-雙軌設計原則)
3. [目錄結構](#3-目錄結構)
4. [Alpaca-py SDK 正確用法](#4-alpaca-py-sdk-正確用法)
5. [模組說明](#5-模組說明)
6. [SMC 策略設定](#6-smc-策略設定smc_configyaml)
7. [風控設定](#7-風控設定risk_configyaml)
8. [持倉動態規則](#8-持倉動態規則position_rulesyaml)
9. [費用優化](#9-費用優化)
10. [資料庫結構](#10-資料庫結構)
11. [Telegram 通知規格](#11-telegram-通知規格)
12. [部署（Zeabur）](#12-部署zeabur)
13. [環境變數完整清單](#13-環境變數完整清單)
14. [Zeabur 雲端測試流程](#14-zeabur-雲端測試流程)

---

## 1. 系統架構

```
WebSocket tick (Alpaca CryptoDataStream)
        │
        ▼
┌───────────────────────────────────────────────────────────┐
│                   FAST TRACK  (<10ms)                      │
│                                                           │
│  CandleBuilder → SMCEngine → PositionManager             │
│  (tick→OHLCV)    (BOS/CHoCH   (SL/TP 監視)               │
│                   /OB/FVG     │                           │
│                   /Sweep)     ├─ Hard SL 插針防護（tick 級）│
│                     │         └─ INVALIDATED 收盤直接出場  │
│           smc_engine.get_signal()                         │
│           ← 純 Python 決策 BUY/SELL/HOLD                  │
│                     │                                     │
│  ⚡ 訊號確認 → 立即送 LimitOrder（不等 Claude）             │
│  ⚡ 進場成交 → 立即送伺服器端保底停損單                      │
└─────────────────────┼─────────────────────────────────────┘
                      │ asyncio.create_task（非阻塞）
                      ▼
┌───────────────────────────────────────────────────────────┐
│                   SLOW TRACK  (可接受延遲)                  │
│                                                           │
│  AnalysisQueue → ClaudeClient → DB.save()                │
│  (asyncio佇列)   (結構描述者)   (aiosqlite)                │
│                       │                                   │
│               TelegramBot（推播通知）                      │
└───────────────────────────────────────────────────────────┘

成交確認軌道（獨立）：
  TradingStream (trade_updates WebSocket)
  → OrderFillHandler → PositionManager.on_fill()
  → 觸發 Slow Track（DB 寫入 + Telegram 推播）
```

**設計核心原則：**
- BUY/SELL/HOLD 決策由純 Python SMC 引擎負責，可測試、可回測
- **訊號確認 → 立即送限價單**，Claude 描述為非同步補寫，不影響進場時機
- **雙層止損**：Hard SL（盤中 tick 級觸發）+ INVALIDATED（K棒收盤確認）
- **伺服器端保命停損**：每次進場成交後自動在 Alpaca 送停損單，Zeabur 當機不裸奔
- Claude 角色：結構描述者，不負責 BUY/SELL 決策
- Circuit Breaker 三態：`CLOSED → OPEN → HALF`，狀態持久化至 DB
- 重連後：先訂閱 WebSocket，再執行 Reconciliation（防止 SL 事件在窗口期掉落）
- **當前版本限 1 個 symbol（BTC/USD）、最多 1 筆持倉**

---

## 2. 雙軌設計原則

| | Fast Track | Slow Track |
|---|---|---|
| **職責** | tick 接收、K棒聚合、SMC 決策、SL/TP 監視、Hard SL 插針防護、限價單送出、伺服器端停損單送出、INVALIDATED 直接出場 | Claude 結構描述、DB 寫入、Telegram 推播 |
| **延遲要求** | < 10ms | 300ms～3s 可接受 |
| **容錯** | WS 自動重連（指數退避）→ 先訂閱 → 再 Reconciliation | asyncio Queue 緩衝，失敗可重試 |
| **決策責任** | ✅ SMC 純 Python，結果可重現 | ❌ Claude 不負責 BUY/SELL |
| **DB 寫入** | 觸發非同步 task（不阻塞 event loop） | aiosqlite 持久連線 + asyncio.Lock |

---

## 3. 目錄結構

```
btcusd-bot/
│
├── config/
│   ├── smc_config.yaml
│   ├── risk_config.yaml
│   └── position_rules.yaml
│
├── src/
│   ├── core/
│   │   ├── feed.py              # CryptoDataStream + TradingStream + 自動重連 + Reconciliation
│   │   ├── candle_builder.py    # M1 bar → M15/H1/H4 聚合
│   │   ├── smc_engine.py        # SMC 純計算引擎（BOS/CHoCH 方向分離 + Sweep 辨識）
│   │   ├── position_manager.py  # 持倉生命週期（Hard SL + 結構 Trailing Stop + HTF 翻轉 hook）
│   │   └── order_executor.py    # TradingClient 下單（LimitOrder）+ 伺服器端停損 + 幽靈訂單防護
│   │
│   ├── ai/
│   │   ├── analysis_queue.py
│   │   ├── claude_client.py
│   │   ├── prompt_builder.py
│   │   └── response_parser.py
│   │
│   ├── risk/
│   │   ├── risk_manager.py
│   │   └── circuit_breaker.py   # 三態熔斷器（狀態持久化 + HALF 態修正）
│   │
│   ├── notification/
│   │   └── telegram_bot.py      # 差異化心跳 + TP1 剩餘持倉快照 + 單 URL 斷線告警
│   │
│   └── storage/
│       ├── db.py                # aiosqlite 持久連線 + asyncio.Lock + timeout=5.0
│       └── schema.sql
│
├── main.py
├── init_db.py
├── requirements.txt
├── .env.example
├── .gitignore
├── zeabur.json
├── DEVELOPMENT.md
└── README.md
```

---

## 4. Alpaca-py SDK 正確用法

> ⚠️ **本系統使用新版 `alpaca-py`，不使用已棄用的 `alpaca-trade-api`。**  
> 兩者 import 路徑不同，混用會導致 `ModuleNotFoundError`。

### 4.1 安裝

```
pip install alpaca-py
```

### 4.2 四個核心 Client

| Client | 用途 | Import |
|--------|------|--------|
| `TradingClient` | 下單、查持倉、查帳戶、取消訂單 | `from alpaca.trading.client import TradingClient` |
| `CryptoDataStream` | 即時 WebSocket tick / bar 串流 | `from alpaca.data.live import CryptoDataStream` |
| `TradingStream` | 即時 WebSocket 訂單狀態（`trade_updates`） | `from alpaca.trading.stream import TradingStream` |
| `CryptoHistoricalDataClient` | 歷史 K 棒（HTF/MTF 結構初始化） | `from alpaca.data.historical import CryptoHistoricalDataClient` |

### 4.3 正確初始化

```python
import os
from alpaca.trading.client import TradingClient
from alpaca.data.live import CryptoDataStream
from alpaca.trading.stream import TradingStream
from alpaca.data.historical import CryptoHistoricalDataClient

API_KEY    = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
IS_PAPER   = os.getenv("ALPACA_PAPER_MODE", "true").lower() == "true"

# 交易客戶端（下單 / 查帳戶 / 查持倉）
trading_client = TradingClient(
    api_key=API_KEY,
    secret_key=SECRET_KEY,
    paper=IS_PAPER
)

# 即時 WebSocket 串流（K棒 + tick）
crypto_stream = CryptoDataStream(
    api_key=API_KEY,
    secret_key=SECRET_KEY
)

# 即時 WebSocket 串流（訂單成交狀態）
trading_stream = TradingStream(
    api_key=API_KEY,
    secret_key=SECRET_KEY,
    paper=IS_PAPER
)

# 歷史資料
hist_client = CryptoHistoricalDataClient(
    api_key=API_KEY,
    secret_key=SECRET_KEY
)
```

> **重要：Paper 金鑰（前綴 `PK...`）與 Live 金鑰（前綴 `AK...`）是不同組。**

### 4.4 限價單進場（預設）

```python
from alpaca.trading.requests import LimitOrderRequest, StopLimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

# 預設：限價單（OB/FVG 回踩目標價）
def make_limit_order(side: str, notional: float, limit_price: float) -> LimitOrderRequest:
    return LimitOrderRequest(
        symbol="BTC/USD",
        notional=str(round(notional, 2)),
        side=OrderSide.BUY if side == "BUY" else OrderSide.SELL,
        time_in_force=TimeInForce.GTC,
        limit_price=str(round(limit_price, 2)),
    )

# 備援：Stop-Limit（極端行情突破後追蹤進場）
def make_stop_limit_order(side: str, notional: float,
                           stop_price: float, limit_price: float) -> StopLimitOrderRequest:
    return StopLimitOrderRequest(
        symbol="BTC/USD",
        notional=str(round(notional, 2)),
        side=OrderSide.BUY if side == "BUY" else OrderSide.SELL,
        time_in_force=TimeInForce.GTC,
        stop_price=str(round(stop_price, 2)),
        limit_price=str(round(limit_price, 2)),
    )
```

> **為何不用市價單：** SMC 策略依賴 OB/FVG 回踩精確入場，市價單在 M15 加密貨幣市場可能造成數十至數百美元滑點，直接破壞精算的 RRR。

### 4.5 伺服器端保底停損單

```python
from alpaca.trading.requests import StopOrderRequest

def make_server_side_stop(side: str, qty: float, hard_sl_price: float) -> StopOrderRequest:
    """
    進場成交後立即送出，作為 Zeabur 當機時的最終保護。
    stop_price = Hard SL 外再擴展 0.5%（由 risk_config.yaml server_side_stop_buffer_pct 控制）
    """
    stop_side = OrderSide.SELL if side == "BUY" else OrderSide.BUY
    return StopOrderRequest(
        symbol="BTC/USD",
        qty=str(round(qty, 8)),
        side=stop_side,
        time_in_force=TimeInForce.GTC,
        stop_price=str(round(hard_sl_price, 2)),
    )
```

> ⚠️ **此停損單必須在 Alpaca 伺服器保持存活**，請勿在後台手動刪除。  
> 系統平倉（TP/INVALIDATED/Trailing）時會自動取消此單；若 Reconciliation 發現孤兒停損單，會一併清理。

### 4.6 trade_updates WebSocket（取代輪詢）

```python
from alpaca.trading.stream import TradingStream
from alpaca.trading.models import TradeUpdate

trading_stream = TradingStream(
    api_key=API_KEY,
    secret_key=SECRET_KEY,
    paper=IS_PAPER
)

async def on_trade_update(update: TradeUpdate):
    """
    事件驅動成交確認，取代 _wait_fill REST 輪詢。
    update.event: 'fill' / 'partial_fill' / 'canceled' / 'expired' / 'pending_new'
    update.order.id, update.order.status, update.order.filled_avg_price
    """
    await order_fill_handler.handle(update)

trading_stream.subscribe_trade_updates(on_trade_update)
# 與 CryptoDataStream 分開 run，在獨立 asyncio task 中啟動
```

### 4.7 查詢持倉（Reconciliation 使用）

```python
positions = trading_client.get_all_positions()

try:
    pos = trading_client.get_open_position("BTC/USD")
except Exception:
    pos = None
```

### 4.8 取消訂單與查詢狀態

```python
from alpaca.trading.enums import OrderStatus

trading_client.cancel_order_by_id(order_id)

order = trading_client.get_order_by_id(order_id)
# order.status: filled / canceled / expired / pending_new / accepted / partially_filled
```

### 4.9 取歷史 K 棒（系統啟動時初始化 HTF 結構）

```python
from alpaca.data.historical import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame
from datetime import datetime, timezone, timedelta

hist_client = CryptoHistoricalDataClient(api_key=API_KEY, secret_key=SECRET_KEY)

# alpaca-py 的 TimeFrame 支援：Minute / Hour / Day
# H4 / M15 等非標準 timeframe 需在 candle_builder 自行聚合

req = CryptoBarsRequest(
    symbol_or_symbols="BTC/USD",
    timeframe=TimeFrame.Hour,
    start=datetime.now(timezone.utc) - timedelta(days=10),
    limit=200,
)
bars = hist_client.get_crypto_bars(req)
df = bars.df
```

---

## 5. 模組說明

### 5.1 `src/core/feed.py`

```python
import asyncio, os
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
        self.trading_client   = TradingClient(
            api_key=self.api_key,
            secret_key=self.secret_key,
            paper=self.is_paper
        )

    async def start(self):
        # TradingStream（trade_updates）在獨立 task 啟動，不受 CryptoDataStream 重連影響
        asyncio.create_task(self._run_trading_stream())

        attempt = 0
        while True:
            try:
                await self._connect_and_stream()
                attempt = 0
            except Exception as e:
                delay = RECONNECT_DELAYS[min(attempt, len(RECONNECT_DELAYS)-1)]
                if self.position_mgr.has_open_positions():
                    # P2 修正：只顯示當前模式 URL
                    dashboard_url = os.getenv("ALPACA_DASHBOARD_URL", "")
                    await self.tg.alert(
                        f"🚨 WS 斷線！持倉中！{delay}s 後重連（第{attempt+1}次）\n{dashboard_url}",
                        level="CRITICAL"
                    )
                await asyncio.sleep(delay)
                attempt += 1

    async def _run_trading_stream(self):
        """TradingStream 獨立重連迴圈"""
        attempt = 0
        while True:
            try:
                stream = TradingStream(
                    api_key=self.api_key,
                    secret_key=self.secret_key,
                    paper=self.is_paper
                )
                stream.subscribe_trade_updates(self.on_trade_update)
                await stream.run()
            except Exception as e:
                delay = RECONNECT_DELAYS[min(attempt, len(RECONNECT_DELAYS)-1)]
                await asyncio.sleep(delay)
                attempt += 1

    async def _connect_and_stream(self):
        stream = CryptoDataStream(api_key=self.api_key, secret_key=self.secret_key)
        stream.subscribe_bars(self._on_bar, "BTC/USD")
        stream.subscribe_trades(self._on_trade, "BTC/USD")

        # P0-2 修正：先完成訂閱（stream 已連線），再執行 Reconciliation
        # 確保 Reconciliation 執行期間 tick/bar 回調已就緒，SL 事件不會掉落
        asyncio.create_task(self._deferred_reconcile())
        await stream.run()

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
        except Exception as e:
            await self.tg.alert(f"🔴 Reconcile 失敗：{e}", level="CRITICAL")
```

---

### 5.2 `src/core/order_executor.py`

```python
import asyncio, os
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    LimitOrderRequest, StopLimitOrderRequest, StopOrderRequest
)
from alpaca.trading.enums import OrderSide, TimeInForce, OrderStatus

class OrderExecutor:
    def __init__(self, position_mgr, db, tg):
        self.client = TradingClient(
            api_key=os.getenv("ALPACA_API_KEY"),
            secret_key=os.getenv("ALPACA_SECRET_KEY"),
            paper=os.getenv("ALPACA_PAPER_MODE", "true").lower() == "true"
        )
        self.position_mgr = position_mgr
        self.db  = db
        self.tg  = tg
        # fill_events: order_id → asyncio.Event，由 trade_updates WS 觸發
        self.fill_events: dict[str, asyncio.Event] = {}
        self.fill_results: dict[str, object]        = {}

    # ──────────────────────────────────────────────
    # trade_updates 事件驅動（P0-新4：取代 REST 輪詢）
    # ──────────────────────────────────────────────
    async def on_trade_update(self, update):
        """由 TradingStream 回調，觸發對應的 fill_event"""
        order_id = str(update.order.id)
        if order_id in self.fill_events:
            self.fill_results[order_id] = update
            self.fill_events[order_id].set()

    async def _wait_fill_event(self, order_id: str, timeout: int):
        """等待 trade_updates WS 推送成交事件，取代 REST 輪詢"""
        event = asyncio.Event()
        self.fill_events[order_id] = event
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
            update = self.fill_results.pop(order_id, None)
            if update and update.order.status == OrderStatus.FILLED:
                return update.order
            raise OrderError(f"訂單非成交狀態：{update.order.status if update else 'unknown'}")
        except asyncio.TimeoutError:
            raise asyncio.TimeoutError(f"等待成交超時：{order_id}")
        finally:
            self.fill_events.pop(order_id, None)

    # ──────────────────────────────────────────────
    # 進場（P0-新2：限價單預設；P0-新1：呼叫方不阻塞 Claude）
    # ──────────────────────────────────────────────
    async def place(self, side: str, notional: float,
                    limit_price: float,
                    stop_price: float = None) -> dict:
        """
        訊號確認後立即呼叫，不等 Claude 描述。
        limit_price：OB/FVG 回踩目標價
        stop_price：若提供則改用 Stop-Limit（備援模式）
        """
        if stop_price:
            req = StopLimitOrderRequest(
                symbol="BTC/USD",
                notional=str(round(notional, 2)),
                side=OrderSide.BUY if side == "BUY" else OrderSide.SELL,
                time_in_force=TimeInForce.GTC,
                stop_price=str(round(stop_price, 2)),
                limit_price=str(round(limit_price, 2)),
            )
        else:
            req = LimitOrderRequest(
                symbol="BTC/USD",
                notional=str(round(notional, 2)),
                side=OrderSide.BUY if side == "BUY" else OrderSide.SELL,
                time_in_force=TimeInForce.GTC,
                limit_price=str(round(limit_price, 2)),
            )

        for attempt in range(3):
            try:
                order = self.client.submit_order(req)
                await self.db.record_pending_order(order.id, side, notional)
                result = await self._wait_fill_event(order.id, timeout=10)
                await self.db.confirm_order_filled(order.id)
                return result
            except asyncio.TimeoutError:
                cancelled = await self._safe_cancel(order.id)
                if not cancelled:
                    await self._verify_and_handle_order(order.id)
                if attempt == 2:
                    raise OrderError(f"下單超時（3次重試後）: {order.id}")
                await asyncio.sleep(0.5 * (2 ** attempt))
            except Exception as e:
                if attempt == 2:
                    raise OrderError(f"下單失敗：{e}")
                await asyncio.sleep(0.5 * (2 ** attempt))

    # ──────────────────────────────────────────────
    # 平倉（P0-1 修正：加入幽靈訂單防護）
    # ──────────────────────────────────────────────
    async def close_position(self, pos) -> dict:
        """
        P0-1 修正：平倉也記錄至 pending_orders，防止網路瞬斷造成幽靈持倉。
        平倉前先取消伺服器端保底停損單。
        """
        # 取消伺服器端保底停損
        if hasattr(pos, 'server_stop_order_id') and pos.server_stop_order_id:
            try:
                self.client.cancel_order_by_id(pos.server_stop_order_id)
            except Exception:
                pass  # 若已被觸發則忽略

        close_order_id = None
        for attempt in range(3):
            try:
                order = self.client.close_position("BTC/USD")
                close_order_id = order.id
                await self.db.record_pending_order(order.id, "CLOSE", 0)
                result = await self._wait_fill_event(order.id, timeout=15)
                await self.db.confirm_order_filled(order.id)
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

    # ──────────────────────────────────────────────
    # 伺服器端保底停損（P1-新1）
    # ──────────────────────────────────────────────
    async def place_server_side_stop(self, side: str, qty: float,
                                      hard_sl_price: float,
                                      buffer_pct: float = 0.005) -> str:
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
            symbol="BTC/USD",
            qty=str(round(qty, 8)),
            side=stop_side,
            time_in_force=TimeInForce.GTC,
            stop_price=str(stop_price),
        )
        try:
            order = self.client.submit_order(req)
            return str(order.id)
        except Exception as e:
            await self.tg.alert(f"⚠️ 伺服器端停損單送出失敗：{e}", level="WARNING")
            return None

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
                    level="WARNING"
                )
                await self.position_mgr.reconcile_single(order)
            elif order.status in (
                OrderStatus.PARTIALLY_FILLED,
                OrderStatus.PENDING_NEW,
                OrderStatus.ACCEPTED,
            ):
                await self.tg.alert(
                    f"🔴 訂單 {order_id} 狀態不確定（{order.status}），請手動確認",
                    level="CRITICAL"
                )
        except Exception as e:
            await self.tg.alert(f"🔴 訂單查詢失敗：{order_id} / {e}", level="CRITICAL")

    async def scan_orphan_orders_on_startup(self):
        pending = await self.db.get_unconfirmed_orders()
        for order_id in pending:
            await self._verify_and_handle_order(order_id)
```

---

### 5.3 `src/core/position_manager.py`（關鍵片段）

**雙層止損：Hard SL（盤中）+ INVALIDATED（收盤）：**

```python
# ──────────────────────────────────────────────
# P0-新3：盤中 Hard SL 插針防護（tick 級）
# ──────────────────────────────────────────────
async def on_tick(self, symbol: str, price: float):
    pos = self.positions.get(symbol)
    if not pos or pos.state != PositionState.OPEN:
        return

    # Hard SL：不等收盤，盤中觸發立即市價出場
    if pos.is_hard_sl_triggered(price):
        pos.state = PositionState.CLOSING
        asyncio.create_task(self._execute_hard_sl_close(pos, price))

async def _execute_hard_sl_close(self, pos, trigger_price: float):
    """盤中插針防護：直接市價出場，不進 Slow Track"""
    try:
        order = await self.executor.close_position(pos)
        self.circuit.record("HARD_SL")
        asyncio.create_task(self.db.close_position(pos, "HARD_SL", order))
        asyncio.create_task(self.tg.notify_close(pos, "HARD_SL",
                                                  extra=f"插針觸發價：${trigger_price:,.0f}"))
    except Exception as e:
        await self.tg.alert(f"🔴 Hard SL 出場失敗：{e}", level="CRITICAL")

# ──────────────────────────────────────────────
# INVALIDATED 收盤確認（原有，保留）
# ──────────────────────────────────────────────
async def on_candle_close(self, symbol: str, candle: dict):
    pos = self.positions.get(symbol)
    if not pos or pos.state != PositionState.OPEN:
        return

    # HTF 偏向翻轉 hook（P1-3）
    if self.smc.htf_bias_changed(symbol):
        await self._handle_htf_bias_flip(pos, symbol)

    if pos.is_invalidated(candle["close"]):
        pos.state = PositionState.CLOSING
        asyncio.create_task(self._execute_invalidated_close(pos, candle))
        return

    if pos.state == PositionState.TRAILING:
        old_sl = pos.stop_loss
        pos.update_structural_trailing(candle)
        # 止損移動時觸發差異化心跳（P2）
        if pos.stop_loss != old_sl:
            asyncio.create_task(self.tg.mark_state_changed("SL_MOVED"))

async def _handle_htf_bias_flip(self, pos, symbol: str):
    """HTF 偏向翻轉：推播告警，若持倉方向與新 HTF 偏向相反則條件平倉"""
    new_bias = self.smc.get_htf_bias(symbol)
    pos_side = pos.side  # "BUY" or "SELL"
    if (pos_side == "BUY" and new_bias == "BEARISH") or \
       (pos_side == "SELL" and new_bias == "BULLISH"):
        await self.tg.alert(
            f"⚠️ HTF 偏向翻轉為 {new_bias}，持倉方向衝突！已提前出場",
            level="WARNING"
        )
        pos.state = PositionState.CLOSING
        asyncio.create_task(self._execute_htf_flip_close(pos))
    else:
        await self.tg.alert(
            f"ℹ️ HTF 偏向翻轉為 {new_bias}，持倉方向一致，繼續持有",
            level="INFO"
        )

async def _execute_invalidated_close(self, pos, candle):
    try:
        order = await self.executor.close_position(pos)
        self.circuit.record("INVALIDATED")
        asyncio.create_task(self.db.close_position(pos, "INVALIDATED", order))
        asyncio.create_task(self.tg.notify_close(pos, "INVALIDATED"))
    except Exception as e:
        await self.tg.alert(f"🔴 INVALIDATED 平倉失敗：{e}", level="CRITICAL")
```

**`Position.is_hard_sl_triggered()` 實作：**

```python
def is_hard_sl_triggered(self, current_price: float) -> bool:
    """
    盤中插針防護：若價格穿越 hard_sl_price 則觸發（不等收盤）。
    hard_sl_price 由 risk_config.yaml 的 hard_sl_buffer_pct 控制，
    設定在結構止損（stop_loss）外的緩衝區。
    例：stop_loss = $93,000，hard_sl_buffer_pct = 0.003
    → hard_sl_price(BUY) = $93,000 * (1 - 0.003) = $92,721
    """
    if self.side == "BUY":
        return current_price <= self.hard_sl_price
    else:
        return current_price >= self.hard_sl_price
```

**結構 Trailing Stop（`_detect_swing` 明確定義，P1-2）：**

```python
def update_structural_trailing(self, candle: dict):
    """
    跟蹤 LTF Swing Low/High，而非固定百分比。
    Swing 判斷：以 N 根 K 棒 Pivot Point 確認（N 由 position_rules.yaml 的
    trailing_pivot_bars 控制，預設 3，即左右各 3 根均低於/高於中間根才確認）。
    """
    swing = self._detect_swing(candle)
    if not swing:
        return
    if self.side == "BUY":
        candidate = swing["low"] - self.structural_buffer
        if (candidate > self.stop_loss and
                (candidate - self.stop_loss) >= self.entry_price * self.min_trail_pct):
            self.stop_loss = candidate
    elif self.side == "SELL":
        candidate = swing["high"] + self.structural_buffer
        if (candidate < self.stop_loss and
                (self.stop_loss - candidate) >= self.entry_price * self.min_trail_pct):
            self.stop_loss = candidate

def _detect_swing(self, latest_candle: dict) -> dict | None:
    """
    Pivot Point 確認（trailing_pivot_bars = N，預設 3）：
    - 多頭 Swing Low：過去 N 根中，中間根的 low 低於左右各 N/2 根
    - 空頭 Swing High：過去 N 根中，中間根的 high 高於左右各 N/2 根
    將 latest_candle 加入滾動窗口（長度 = 2*N+1），若中間根符合 Pivot 條件則回傳。
    快速行情中所有 K 棒均創新高/低時，無 Pivot 確認 → 回傳 None → Stop 不移動。
    """
    N = self.trailing_pivot_bars  # 來自 position_rules.yaml，預設 3
    self._candle_buffer.append(latest_candle)
    if len(self._candle_buffer) < 2 * N + 1:
        return None

    window = list(self._candle_buffer)[-(2 * N + 1):]
    pivot  = window[N]
    left   = window[:N]
    right  = window[N+1:]

    if all(pivot["low"] < c["low"] for c in left + right):
        return {"low": pivot["low"]}
    if all(pivot["high"] > c["high"] for c in left + right):
        return {"high": pivot["high"]}
    return None
```

---

### 5.4 `src/core/smc_engine.py`（BOS/CHoCH 方向分離 + Sweep）

**BOS / CHoCH 方向分離（保留原有邏輯）：**

```python
def _check_ltf_trigger_aligned(self) -> bool:
    """
    HTF BULLISH → 接受 BULLISH_BOS 或 BULLISH_CHOCH
    HTF BEARISH → 接受 BEARISH_BOS 或 BEARISH_CHOCH
    反向信號 → HOLD
    """
    if self.htf_bias == "BULLISH":
        return self.ltf_trigger in ("BULLISH_BOS", "BULLISH_CHOCH")
    elif self.htf_bias == "BEARISH":
        return self.ltf_trigger in ("BEARISH_BOS", "BEARISH_CHOCH")
    return False
```

**Sweep 流動性掠奪辨識（P1-新3）：**

```python
def _check_sweep(self, candle: dict, eqh_eql_levels: list) -> dict | None:
    """
    偵測 EQH/EQL 假突破（Sweep）+ 長影線確認，作為高勝率反向進場點。

    觸發條件：
    1. K棒盤中突破前高（EQH）或前低（EQL）
    2. K棒收盤回到突破點內側（假突破確認）
    3. 影線長度 ≥ 實體長度的 sweep_wick_ratio 倍（預設 2.0，可設定）

    回傳：{'direction': 'BULLISH'|'BEARISH', 'swept_level': float, 'strength': float}
    """
    body = abs(candle["close"] - candle["open"])
    if body == 0:
        return None

    # 看多 Sweep：打穿前低後收回（長下影線）
    lower_wick = min(candle["open"], candle["close"]) - candle["low"]
    for level in eqh_eql_levels:
        if (level["type"] == "EQL" and
                candle["low"] < level["price"] and          # 盤中破低
                candle["close"] > level["price"] and        # 收盤收回
                lower_wick >= body * self.sweep_wick_ratio):
            return {
                "direction": "BULLISH",
                "swept_level": level["price"],
                "strength": lower_wick / body
            }

    # 看空 Sweep：打穿前高後收回（長上影線）
    upper_wick = candle["high"] - max(candle["open"], candle["close"])
    for level in eqh_eql_levels:
        if (level["type"] == "EQH" and
                candle["high"] > level["price"] and         # 盤中破高
                candle["close"] < level["price"] and        # 收盤收回
                upper_wick >= body * self.sweep_wick_ratio):
            return {
                "direction": "BEARISH",
                "swept_level": level["price"],
                "strength": upper_wick / body
            }
    return None
```

**HTF 偏向變更偵測：**

```python
def htf_bias_changed(self, symbol: str) -> bool:
    """回傳本次 update 後 HTF 偏向是否翻轉"""
    return self._htf_bias_flipped.get(symbol, False)

def get_htf_bias(self, symbol: str) -> str:
    return self._htf_bias.get(symbol, "NEUTRAL")
```

---

### 5.5 `src/risk/circuit_breaker.py`（持久化 + HALF 態修正）

```python
async def load_state(self, db):
    """啟動時從 DB 恢復熔斷器狀態"""
    row = await db.get_circuit_breaker_state()
    if row:
        self.state       = BreakerState[row["state"]]
        self.loss_streak = row["loss_streak"]
        self.opened_at   = row["opened_at"]
        if self.state == BreakerState.OPEN:
            elapsed_h = (datetime.utcnow() - self.opened_at).total_seconds() / 3600
            if elapsed_h >= self.pause_hours:
                self.state = BreakerState.HALF
                await self._persist(db)

def record(self, close_reason: str):
    """
    P0-3 修正：HALF 態試單失敗時，重置 loss_streak = 1（記錄本次失敗），
    而非延用舊值，避免「永遠無法恢復」的狀態鎖。
    """
    if close_reason in ("SL", "INVALIDATED", "TRAILING_SL", "HARD_SL"):
        if self.state == BreakerState.HALF:
            # HALF 態試單失敗：回到 OPEN，重置連虧計數（從 1 開始，記本次）
            self.state       = BreakerState.OPEN
            self.opened_at   = datetime.utcnow()
            self.loss_streak = 1   # 修正：不延用舊值
        else:
            self.loss_streak += 1
            if self.loss_streak >= self.max_losses:
                self.state     = BreakerState.OPEN
                self.opened_at = datetime.utcnow()
    else:
        self.loss_streak = 0
        if self.state == BreakerState.HALF:
            self.state = BreakerState.CLOSED
    asyncio.create_task(self._persist_with_fallback())

async def _persist_with_fallback(self):
    """
    P0-4 修正：await 等待 DB 寫入完成；失敗時強制切換為 OPEN 熔斷器並告警，
    防止 DB 故障後以錯誤狀態重啟繼續交易。
    """
    try:
        await self._persist(self.db)
    except Exception as e:
        # DB 寫入失敗：強制 OPEN 以最保守姿態運行
        self.state = BreakerState.OPEN
        if self.tg:
            await self.tg.alert(
                f"🔴 熔斷器 DB 持久化失敗：{e}\n已強制切換為 OPEN 狀態",
                level="CRITICAL"
            )
```

---

### 5.6 `src/storage/db.py`（持久連線 + timeout + asyncio.Lock）

```python
import aiosqlite, asyncio, os
from datetime import datetime

class Database:
    def __init__(self):
        self.path = os.getenv("DB_PATH", "/app/data/trading.db")
        self._conn: aiosqlite.Connection = None
        self._lock = asyncio.Lock()

    async def connect(self):
        """
        P2 修正：持久連線，避免每次 CRUD 重新 open/close。
        P1-新2：timeout=5.0 防止高頻寫入時 database is locked 錯誤。
        """
        self._conn = await aiosqlite.connect(
            self.path,
            timeout=5.0,
            isolation_level=None  # autocommit，配合 asyncio.Lock 手動管理
        )
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")

    async def close(self):
        if self._conn:
            await self._conn.close()

    async def _execute(self, sql: str, params: tuple = ()):
        """P2 修正：asyncio.Lock 序列化所有寫入，防止並發競態"""
        async with self._lock:
            await self._conn.execute(sql, params)
            await self._conn.commit()

    async def save_circuit_breaker_state(self, state, loss_streak, opened_at):
        await self._execute(
            """INSERT OR REPLACE INTO circuit_breaker_state
               (id, state, loss_streak, opened_at, updated_at)
               VALUES (1,?,?,?,?)""",
            (state, loss_streak,
             opened_at.isoformat() if opened_at else None,
             datetime.utcnow().isoformat())
        )

    async def get_circuit_breaker_state(self) -> dict:
        async with self._lock:
            self._conn.row_factory = aiosqlite.Row
            cur = await self._conn.execute(
                "SELECT * FROM circuit_breaker_state WHERE id=1"
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def record_pending_order(self, order_id, side, notional):
        await self._execute(
            """INSERT OR IGNORE INTO pending_orders
               (order_id, side, notional_usd, submitted_at)
               VALUES (?,?,?,?)""",
            (order_id, side, notional, datetime.utcnow().isoformat())
        )

    async def get_unconfirmed_orders(self) -> list:
        async with self._lock:
            cur = await self._conn.execute(
                "SELECT order_id FROM pending_orders WHERE confirmed=0"
            )
            return [r[0] for r in await cur.fetchall()]

    async def confirm_order_filled(self, order_id: str):
        await self._execute(
            "UPDATE pending_orders SET confirmed=1, confirmed_at=? WHERE order_id=?",
            (datetime.utcnow().isoformat(), order_id)
        )
```

---

### 5.7 `main.py`

```python
import asyncio, os
from src.core.feed import DataFeed
from src.core.smc_engine import SMCEngine
from src.core.position_manager import PositionManager
from src.core.order_executor import OrderExecutor
from src.ai.analysis_queue import AnalysisQueue
from src.ai.claude_client import ClaudeClient
from src.risk.circuit_breaker import CircuitBreaker
from src.risk.risk_manager import RiskManager
from src.notification.telegram_bot import TelegramNotifier
from src.storage.db import Database

KNOWN_VOLUME_PATHS = ["/app/data"]  # Zeabur Volume 掛載點

class TradingSystem:
    def __init__(self):
        self.db      = Database()
        self.circuit = CircuitBreaker(db=self.db)
        self.tg      = TelegramNotifier()
        self.smc     = SMCEngine()
        self.risk    = RiskManager()
        self.position_mgr = PositionManager(
            on_close=self._on_position_close,
            db=self.db, tg=self.tg, smc=self.smc
        )
        self.executor = OrderExecutor(
            position_mgr=self.position_mgr, db=self.db, tg=self.tg
        )
        self.claude   = ClaudeClient(config={})
        self.ai_queue = AnalysisQueue(max_size=50)
        self.feed     = DataFeed(
            on_tick=self.position_mgr.on_tick,
            on_candle_close=self.on_candle_close,
            on_trade_update=self.executor.on_trade_update,
            position_mgr=self.position_mgr,
            tg=self.tg,
        )

    async def startup(self):
        # P0-5：Volume 存活檢查
        await self._check_volume()
        await self.db.connect()
        await self.circuit.load_state(self.db)
        await self.executor.scan_orphan_orders_on_startup()
        is_paper = os.getenv("ALPACA_PAPER_MODE", "true").lower() == "true"
        await self.tg.notify_startup(self.circuit.state,
                                      mode="PAPER" if is_paper else "LIVE")

    async def _check_volume(self):
        """
        P0-5：偵測 /app/data 是否在已知掛載點。
        若 DB 路徑不在 Volume 下，強制 OPEN 熔斷器並推播告警。
        """
        db_path = os.getenv("DB_PATH", "/app/data/trading.db")
        if not any(db_path.startswith(p) for p in KNOWN_VOLUME_PATHS):
            await self.tg.alert(
                f"🔴 [BOOT] Volume 異常：DB 路徑 {db_path} 不在已知掛載點！\n"
                f"熔斷器強制設為 OPEN，請確認 Zeabur Volume 掛載。",
                level="CRITICAL"
            )
            self.circuit.state = BreakerState.OPEN
        else:
            print(f"[BOOT] Volume check OK: {os.path.dirname(db_path)} is mounted")

    async def on_candle_close(self, symbol, tf, candle):
        ctx = self.smc.update(symbol, tf, candle)

        # P1-3：HTF 偏向翻轉由 position_manager 在 on_candle_close 中處理
        # 此處只處理 LTF 訊號候選
        if tf != self.smc.ltf or not ctx.has_candidate():
            return
        if self.circuit.is_open():
            return
        await self.ai_queue.enqueue(symbol, ctx)

    async def _process_ai_job(self, job):
        """
        P0-新1：下單與 Claude 描述完全解耦。
        訊號確認 → 立即送限價單 → 進場成交後送伺服器端停損 → Claude 非同步補寫。
        """
        notional    = self.risk.calc_notional(job.signal)
        limit_price = job.signal.entry_limit_price

        # ① 立即送限價單（不等 Claude）
        order = await self.executor.place(
            job.signal.direction, notional, limit_price
        )
        if not order:
            return

        # ② 進場成交 → 開立持倉
        pos = await self.position_mgr.open(
            job.symbol, job.signal, order, analysis_id=None
        )

        # ③ 立即送伺服器端保底停損（P1-新1）
        server_stop_id = await self.executor.place_server_side_stop(
            side=job.signal.direction,
            qty=float(order.filled_qty),
            hard_sl_price=job.signal.hard_sl_price,
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
            await self.db.update_position_analysis_id(pos.id, analysis_id)
            await self.tg.notify_trade_open(job.symbol, job.signal, description)
        except Exception as e:
            await self.tg.alert(f"⚠️ Slow Track 記錄失敗：{e}", level="WARNING")

    async def _on_position_close(self, pos, reason):
        if reason in ("INVALIDATED", "HARD_SL", "HTF_FLIP"):
            return  # 已由 position_manager 獨立處理
        await self.executor.close_position(pos)
        self.circuit.record(reason)
        await self.db.close_position(pos, reason)
        await self.tg.notify_close(pos, reason)

    async def _heartbeat_loop(self):
        """
        P1-4 修正：sleep(14400)，與文件「每 4 小時強制一次」一致。
        P2 修正：差異化觸發條件明確定義（由 TelegramNotifier 內部狀態管理）。
        觸發條件：① 持倉浮損變化 ±$50 ② 熔斷器狀態改變 ③ SL 被移動
        其餘靜默，每 4 小時強制推播一次。
        """
        last_forced = asyncio.get_event_loop().time()
        while True:
            await asyncio.sleep(300)  # 每 5 分鐘檢查一次差異化條件
            now = asyncio.get_event_loop().time()
            force = (now - last_forced) >= 14400  # 4 小時強制
            if force or self.tg.has_meaningful_state_change(self._build_snapshot()):
                await self.tg.notify_heartbeat(self._build_snapshot())
                if force:
                    last_forced = now

    async def run(self):
        print("Trading System v7.0 starting...")
        await self.startup()
        await asyncio.gather(
            self.feed.start(),
            self.ai_queue.worker(self._process_ai_job),
            self._heartbeat_loop(),
        )

if __name__ == "__main__":
    system = TradingSystem()
    asyncio.run(system.run())
```

---

## 6. SMC 策略設定（`smc_config.yaml`）

```yaml
version: "7.0"

timeframes:
  htf: "H4"
  mtf: "H1"
  ltf: "M15"

htf_bars: 200
ltf_bars: 100

structure:
  bos:
    min_swing_bars: 5
  choch:
    require_displacement: true
    displacement_min_candles: 2        # CHoCH 專屬：最少需要 N 根位移 K 棒確認（P1-5）

liquidity:
  order_block:
    enabled: true
    max_age_bars: 30                   # P2 修正：從 50 縮短至 30（M15 = 7.5 小時）
    min_body_ratio: 0.6
    mitigation_type: close
    wick_penetration_threshold: 0.7    # P2 新增：指針穿越 OB 70% 以上視為緩解
  fair_value_gap:
    enabled: true
    min_gap_usd: 50
    partial_fill_threshold: 0.5
  equal_highs_lows:
    enabled: true
    tolerance_usd: 150                 # P2 建議：BTC 日波幅 3000-5000 USD 時，100 太窄
  breaker_block:
    enabled: true
  sweep:                               # P1-新3：Sweep 流動性掠奪辨識
    enabled: true
    wick_ratio: 2.0                    # 影線長度 ≥ 實體 2 倍才確認
    require_htf_alignment: true        # Sweep 也需 HTF 方向對齊才入場

entry:
  require_htf_alignment: true

  # P1-5 修正：BOS / CHoCH 各自獨立 entry model
  bos_entry_model: ob_fvg             # BOS：OB + FVG 回踩（動能延續，OB 通常更老）
  choch_entry_model: fvg_only         # CHoCH：僅 FVG（剛形成的新 OB 更可信；ob_fvg 也可接受）
  sweep_entry_model: ob_fvg           # Sweep：OB + FVG，強調精準回踩

  premium_discount:
    enabled: true
    # P2 補充：計算方式
    # range_source: htf（用 H4 最近 swing range 計算 50% equilibrium）
    # discount_zone: equilibrium 以下 → 多頭入場偏好區
    # premium_zone:  equilibrium 以上 → 空頭入場偏好區
    # 注意：強趨勢行情中價格長時間在 premium/discount 推進，
    #       若連續 N 根 LTF K 棒均在同側，自動放寬至允許 equilibrium 附近入場
    range_source: htf
    strong_trend_relax_bars: 8        # 連續 8 根 LTF 在同側 → 放寬 premium/discount 過濾

  session_filter:
    enabled: true
    no_entry_sessions:
      - name: "Asia_Low_Liquidity"
        utc_start: "20:00"
        utc_end:   "01:00"
      - name: "Pre_London_Watch"
        utc_start: "05:00"
        utc_end:   "07:00"
        mode: "require_displacement"
    session_open_cooldown_bars: 1
    affect_open_positions: false

signal_logic:
  ltf_trigger_alignment: strict

ai:
  model: "claude-haiku-4-5-20251001"
  max_tokens: 600
  timeout_seconds: 10
  role: "describer"
  forbidden_elements:
    - "DXY"
    - "VIX"
    - "非農"
    - "聯準會"
    - "利率"
    - "通膨"
    - "CPI"
    - "ETF 申購"
    - "鏈上數據"
    - "新聞"
    - "財報"
    - "信心"
    - "勝率"
```

---

## 7. 風控設定（`risk_config.yaml`）

```yaml
account:
  currency: USD
  max_daily_loss_pct: 0.02
  max_weekly_loss_pct: 0.05

position:
  risk_per_trade_pct: 0.01
  max_concurrent_trades: 1            # P2 修正：當前版本限 1 筆（單 symbol BTC/USD）
  max_single_symbol_pct: 0.10

btcusd:
  min_notional_usd: 1
  max_notional_usd: 5000
  spread_filter_pct: 0.001
  limit_order_timeout_seconds: 300    # 新增：限價單未成交 N 秒後自動取消（P0-新2）

circuit_breaker:
  consecutive_losses: 3
  pause_hours: 4
  persist_state: true

hard_sl:                              # P0-新3：盤中插針防護設定
  enabled: true
  buffer_pct: 0.003                   # 結構止損外再加 0.3% 作為 Hard SL 觸發點

server_side_stop:                     # P1-新1：伺服器端保底停損設定
  enabled: true
  buffer_pct: 0.005                   # Hard SL 外再加 0.5% 作為伺服器端停損（最終防線）

# Paper Testing 階段維持 false；確認策略品質後改 true
auto_trade: false
```

---

## 8. 持倉動態規則（`position_rules.yaml`）

```yaml
partial_tp:
  enabled: true
  tp1_close_pct: 0.5
  move_sl_to_entry: true

trailing_stop:
  enabled: true
  activate_after: tp1
  mode: structural
  structural_buffer_usd: 50
  min_trail_pct: 0.003
  fallback_trail_pct: 0.015
  trailing_pivot_bars: 3              # P1-2 明確定義：Pivot 判斷左右各 N 根

invalidation:
  enabled: true
  check_on: candle_close              # 收盤確認（主要）
  # Hard SL 盤中觸發由 risk_config.yaml hard_sl 區塊控制（次要，快速防護）

max_hold:
  enabled: true
  hours: 72
  force_close_condition: "overtime_and_losing"
  unrealized_pnl_threshold: 0.0

server_side_stop:
  auto_cancel_on_close: true          # 系統平倉時自動取消伺服器端停損單
```

---

## 9. 費用優化

| 項目 | 月費 | 說明 |
|------|------|------|
| Haiku 4.5 + Prompt Cache | ~$1.5 | 描述模式 output ~150 tokens |
| Sonnet 4.6（選用） | ~$5.2 | 修改 `ai.model` 一行切換 |
| Zeabur Developer Plan | $5 | 含 $5 免費額度 |
| Alpaca Paper Trading | $0 | 免費 |

---

## 10. 資料庫結構

### `src/storage/schema.sql`

```sql
CREATE TABLE IF NOT EXISTS analysis_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp           TEXT    NOT NULL,
    symbol              TEXT    NOT NULL,
    timeframe           TEXT    NOT NULL,
    signal              TEXT    NOT NULL,       -- BUY / SELL / HOLD / SWEEP_BUY / SWEEP_SELL
    signal_source       TEXT,                   -- BOS / CHOCH / SWEEP（新增）
    htf_bias            TEXT,
    entry_limit_price   REAL,                   -- 限價單進場價（新增）
    entry_low           REAL,
    entry_high          REAL,
    stop_loss           REAL,
    hard_sl_price       REAL,                   -- 盤中插針防護觸發點（新增）
    take_profit_1       REAL,
    take_profit_2       REAL,
    invalidation_level  REAL,
    rrr                 REAL,
    reject_reason       TEXT,
    structure_description TEXT,
    executed            INTEGER DEFAULT 0,
    skip_reason         TEXT,
    config_version      TEXT,
    price_at_signal     REAL,
    outcome_checked     INTEGER DEFAULT 0,
    outcome_result      TEXT,
    outcome_checked_at  TEXT
);

CREATE TABLE IF NOT EXISTS trade_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    analysis_id     INTEGER NOT NULL,
    symbol          TEXT    NOT NULL,
    side            TEXT,
    notional_usd    REAL,
    fill_price      REAL,
    limit_price     REAL,                       -- 限價單目標價（新增）
    stop_loss       REAL,
    hard_sl_price   REAL,                       -- 盤中止損觸發點（新增）
    server_stop_order_id TEXT,                  -- 伺服器端停損單 ID（新增）
    take_profit     REAL,
    open_time       TEXT,
    close_time      TEXT,
    close_price     REAL,
    pnl_usd         REAL,
    close_reason    TEXT,                       -- SL / HARD_SL / INVALIDATED / TRAILING_SL / TP / HTF_FLIP
    broker_order_id TEXT,
    forced_close_pnl_at_trigger REAL,
    adopted         INTEGER DEFAULT 0,
    FOREIGN KEY (analysis_id) REFERENCES analysis_log(id)
);

-- 熔斷器狀態持久化（單行表）
CREATE TABLE IF NOT EXISTS circuit_breaker_state (
    id          INTEGER PRIMARY KEY DEFAULT 1,
    state       TEXT    NOT NULL DEFAULT 'CLOSED',
    loss_streak INTEGER NOT NULL DEFAULT 0,
    opened_at   TEXT,
    updated_at  TEXT    NOT NULL
);

-- 幽靈訂單防護（含平倉訂單）
CREATE TABLE IF NOT EXISTS pending_orders (
    order_id      TEXT    PRIMARY KEY,
    side          TEXT,                         -- BUY / SELL / CLOSE
    notional_usd  REAL,
    submitted_at  TEXT    NOT NULL,
    confirmed     INTEGER DEFAULT 0,
    confirmed_at  TEXT
);

CREATE TABLE IF NOT EXISTS system_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT NOT NULL,
    event_type  TEXT,
    message     TEXT,
    resolved    INTEGER DEFAULT 0
);
```

### 回測查詢範例

```sql
-- 每日信號觸發頻率（目標 12–20 次）
SELECT DATE(timestamp) as date,
       signal_source,
       COUNT(*) as signals
FROM analysis_log WHERE signal != 'HOLD'
GROUP BY DATE(timestamp), signal_source;

-- Sweep vs BOS vs CHoCH 勝率比較
SELECT signal_source,
       COUNT(*) as count,
       ROUND(AVG(CASE WHEN pnl_usd > 0 THEN 1.0 ELSE 0.0 END), 3) as win_rate,
       ROUND(SUM(pnl_usd), 2) as total_pnl
FROM trade_log
JOIN analysis_log ON trade_log.analysis_id = analysis_log.id
GROUP BY signal_source;

-- close_reason 分佈（含 HARD_SL）
SELECT close_reason, COUNT(*) as count,
       ROUND(AVG(pnl_usd), 2) as avg_pnl,
       ROUND(SUM(pnl_usd), 2) as total_pnl
FROM trade_log
GROUP BY close_reason ORDER BY count DESC;

-- 限價單未成交率（觸發但未執行）
SELECT DATE(timestamp) as date,
       COUNT(*) as signals,
       SUM(CASE WHEN executed=0 AND skip_reason='LIMIT_TIMEOUT' THEN 1 ELSE 0 END) as limit_expired
FROM analysis_log WHERE signal != 'HOLD'
GROUP BY DATE(timestamp);
```

---

## 11. Telegram 通知規格

### 11.1 通知事件清單

| 事件 | 優先級 |
|------|--------|
| 啟動通知（含熔斷器狀態 + **模式 PAPER/LIVE**） | 一般 |
| 開倉 / TP1（**含剩餘持倉快照**）/ 平倉 | ⭐ 交易事件 |
| Hard SL 觸發（插針出場） | ⭐ 交易事件 |
| 熔斷器狀態變更 | 🔴 重要 |
| HTF 偏向翻轉告警 | ⚠️ 警告 |
| Reconciliation 告警 | ⚠️ 警告 |
| Volume 異常告警 | 🔴 重要 |
| WS 斷線（含持倉快照 + **當前模式單一 URL**） | 🚨 緊急 |
| 差異化心跳（浮損 ±$50 / 熔斷器變更 / SL 移動 / 每 4 小時強制） | 一般 |

### 11.2 通知格式範例

**開倉：**
```
━━━━━━━━━━━━━━━━━━━━━
📈 *開倉*  BTC/USD LONG
━━━━━━━━━━━━━━━━━━━━━
進場限價：$94,350（OB 回踩）
止損：$93,100  (−$1,250)
Hard SL：$92,821（盤中觸發點）
目標①：$96,800  (+$2,450)
目標②：$98,500  (+$4,150)
失效條件：收盤跌破 $92,800
RRR：1:1.96
HTF 偏向：BULLISH
結構：H1 OB + M15 FVG，CHoCH 確認
訊號類型：CHoCH（位移確認 2 根）
條件確認：5/5
─ 帳戶風險 ─
本筆：$125 | 今日已用：$0 | 熔斷剩：3 筆
```

**TP1 達成（含剩餘持倉快照，P2）：**
```
━━━━━━━━━━━━━━━━━━━━━
✅ *TP1 達成*  BTC/USD LONG（已平 50%）
━━━━━━━━━━━━━━━━━━━━━
實現：+$1,225
─ 剩餘持倉 ─
止損已移至：$94,350（成本，已鎖利）
目標②：$98,500  (+$4,150)
Trailing：啟動（跟蹤 M15 Swing Low，Pivot 3 根）
```

**Hard SL 插針出場（P0-新3）：**
```
━━━━━━━━━━━━━━━━━━━━━
⚡ *Hard SL 出場*  BTC/USD LONG
━━━━━━━━━━━━━━━━━━━━━
插針觸發價：$92,750
出場成交：$92,718（市價）
虧損：−$162
說明：盤中插針觸發 Hard SL（未等收盤確認）
```

**WS 斷線（P2 修正：只顯示當前模式 URL）：**
```
🚨 *WebSocket 斷線*
持倉中！正在重連...（第 2 次，8s 後）
─ 持倉快照 ─
BTC/USD LONG $94,350
止損：$93,100 | 浮動（最後已知）：+$182
Hard SL 已在伺服器：$92,821
─ 緊急操作 ─
https://app.alpaca.markets/paper/dashboard/overview
```

**啟動通知（P2：明確顯示模式）：**
```
🟢 *Trading System v7.0 啟動*
模式：PAPER（Paper Trading）
熔斷器：CLOSED（連虧：0）
Volume：/app/data ✅
trade_updates WS：已連線 ✅
```

---

## 12. 部署（Zeabur）

### 12.1 必要設定

| 項目 | 值 |
|------|---|
| Runtime | Python 3.11 |
| Build 指令 | `pip install -r requirements.txt && python init_db.py` |
| Start 指令 | `python main.py` |
| Volume ① `/app/data` | SQLite DB：熔斷器狀態、幽靈訂單記錄、持久連線路徑 |
| Volume ② `/app/config` | yaml 設定，需持久化 |

> ⚠️ **兩個 Volume 均必須掛載**。  
> `/app/data` 遺失 → 系統強制以 OPEN 熔斷器啟動並推播 Telegram 告警（不靜默繼續交易）。

### 12.2 `zeabur.json`

```json
{
  "build": {
    "command": "pip install -r requirements.txt && python init_db.py"
  },
  "start": {
    "command": "python main.py"
  }
}
```

### 12.3 `requirements.txt`

```
alpaca-py>=0.40.0
anthropic>=0.25.0
aiosqlite>=0.20.0
python-telegram-bot>=21.0
python-dotenv>=1.0.0
pyyaml>=6.0
pandas>=2.0.0
```

> ⚠️ 必須是 `alpaca-py`，**不能有** `alpaca-trade-api`（兩者 import 路徑衝突）。

### 12.4 `.gitignore`

```
.env
/data/
*.db
__pycache__/
*.pyc
.DS_Store
```

---

## 13. 環境變數完整清單

```dotenv
# ── Alpaca ─────────────────────────────────────────
ALPACA_API_KEY=PKxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
ALPACA_SECRET_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
ALPACA_PAPER_MODE=true

# WS 斷線通知 URL：只填當前模式（P2 修正）
# Paper：https://app.alpaca.markets/paper/dashboard/overview
# Live： https://app.alpaca.markets/dashboard/overview
ALPACA_DASHBOARD_URL=https://app.alpaca.markets/paper/dashboard/overview

# ── Claude ──────────────────────────────────────────
ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# ── Telegram ────────────────────────────────────────
TELEGRAM_BOT_TOKEN=xxxxxxxxxx:xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TELEGRAM_CHAT_ID=xxxxxxxxxx

# ── 路徑（配合 Zeabur Volume 掛載點） ─────────────────
DB_PATH=/app/data/trading.db
CONFIG_DIR=/app/config

# ── 系統設定 ─────────────────────────────────────────
LOG_LEVEL=INFO
TZ=UTC
```

### 環境變數 → SDK 對照

| 環境變數 | 對應程式碼 | 說明 |
|---------|----------|------|
| `ALPACA_API_KEY` | `TradingClient(api_key=...)` `CryptoDataStream(api_key=...)` `TradingStream(api_key=...)` | Paper 或 Live Key |
| `ALPACA_SECRET_KEY` | `TradingClient(secret_key=...)` `CryptoDataStream(secret_key=...)` `TradingStream(secret_key=...)` | Paper 或 Live Secret |
| `ALPACA_PAPER_MODE` | `TradingClient(paper=True/False)` `TradingStream(paper=True/False)` | `"true"` → Paper，`"false"` → Live |
| `ALPACA_DASHBOARD_URL` | `tg.alert(...)` WS 斷線通知 | 只填當前模式 URL |
| `ANTHROPIC_API_KEY` | `anthropic.Anthropic()` 自動偵測 | Anthropic SDK 自動讀取 |
| `TELEGRAM_BOT_TOKEN` | `Bot(token=...)` | — |
| `TELEGRAM_CHAT_ID` | `bot.send_message(chat_id=...)` | — |
| `DB_PATH` | `aiosqlite.connect(path)` | 需在 Volume 掛載的路徑下 |
| `CONFIG_DIR` | `open(f"{CONFIG_DIR}/smc_config.yaml")` | 需在 Volume 掛載的路徑下 |

---

## 14. Zeabur 雲端測試流程

### Phase 1：基礎連線驗證（Day 1）

**步驟：**

1. Fork repo → Zeabur 連結 GitHub repo → 填入 Variables → 掛載兩個 Volumes → Deploy
2. 開啟 **Runtime Logs**，確認出現：
   ```
   Trading System v7.0 starting...
   [BOOT] Volume check OK: /app/data is mounted
   Circuit breaker state restored: CLOSED (streak: 0)
   Scanning orphan orders on startup... none found.
   WebSocket trade_updates connected
   WebSocket BTC/USD bars+trades subscribed
   Reconciliation complete: no drift detected
   Candle close: BTC/USD M15
   ```
3. 確認 Telegram 收到啟動通知，**含 `模式：PAPER`**
4. 等待最多 4 小時，確認心跳推播正常

**Zeabur 日誌錯誤排查對照表：**

| 日誌錯誤訊息 | 原因 | 修復 |
|------------|------|------|
| `alpaca.common.exceptions.APIError: 40110000` | API Key/Secret 錯誤，或 Paper/Live 金鑰混用 | 確認 Key 前綴：Paper = `PK`，Live = `AK` |
| `ModuleNotFoundError: No module named 'alpaca'` | `requirements.txt` 未含 `alpaca-py` | 確認是 `alpaca-py` |
| `ModuleNotFoundError: No module named 'alpaca.trading'` | 舊版 SDK | 移除 `alpaca-trade-api`，改用 `alpaca-py` |
| `ModuleNotFoundError: No module named 'alpaca.trading.stream'` | `alpaca-py` 版本過舊 | 確認 `alpaca-py>=0.40.0` |
| `telegram.error.Unauthorized` | `TELEGRAM_BOT_TOKEN` 錯誤 | 重新向 @BotFather 取得 token |
| `aiosqlite.OperationalError: unable to open database` | `/app/data` Volume 未掛載 | Zeabur → Service → Volumes 頁面確認 |
| `aiosqlite.OperationalError: database is locked` | timeout 設定無效 | 確認 `db.connect()` 有 `timeout=5.0` |
| `FileNotFoundError: .../smc_config.yaml` | `/app/config` Volume 未掛載 | 確認 Volume 掛載且三個 yaml 均存在 |
| `anthropic.AuthenticationError` | `ANTHROPIC_API_KEY` 錯誤 | 確認金鑰格式（`sk-ant-...`） |
| `[BOOT] Volume 異常` | DB 路徑不在 `/app/data` | 確認 `DB_PATH=/app/data/trading.db` |

---

### Phase 2：策略信號驗證（Week 1–2）

**目標：** 觀察 SMC 信號品質，`auto_trade: false`

1. `risk_config.yaml` 確認 `auto_trade: false` → Zeabur 重新部署
2. 下載 DB 檔案（Zeabur Volume 頁面），用 [DB Browser for SQLite](https://sqlitebrowser.org/) 查詢：

```sql
-- 每日信號頻率，區分來源（目標 12–20 次/天）
SELECT DATE(timestamp) as date, signal_source, COUNT(*) as signals
FROM analysis_log WHERE signal != 'HOLD'
GROUP BY DATE(timestamp), signal_source;

-- Sweep 信號是否正常觸發
SELECT * FROM analysis_log WHERE signal_source = 'SWEEP'
ORDER BY timestamp DESC LIMIT 20;

-- CHoCH 位移確認是否生效（應無 displacement_bars < 2 的 CHoCH 入場）
SELECT * FROM analysis_log WHERE signal_source = 'CHOCH' AND executed = 1
ORDER BY timestamp DESC LIMIT 20;

-- 確認反向觸發被正確過濾
SELECT COUNT(*) FROM analysis_log
WHERE reject_reason = 'NO_VALID_LTF_TRIGGER';
```

---

### Phase 3：Paper Trading 下單驗證（Week 3–4）

**目標：** 確認全流程：限價單進場 → 伺服器端停損單 → 持倉管理 → 熔斷器

1. `risk_config.yaml` → `auto_trade: true` → 重新部署
2. 等待 Telegram 收到 `📈 *開倉*` 通知，確認：
   - 通知含限價進場價（非市價）
   - 通知含 Hard SL 觸發點
   - 通知含帳戶風險摘要
3. 登入 [paper.alpaca.markets](https://app.alpaca.markets/paper/dashboard/overview) 確認：
   - 限價單存在（非已成交的市價單）
   - 成交後有一張伺服器端停損單
4. **驗證熔斷器持久化**：
   - Zeabur → Restart Service
   - 確認 Telegram 啟動通知熔斷器狀態與重啟前一致
5. **驗證 Reconciliation 窗口修正**：
   - Zeabur → Stop → 等 30s → Start
   - 日誌應顯示「先訂閱 WebSocket，再執行 Reconciliation」順序

---

### Phase 4：切換實盤（Paper 驗證完畢後）

1. 在 [app.alpaca.markets](https://app.alpaca.markets) 申請 Live 帳戶並充值
2. 更新 Zeabur Variables：
   - `ALPACA_API_KEY` → Live Key (`AK...`)
   - `ALPACA_SECRET_KEY` → Live Secret (`AS...`)
   - `ALPACA_PAPER_MODE` → `false`
   - `ALPACA_DASHBOARD_URL` → `https://app.alpaca.markets/dashboard/overview`
3. Zeabur Redeploy，確認啟動通知顯示 `模式：LIVE`
4. 初期建議 `risk_per_trade_pct: 0.005`（0.5%）測試 1 週

---

### 上線前確認清單

- [ ] Zeabur Volume `/app/data` 與 `/app/config` 均已掛載
- [ ] `requirements.txt` 使用 `alpaca-py>=0.40.0`，無 `alpaca-trade-api`
- [ ] `.env` 未進入 git（確認 `.gitignore`）
- [ ] Telegram 收到啟動通知，含熔斷器狀態 + **模式（PAPER/LIVE）**
- [ ] 日誌出現 `Volume check OK`
- [ ] 日誌出現 `WebSocket trade_updates connected`（訂單成交 WS 就緒）
- [ ] 日誌出現 `Candle close: BTC/USD M15`
- [ ] 心跳推播正常（4 小時強制一次）
- [ ] 重啟後熔斷器狀態從 DB 正確恢復（不自動清零）
- [ ] Paper Trading Paper Trading ≥ 2 週，`analysis_log` 記錄完整
- [ ] 信號觸發頻率 12–20 次/天，`signal_source` 區分正常
- [ ] 首筆 Paper 開倉：Alpaca 後台同時存在限價進場單 + 伺服器端停損單
- [ ] 觸發 Hard SL 情境（可手動設高 `hard_sl_buffer_pct` 測試）：系統以 `HARD_SL` 出場
- [ ] `NO_VALID_LTF_TRIGGER` 出現於反向觸發情境
- [ ] `SESSION_COOLDOWN` 出現於時段切換後第一根 K 棒
- [ ] Sweep 信號在 `analysis_log` 中以 `signal_source='SWEEP'` 正確記錄

---

*DEVELOPMENT.md · v7.0 · April 2026 · Pure Price Action / SMC*
