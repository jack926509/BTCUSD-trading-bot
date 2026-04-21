# ₿ BTC/USD 全自動交易系統

> Pure Price Action / SMC 策略 · Alpaca Crypto × Claude AI × Telegram 單向通知  
> 開發階段月費約 **$2–5** · 測試環境：**Zeabur 雲端部署（無需本機環境）**

---

## 功能概覽

- **全自動 24/7 監視**：`CryptoDataStream` WebSocket 串流，K棒收盤事件驅動，無輪詢
- **SMC 策略**：BOS / CHoCH / OB / FVG / EQH-EQL / Sweep 流動性掠奪，多時框對齊（H4 → H1 → M15）
- **訊號確認即時送單**：SMC 訊號確認後立即下單，Claude 描述與 Telegram 通知以 `asyncio.create_task` 非同步補寫，**不阻塞進場**
- **限價單進場**：預設使用 `LimitOrderRequest`（OB/FVG 回踩價位），保護 SMC 精算的 RRR；極端行情備援為 Stop-Limit
- **雙層止損保護**：收盤確認 INVALIDATED（主要）＋ 盤中 Hard SL 插針防護（若盤中價格穿越緩衝區達閾值，不等收盤直接市價出場）
- **雲端伺服器端停損單**：每次進場成交後立即在 Alpaca 伺服器送出保底停損單（Hard SL 外 0.5%），Zeabur 當機時持倉仍有保護
- **BOS / CHoCH 方向嚴格分離**：反向觸發信號自動過濾為 HOLD，不混用；BOS / CHoCH 各有獨立 entry model 設定
- **Sweep 流動性掠奪辨識**：偵測前高/前低假突破後收長影線，作為高勝率反向進場點
- **持倉動態管理**：部分止盈（TP1 平倉 50%）→ 止損移至成本 → 結構 Trailing Stop（跟蹤 LTF Swing Low/High，以 N 根 K 棒 Pivot 確認）
- **三態熔斷器**：連虧 3 筆自動暫停，**狀態持久化至 DB**，Zeabur 重啟不自動解除；HALF 態試單失敗正確重置連虧計數
- **重連後狀態對齊（Reconciliation）**：先訂閱 WebSocket 再執行 Reconciliation，防止重連窗口止損事件掉落
- **Volume 遺失保護**：啟動時偵測 DB 路徑是否在已知掛載點，若異常則強制以 OPEN 熔斷器狀態啟動並告警
- **事件驅動成交確認**：訂閱 `trade_updates` WebSocket，取代 REST API 輪詢，消除速率限制風險
- **aiosqlite 防鎖死**：連線設定 `timeout=5.0`，極端行情高頻寫入不造成系統停擺
- **INVALIDATED 直接出場**：K棒收盤確認失效後立即在 Fast Track 送出平倉，不進 Slow Track
- **差異化心跳**：持倉浮盈/浮虧變化 ±$50、熔斷器狀態改變、止損移動時推播；無變化靜默，每 4 小時強制一次
- **完整交易日誌**：每次 SMC 決策記錄入 SQLite，供 Zeabur Volume 下載後回測分析

---

## Telegram 通知定位

本系統 Telegram 為**純單向推播**，系統控制透過設定檔與 Zeabur 後台操作：

| 需求 | 操作方式 |
|------|---------|
| 暫停交易 | `risk_config.yaml` → `auto_trade: false`，Zeabur 重啟 |
| 修改策略 | 編輯 `smc_config.yaml`（透過 Zeabur Volume 更新），重啟 |
| 緊急平倉 | 登入 Alpaca 後台手動操作 |
| 切換模型 | `smc_config.yaml` → `ai.model`，重啟 |
| 重置熔斷器 | 手動清除 DB `circuit_breaker_state` 表後重啟（重啟**不**自動重置） |

**推播事件：** 啟動通知（含熔斷器狀態 + 運行模式 PAPER/LIVE）/ 開倉 / TP1 部分平倉（含剩餘持倉快照）/ 平倉 / 熔斷器狀態變更 / Reconciliation 告警 / 差異化心跳 / WS 斷線（含持倉快照 + 當前模式 Alpaca 直連 URL）/ 系統錯誤 / Volume 異常告警

---

## 快速啟動（Zeabur 雲端，無需本機環境）

### 1. 取得必要金鑰

