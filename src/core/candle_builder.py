from collections import defaultdict, deque
from datetime import datetime, timezone


class CandleBuilder:
    """
    M1 bar → M1 / H1 / H4 聚合。
    Alpaca CryptoDataStream 提供 1-minute bars，
    本模組將其聚合為策略所需的多週期 K 棒。
    M1 timeframe passes each bar through as-is (period=1).

    FIX P1-6: 收盤判斷改為 period_start 邊界變化，
    不再依賴 bar_count >= minutes（加密貨幣有缺口時永遠達不到）。
    """

    TIMEFRAME_MINUTES = {
        "M1":  1,
        "H1":  60,
        "H4":  240,
    }

    def __init__(self, on_candle_close):
        """
        on_candle_close(symbol, timeframe, candle_dict) 會在每根 K 棒收盤時呼叫。
        candle_dict: {"open", "high", "low", "close", "volume", "timestamp"}
        """
        self._on_close = on_candle_close
        self._accum    = defaultdict(lambda: defaultdict(lambda: None))
        self._history  = defaultdict(lambda: defaultdict(lambda: deque(maxlen=200)))

    async def on_m1_bar(self, symbol: str, bar):
        """接收 Alpaca M1 bar，驅動所有 timeframe 聚合"""
        ts = self._bar_timestamp(bar)
        m1 = {
            "open":      float(bar.open),
            "high":      float(bar.high),
            "low":       float(bar.low),
            "close":     float(bar.close),
            "volume":    float(bar.volume),
            "timestamp": ts,
        }

        for tf, minutes in self.TIMEFRAME_MINUTES.items():
            closed = self._aggregate(symbol, tf, minutes, m1)
            if closed:
                self._history[symbol][tf].append(closed)
                await self._on_close(symbol, tf, closed)

    def _bar_timestamp(self, bar) -> datetime:
        ts = bar.timestamp
        if hasattr(ts, "tzinfo") and ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts

    def _period_key(self, ts: datetime, minutes: int) -> int:
        """
        FIX P1-6: 用 Unix epoch 分鐘數整除週期，
        跨 UTC 日午夜時也能正確對齊（不用 hour*60+minute）。
        """
        epoch_minutes = int(ts.timestamp()) // 60
        return (epoch_minutes // minutes) * minutes

    def _aggregate(self, symbol: str, tf: str, minutes: int, m1: dict) -> dict | None:
        """
        累積 M1 K 棒。
        FIX P1-6: 收盤條件改為「period_key 發生變化」，
        不依賴 bar_count，解決加密貨幣行情缺口導致 H4 永遠不收盤的問題。
        """
        acc        = self._accum[symbol][tf]
        ts         = m1["timestamp"]
        period_key = self._period_key(ts, minutes)

        if acc is None:
            self._accum[symbol][tf] = {
                "open":       m1["open"],
                "high":       m1["high"],
                "low":        m1["low"],
                "close":      m1["close"],
                "volume":     m1["volume"],
                "period_key": period_key,
                "timestamp":  ts,
            }
            return None

        if period_key == acc["period_key"]:
            # 同週期：更新 OHLCV
            acc["high"]      = max(acc["high"], m1["high"])
            acc["low"]       = min(acc["low"],  m1["low"])
            acc["close"]     = m1["close"]
            acc["volume"]   += m1["volume"]
            acc["timestamp"] = ts
            return None
        else:
            # 新週期開始：收盤上一根，開啟新的累積
            closed = {
                "open":      acc["open"],
                "high":      acc["high"],
                "low":       acc["low"],
                "close":     acc["close"],
                "volume":    acc["volume"],
                "timestamp": acc["timestamp"],
            }
            self._accum[symbol][tf] = {
                "open":       m1["open"],
                "high":       m1["high"],
                "low":        m1["low"],
                "close":      m1["close"],
                "volume":     m1["volume"],
                "period_key": period_key,
                "timestamp":  ts,
            }
            return closed

    def get_history(self, symbol: str, tf: str) -> list:
        return list(self._history[symbol][tf])

    def seed_history(self, symbol: str, tf: str, bars: list):
        """用歷史資料初始化 K 棒緩存（系統啟動時呼叫）"""
        for bar in bars:
            self._history[symbol][tf].append(bar)
