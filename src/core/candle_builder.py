from collections import defaultdict, deque
from datetime import datetime, timezone


class CandleBuilder:
    """
    M1 bar → M15 / H1 / H4 聚合。
    Alpaca CryptoDataStream 提供 1-minute bars，
    本模組將其聚合為策略所需的多週期 K 棒。
    """

    TIMEFRAME_MINUTES = {
        "M15": 15,
        "H1":  60,
        "H4":  240,
    }

    def __init__(self, on_candle_close):
        """
        on_candle_close(symbol, timeframe, candle_dict) 會在每根 K 棒收盤時呼叫。
        candle_dict: {"open", "high", "low", "close", "volume", "timestamp"}
        """
        self._on_close = on_candle_close
        # 每個 symbol + tf 的累積 K 棒資料
        self._accum = defaultdict(lambda: defaultdict(lambda: None))
        # 已收盤的歷史 K 棒緩存（最多保留 200 根）
        self._history = defaultdict(lambda: defaultdict(lambda: deque(maxlen=200)))

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

    def _aggregate(self, symbol: str, tf: str, minutes: int, m1: dict) -> dict | None:
        """
        累積 M1 K 棒直到填滿 tf 的週期。
        回傳已收盤的 K 棒；若尚未收盤則回傳 None。
        """
        acc = self._accum[symbol][tf]
        ts  = m1["timestamp"]

        # 計算此 M1 屬於哪個 tf 週期（取整數）
        total_minutes = ts.hour * 60 + ts.minute
        period_start  = (total_minutes // minutes) * minutes

        if acc is None:
            # 開始新的 K 棒
            self._accum[symbol][tf] = {
                "open":          m1["open"],
                "high":          m1["high"],
                "low":           m1["low"],
                "close":         m1["close"],
                "volume":        m1["volume"],
                "period_start":  period_start,
                "bar_count":     1,
            }
            return None

        if period_start == acc["period_start"]:
            # 同週期：更新 OHLCV
            acc["high"]  = max(acc["high"], m1["high"])
            acc["low"]   = min(acc["low"],  m1["low"])
            acc["close"] = m1["close"]
            acc["volume"] += m1["volume"]
            acc["bar_count"] += 1

            # 最後一根 M1（週期滿）
            if acc["bar_count"] >= minutes:
                closed = {
                    "open":      acc["open"],
                    "high":      acc["high"],
                    "low":       acc["low"],
                    "close":     acc["close"],
                    "volume":    acc["volume"],
                    "timestamp": ts,
                }
                self._accum[symbol][tf] = None
                return closed
            return None
        else:
            # 新週期開始：收盤上一根
            closed = {
                "open":      acc["open"],
                "high":      acc["high"],
                "low":       acc["low"],
                "close":     acc["close"],
                "volume":    acc["volume"],
                "timestamp": ts,
            }
            self._accum[symbol][tf] = {
                "open":         m1["open"],
                "high":         m1["high"],
                "low":          m1["low"],
                "close":        m1["close"],
                "volume":       m1["volume"],
                "period_start": period_start,
                "bar_count":    1,
            }
            return closed

    def get_history(self, symbol: str, tf: str) -> list:
        return list(self._history[symbol][tf])

    def seed_history(self, symbol: str, tf: str, bars: list):
        """用歷史資料初始化 K 棒緩存（系統啟動時呼叫）"""
        for bar in bars:
            self._history[symbol][tf].append(bar)