| 服務 | 說明 | 費用 |
|------|------|------|
| [Alpaca Paper Trading](https://app.alpaca.markets/paper/dashboard/overview) | Paper 帳戶取得 API Key（前綴 `PK...`） | 免費 |
| [Anthropic Console](https://console.anthropic.com) | Claude API Key（`sk-ant-...`） | 用量計費 |
| [Telegram @BotFather](https://t.me/BotFather) | 建立 Bot，取得 Token | 免費 |
| [Telegram @userinfobot](https://t.me/userinfobot) | 取得自己的 Chat ID | 免費 |

> ⚠️ **Paper 和 Live 是兩組不同的金鑰。**  
> Paper 金鑰前綴 `PK...`，在 paper.alpaca.markets 取得。  
> Live 金鑰前綴 `AK...`，在 app.alpaca.markets 取得。請勿混用。

### 2. 部署到 Zeabur

```
1. Fork 此 repo 至 github.com/<你的帳號>/btcusd-bot
2. Zeabur → New Project → Deploy from GitHub → 選擇此 repo
3. 設定兩個 Volume：
   /app/data   （SQLite DB）
   /app/config （yaml 設定）
4. Variables 頁面填入所有環境變數（見下方清單）
5. Deploy → 觀察 Runtime Logs
```

### 3. Zeabur Variables 清單

| 變數名稱 | 說明 | 範例值 |
|---------|------|-------|
| `ALPACA_API_KEY` | Paper API Key | `PKxxxxxxxx...` |
| `ALPACA_SECRET_KEY` | Paper Secret Key | `xxxxxxxx...` |
| `ALPACA_PAPER_MODE` | `true` = Paper，`false` = Live | `true` |
| `ALPACA_DASHBOARD_URL` | WS 斷線通知的直連 URL（僅填當前模式） | `https://app.alpaca.markets/paper/dashboard/overview` |
| `ANTHROPIC_API_KEY` | Claude API Key | `sk-ant-xxxxxxxx...` |
| `TELEGRAM_BOT_TOKEN` | Telegram Bot Token | `xxxxxxxxxx:xxx...` |
| `TELEGRAM_CHAT_ID` | Telegram Chat ID | `xxxxxxxxxx` |
| `DB_PATH` | SQLite 路徑（配合 Volume） | `/app/data/trading.db` |
| `CONFIG_DIR` | yaml 設定目錄（配合 Volume） | `/app/config` |
| `LOG_LEVEL` | 日誌層級 | `DEBUG`（初期）/ `INFO`（穩定後） |
| `TZ` | 時區 | `UTC` |

### 4. 確認運作

啟動後 Zeabur Runtime Logs 應顯示：
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

Telegram 應收到啟動通知（含 `模式：PAPER`），4 小時後收到第一次心跳推播。

---

## 技術架構

```
Fast Track (<10ms)：CryptoDataStream → K棒聚合 → SMC 計算 → 持倉監視
  ├─ 訊號確認 → 立即送限價單（不等 Claude）
  ├─ Hard SL 盤中插針防護（tick 級監視）
  └─ INVALIDATED 收盤確認 → 直接出場，不進 Slow Track

Slow Track（可接受延遲）：Claude 描述 → aiosqlite 寫入 → Telegram 推播
  └─ asyncio.create_task 非同步執行，不阻塞進場

成交確認：trade_updates WebSocket 事件驅動（取代 REST 輪詢）
雲端保底：進場成交後立即送伺服器端停損單（Hard SL 外 0.5%）
```

**SDK 對照：**

| 功能 | alpaca-py Client |
|------|-----------------|
| 下單 / 查帳戶 / 查持倉 | `TradingClient(paper=True/False)` |
| 即時 WebSocket 串流（K棒 + tick） | `CryptoDataStream` |
| 即時 WebSocket 串流（訂單狀態） | `TradingStream`（`trade_updates`） |
| 歷史 K 棒 | `CryptoHistoricalDataClient` |

詳細架構與實作說明見 [DEVELOPMENT.md](./DEVELOPMENT.md)。

---

## 設定檔說明

透過 Zeabur Volume 更新後重啟服務生效：

| 檔案 | 用途 |
|------|------|
| `config/smc_config.yaml` | SMC 策略、BOS/CHoCH 各自 entry model、Sweep 辨識、時段過濾、AI 模型 |
| `config/risk_config.yaml` | 資金風控、熔斷器閾值、Hard SL 倍數、`auto_trade` 開關 |
| `config/position_rules.yaml` | 部分止盈、結構 Trailing Stop（Pivot 確認根數）、伺服器端保底停損 |

---

## v7.0 變更摘要（vs v6.1）

| 問題 | v6.1 | v7.0 |
|------|------|------|
| 進場延遲 | 等待 Claude API 描述後才下單（數秒延遲） | 訊號確認即時下限價單，Claude 非同步補寫日誌 |
| 訂單類型 | 全面市價單（嚴重滑點風險） | 預設限價單（OB/FVG 回踩），Stop-Limit 作為備援 |
| 插針防護 | 僅收盤確認 INVALIDATED | 雙層：Hard SL 盤中插針防護 ＋ 收盤確認 INVALIDATED |
| 伺服器端保命 | 無（客戶端 SL 裸奔） | 進場成交後立即在 Alpaca 送伺服器端停損單 |
| 成交確認 | REST API 每 0.3s 輪詢（Rate Limit 風險） | `trade_updates` WebSocket 事件驅動 |
| DB 防鎖死 | 無 timeout 設定 | `aiosqlite.connect(..., timeout=5.0)` |
| Sweep 辨識 | 無 | EQH/EQL 假突破 + 長影線確認（高勝率反向進場點） |
| BOS/CHoCH entry model | 共用同一組 ob_fvg | 各自獨立設定（CHoCH 加強位移確認） |
| Trailing Stop 定義 | `_detect_swing` 黑盒，邏輯未揭露 | Pivot Point N 根確認，文件明確定義 |
| close_position 防護 | 無幽靈訂單防護 | 與 place() 相同的 pending_orders 記錄機制 |
| Reconciliation 窗口 | 先 Reconcile 再訂閱（SL 事件可能掉落） | 先訂閱後 Reconcile |
| HALF 態熔斷器 bug | loss_streak 不重置，可能永久鎖倉 | HALF → OPEN 時 loss_streak = 1 |
| _persist 可靠性 | fire-and-forget，DB 失敗無補償 | await + 失敗時強制 OPEN 熔斷器 |
| Volume 遺失 | 無保護，以 CLOSED 熔斷器啟動 | 啟動時偵測掛載點，異常則強制 OPEN 並告警 |
| 心跳間隔（程式碼） | sleep(3600)（與文件矛盾） | sleep(14400)，與「每 4 小時」設計一致 |
| 心跳觸發條件 | 「狀態有變化」定義不清 | 明確：浮損變化 ±$50、熔斷器變更、SL 移動 |
| WS 斷線通知 URL | 同時顯示 Paper + Live 兩條（增加認知負擔） | 只顯示當前模式 URL（由 `ALPACA_PAPER_MODE` 決定） |
| DB 連線方式 | 每次操作重新 open/close | 持久連線 + asyncio.Lock 序列化寫入 |
| OB 有效性 | max_age_bars: 50，無 wick 緩解判斷 | max_age_bars: 30，加入 wick_penetration_threshold |
| HTF 偏向翻轉 | 無 hook，持倉不保護 | HTF 翻轉時觸發持倉告警 / 條件平倉 |
| max_concurrent_trades | 設為 2（與單 symbol 架構矛盾） | 改為 1，明確標注單 symbol 限制 |
| TP1 通知 | 僅顯示部分平倉，無剩餘持倉資訊 | 含剩餘持倉快照（新止損 + 目標② + Trailing 狀態） |
| HTF 偏向翻轉 | 無 hook，持倉不保護 | HTF 翻轉時觸發持倉告警 / 條件平倉 |

---

## 費用說明

| 項目 | 費用 |
|------|------|
| Alpaca Paper Trading | $0 |
| Claude Haiku 4.5 + Prompt Cache | ~$1.5/月 |
| Zeabur Developer Plan | $5/月（含 $5 免費額度） |
| **合計** | **~$2–5/月** |

---

## 注意事項

- **限價單未成交風險**：預設限價單在快速行情中可能因價格未回踩而未成交，建議在 `risk_config.yaml` 設定 `limit_order_timeout_seconds`（預設 300 秒），超時自動取消
- **伺服器端停損單**：進場成交後系統自動送出，Hard SL 外 0.5% 作為最終保護；切勿在 Alpaca 後台手動刪除這張單
- **Paper Trading 優先**：`ALPACA_PAPER_MODE=true`，建議觀察至少 2 週再考慮切換實盤
- **熔斷器持久化**：v7.0 熔斷狀態存於 DB，Zeabur 重啟不會解除熔斷；若需手動重置，清除 DB 中 `circuit_breaker_state` 表後重啟
- **`.env` 不可進入 git**：Zeabur 透過 Variables 頁面設定金鑰，不上傳 `.env` 檔案
- **Volume 必須掛載**：`/app/data` 遺失 = 系統強制以 OPEN 熔斷器啟動並推播告警，不會靜默繼續交易

---

## Zeabur 雲端測試建議順序

```
Phase 1：基礎連線驗證（Day 1）
  → Zeabur 部署成功 + Telegram 收到啟動通知（含 模式：PAPER）
  → 日誌出現 Volume check OK + trade_updates WebSocket connected + Candle close

Phase 2：策略信號驗證（Week 1–2）
  → auto_trade: false，觀察 analysis_log 信號品質
  → 確認 Sweep 信號與 BOS/CHoCH 信號分別被記錄
  → 確認 CHoCH 有位移確認，BOS 有獨立 entry model

Phase 3：Paper Trading 全流程（Week 3–4）
  → auto_trade: true，驗證限價單進場 / 伺服器端停損單 / Hard SL / TP1 / 熔斷器 / Reconciliation

Phase 4：切換實盤（評估後）
  → 更換 Live API Key，ALPACA_PAPER_MODE=false
  → 更新 ALPACA_DASHBOARD_URL 為 Live URL
```

詳細步驟與日誌排查對照表見 [DEVELOPMENT.md § 14](./DEVELOPMENT.md#14-zeabur-雲端測試流程)。

---

*v7.0 · April 2026 · Pure Price Action / SMC*
