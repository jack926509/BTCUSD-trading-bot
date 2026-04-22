import os
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from src.config.loader import get_smc_config


@dataclass
class SMCSignal:
    direction:          str   = "HOLD"   # BUY / SELL / HOLD / SWEEP_BUY / SWEEP_SELL
    source:             str   = ""       # BOS / CHOCH / SWEEP
    htf_bias:           str   = "NEUTRAL"
    entry_limit_price:  float = 0.0
    entry_low:          float = 0.0
    entry_high:         float = 0.0
    stop_loss:          float = 0.0
    hard_sl_price:      float = 0.0
    take_profit_1:      float = 0.0
    take_profit_2:      float = 0.0
    invalidation_level: float = 0.0
    rrr:                float = 0.0
    price_at_signal:    float = 0.0
    timeframe:          str   = "M1"
    conditions_met:     list  = field(default_factory=list)
    reject_reason:      str   = ""
    # BOS / CHoCH specific
    displacement_bars:  Optional[int]   = None
    # Sweep specific
    swept_level:        Optional[float] = None
    # OB / FVG levels for notification
    ob_level:           Optional[float] = None
    fvg_range:          Optional[str]   = None
    # E5 / E6 / E2 extra
    atr_value:          Optional[float] = None
    retrace_pct:        Optional[float] = None
    confluence:         bool            = False
    # X1: 結構目標 TP flag
    tp_is_structural:   bool            = False


class SMCContext:
    def __init__(self, signal: SMCSignal):
        self._signal = signal

    def has_candidate(self) -> bool:
        return self._signal.direction != "HOLD"

    def get_signal(self) -> SMCSignal:
        return self._signal


class SMCEngine:
    def __init__(self):
        self._cfg = get_smc_config()

        self.htf = self._cfg["timeframes"]["htf"]   # H4
        self.mtf = self._cfg["timeframes"]["mtf"]   # H1
        self.ltf = self._cfg["timeframes"]["ltf"]   # M1

        # 每個 symbol 的 K 棒緩存
        self._bars:       dict = defaultdict(lambda: defaultdict(lambda: deque(maxlen=300)))
        self._htf_bias:   dict = {}
        self._htf_bias_flipped: dict = {}

        # Structure state
        # B-4: 改用 deque(maxlen=50)，避免 list.pop(0) O(n) 操作
        self._swing_highs: dict = defaultdict(lambda: defaultdict(lambda: deque(maxlen=50)))
        self._swing_lows:  dict = defaultdict(lambda: defaultdict(lambda: deque(maxlen=50)))
        self._order_blocks: dict = defaultdict(lambda: defaultdict(list))
        self._fvgs:         dict = defaultdict(lambda: defaultdict(list))
        self._eqh_eql:      dict = defaultdict(lambda: defaultdict(list))

        sweep_cfg                = self._cfg.get("liquidity", {}).get("sweep", {})
        self.sweep_wick_ratio    = sweep_cfg.get("wick_ratio", 2.0)
        self.sweep_htf_align     = sweep_cfg.get("require_htf_alignment", True)

        ob_cfg                   = self._cfg.get("liquidity", {}).get("order_block", {})
        self.ob_max_age          = ob_cfg.get("max_age_bars", 30)
        self.ob_min_body_ratio   = ob_cfg.get("min_body_ratio", 0.6)
        self.ob_wick_threshold   = ob_cfg.get("wick_penetration_threshold", 0.7)

        fvg_cfg                  = self._cfg.get("liquidity", {}).get("fair_value_gap", {})
        self.fvg_min_gap         = fvg_cfg.get("min_gap_usd", 50)
        self.fvg_partial_fill    = fvg_cfg.get("partial_fill_threshold", 0.5)

        eql_cfg                  = self._cfg.get("liquidity", {}).get("equal_highs_lows", {})
        self.eql_tolerance       = eql_cfg.get("tolerance_usd", 150)

        bos_cfg                  = self._cfg.get("structure", {}).get("bos", {})
        self.bos_min_swing       = bos_cfg.get("min_swing_bars", 5)

        choch_cfg                = self._cfg.get("structure", {}).get("choch", {})
        self.choch_displacement  = choch_cfg.get("displacement_min_candles", 2)

        entry_cfg                = self._cfg.get("entry", {})
        self.bos_entry_model     = entry_cfg.get("bos_entry_model", "ob_fvg")
        self.choch_entry_model   = entry_cfg.get("choch_entry_model", "fvg_only")
        self.sweep_entry_model   = entry_cfg.get("sweep_entry_model", "ob_fvg")

        # E1 Premium/Discount
        pd_cfg = entry_cfg.get("premium_discount", {})
        self.pd_enabled          = pd_cfg.get("enabled", False)
        self.pd_threshold        = pd_cfg.get("discount_threshold", 0.5)
        self.pd_relax_bars       = pd_cfg.get("strong_trend_relax_bars", 8)

        # E2 Confluence
        conf_cfg = entry_cfg.get("confluence", {})
        self.conf_enabled        = conf_cfg.get("enabled", True)
        self.conf_require        = conf_cfg.get("require_overlap", False)

        # E3 Rejection candle
        rej_cfg = entry_cfg.get("rejection_candle", {})
        self.rej_enabled         = rej_cfg.get("enabled", False)
        self.rej_wick_ratio      = rej_cfg.get("min_wick_ratio", 1.2)

        # E4 OB entry mode
        self.ob_entry_mode       = entry_cfg.get("ob_entry_mode", "edge")

        # E5 ATR SL
        atr_cfg = entry_cfg.get("atr_sl", {})
        self.atr_enabled         = atr_cfg.get("enabled", False)
        self.atr_period          = atr_cfg.get("atr_period", 14)
        self.atr_multiplier      = atr_cfg.get("atr_multiplier", 1.5)
        self.atr_tf              = atr_cfg.get("atr_timeframe", "M1")
        self.atr_min_pct         = atr_cfg.get("min_sl_pct", 0.003)
        self.atr_max_pct         = atr_cfg.get("max_sl_pct", 0.015)

        # E6 OTE Fibonacci
        ote_cfg = entry_cfg.get("ote", {})
        self.ote_enabled         = ote_cfg.get("enabled", False)
        self.ote_min             = ote_cfg.get("min_retrace", 0.62)
        self.ote_max             = ote_cfg.get("max_retrace", 0.79)

        self._hard_sl_buffer     = 0.003

        # 信號統計（用於 heartbeat 診斷）
        self._signal_stats: Counter = Counter()  # reason → count
        self._signal_total: int     = 0

    # ── Public API ───────────────────────────────────────────────────────────

    def update(self, symbol: str, tf: str, candle: dict) -> SMCContext:
        """
        每根 K 棒收盤時呼叫。
        回傳 SMCContext，若有交易訊號則 has_candidate() == True。
        """
        self._bars[symbol][tf].append(candle)
        self._htf_bias_flipped[symbol] = False

        if tf == self.htf:
            old_bias = self._htf_bias.get(symbol, "NEUTRAL")
            self._update_htf_bias(symbol)
            if self._htf_bias.get(symbol) != old_bias:
                self._htf_bias_flipped[symbol] = True
            return SMCContext(SMCSignal())  # HTF 更新不產生進場訊號

        if tf == self.mtf:
            self._update_structure(symbol, tf)
            return SMCContext(SMCSignal())

        if tf == self.ltf:
            self._update_structure(symbol, tf)
            signal = self._evaluate_ltf_signal(symbol, candle)
            # 記錄訊號統計
            self._signal_total += 1
            reason = signal.reject_reason if signal.reject_reason else (
                signal.direction if signal.direction != "HOLD" else "HOLD"
            )
            self._signal_stats[reason] += 1
            return SMCContext(signal)

        return SMCContext(SMCSignal())

    def htf_bias_changed(self, symbol: str) -> bool:
        return self._htf_bias_flipped.get(symbol, False)

    def get_htf_bias(self, symbol: str) -> str:
        return self._htf_bias.get(symbol, "NEUTRAL")

    def get_signal_stats(self) -> dict:
        """回傳信號統計，用於 heartbeat 診斷"""
        return {
            "total":  self._signal_total,
            "counts": dict(self._signal_stats),
        }

    def reset_signal_stats(self):
        """Reset counters after heartbeat report"""
        self._signal_stats.clear()
        self._signal_total = 0

    def seed_bars(self, symbol: str, tf: str, bars: list):
        for bar in bars:
            self._bars[symbol][tf].append(bar)
        if tf == self.htf:
            self._update_htf_bias(symbol)
        else:
            self._update_structure(symbol, tf)

    # ── HTF Bias ─────────────────────────────────────────────────────────────

    def _update_htf_bias(self, symbol: str):
        """
        T-2 FIX: 比較確認的 Swing Point，而非單根棒的原始高低點。
        HH + HL = BULLISH；LH + LL = BEARISH；否則保持前值。
        """
        bars = list(self._bars[symbol][self.htf])
        if len(bars) < 10:
            self._htf_bias[symbol] = "NEUTRAL"
            return

        n = 3  # HTF swing 確認左右各 3 根
        swing_highs = []
        swing_lows  = []

        for i in range(n, len(bars) - n):
            pivot = bars[i]
            left  = bars[i - n:i]
            right = bars[i + 1:i + n + 1]
            if all(pivot["high"] > b["high"] for b in left + right):
                swing_highs.append(pivot["high"])
            if all(pivot["low"] < b["low"] for b in left + right):
                swing_lows.append(pivot["low"])

        # P1-7: 3-point confirmation — 需連 3 根 swing 同向才翻牌，避震盪 flip-flop
        if len(swing_highs) < 3 or len(swing_lows) < 3:
            # 回退 2-point（過渡期；資料逐步累積後 3-point 生效）
            if len(swing_highs) >= 2 and len(swing_lows) >= 2:
                if swing_highs[-1] > swing_highs[-2] and swing_lows[-1] > swing_lows[-2]:
                    self._htf_bias[symbol] = "BULLISH"
                elif swing_highs[-1] < swing_highs[-2] and swing_lows[-1] < swing_lows[-2]:
                    self._htf_bias[symbol] = "BEARISH"
            return

        hh3 = swing_highs[-1] > swing_highs[-2] > swing_highs[-3]
        hl3 = swing_lows[-1]  > swing_lows[-2]  > swing_lows[-3]
        lh3 = swing_highs[-1] < swing_highs[-2] < swing_highs[-3]
        ll3 = swing_lows[-1]  < swing_lows[-2]  < swing_lows[-3]

        if hh3 and hl3:
            self._htf_bias[symbol] = "BULLISH"
        elif lh3 and ll3:
            self._htf_bias[symbol] = "BEARISH"
        # else: 結構不明確，保持前值

    # ── Structure Update ─────────────────────────────────────────────────────

    def _update_structure(self, symbol: str, tf: str):
        bars = list(self._bars[symbol][tf])
        if len(bars) < self.bos_min_swing + 2:
            return
        self._detect_swing_points(symbol, tf, bars)
        self._detect_order_blocks(symbol, tf, bars)
        self._detect_fvgs(symbol, tf, bars)
        self._detect_eqh_eql(symbol, tf, bars)

    def _detect_swing_points(self, symbol: str, tf: str, bars: list):
        n = self.bos_min_swing
        if len(bars) < 2 * n + 1:
            return
        pivot = bars[-(n + 1)]
        left  = bars[-(2 * n + 1):-(n + 1)]
        right = bars[-n:]

        if all(pivot["high"] > b["high"] for b in left + right):
            self._swing_highs[symbol][tf].append(pivot["high"])

        if all(pivot["low"] < b["low"] for b in left + right):
            self._swing_lows[symbol][tf].append(pivot["low"])

    def _detect_order_blocks(self, symbol: str, tf: str, bars: list):
        if len(bars) < 3:
            return
        cfg = self._cfg.get("liquidity", {}).get("order_block", {})
        if not cfg.get("enabled", True):
            return

        prev2, prev1, curr = bars[-3], bars[-2], bars[-1]
        body2 = abs(prev2["close"] - prev2["open"])
        range2 = prev2["high"] - prev2["low"]
        if range2 == 0:
            return
        body_ratio = body2 / range2

        if body_ratio < self.ob_min_body_ratio:
            return

        # P1-6: OB volume confirmation — smart money footprint 需有量能支持
        vol_mult = cfg.get("volume_multiplier", 1.5)
        if vol_mult > 0 and len(bars) >= 20:
            recent_vols = [b.get("volume", 0) for b in bars[-21:-1]]  # 不含當前
            recent_vols = [v for v in recent_vols if v > 0]
            if recent_vols:
                avg_vol  = sum(recent_vols) / len(recent_vols)
                prev2_v  = prev2.get("volume", 0)
                if avg_vol > 0 and prev2_v < avg_vol * vol_mult:
                    return  # 量能不足，不認列此 OB

        obs = self._order_blocks[symbol][tf]

        # Bearish OB: 上漲蠟燭後轉跌
        if (prev2["close"] > prev2["open"] and
                curr["close"] < curr["open"] and
                curr["close"] < prev2["low"]):
            obs.append({
                "type": "BEARISH",
                "high": prev2["high"],
                "low":  prev2["low"],
                "age":  0,
            })

        # Bullish OB: 下跌蠟燭後轉漲
        if (prev2["close"] < prev2["open"] and
                curr["close"] > curr["open"] and
                curr["close"] > prev2["high"]):
            obs.append({
                "type": "BULLISH",
                "high": prev2["high"],
                "low":  prev2["low"],
                "age":  0,
            })

        # Age OBs and remove stale
        for ob in obs:
            ob["age"] += 1
        self._order_blocks[symbol][tf] = [
            ob for ob in obs if ob["age"] <= self.ob_max_age
        ]

    def _detect_fvgs(self, symbol: str, tf: str, bars: list):
        if len(bars) < 3:
            return
        cfg = self._cfg.get("liquidity", {}).get("fair_value_gap", {})
        if not cfg.get("enabled", True):
            return

        # T-5 FIX: 標記現有 FVG 是否已被填補
        curr = bars[-1]
        for fvg in self._fvgs[symbol][tf]:
            if fvg["filled"]:
                continue
            gap_size = fvg["high"] - fvg["low"]
            if gap_size <= 0:
                continue
            if fvg["type"] == "BULLISH":
                # 價格進入 FVG 達 partial_fill_threshold 比例
                if curr["low"] <= fvg["low"] + gap_size * self.fvg_partial_fill:
                    fvg["filled"] = True
            elif fvg["type"] == "BEARISH":
                if curr["high"] >= fvg["high"] - gap_size * self.fvg_partial_fill:
                    fvg["filled"] = True

        bar1, _, bar3 = bars[-3], bars[-2], bars[-1]

        # Bullish FVG: bar1.high < bar3.low
        if bar3["low"] - bar1["high"] >= self.fvg_min_gap:
            self._fvgs[symbol][tf].append({
                "type":   "BULLISH",
                "high":   bar3["low"],
                "low":    bar1["high"],
                "filled": False,
            })

        # Bearish FVG: bar3.high < bar1.low
        if bar1["low"] - bar3["high"] >= self.fvg_min_gap:
            self._fvgs[symbol][tf].append({
                "type":   "BEARISH",
                "high":   bar1["low"],
                "low":    bar3["high"],
                "filled": False,
            })

        # Keep last 20
        if len(self._fvgs[symbol][tf]) > 20:
            self._fvgs[symbol][tf] = self._fvgs[symbol][tf][-20:]

    def _detect_eqh_eql(self, symbol: str, tf: str, bars: list):
        cfg = self._cfg.get("liquidity", {}).get("equal_highs_lows", {})
        if not cfg.get("enabled", True):
            return

        tol = self.eql_tolerance
        recent = bars[-30:]

        highs = [b["high"] for b in recent]
        lows  = [b["low"]  for b in recent]

        # O(n log n)：排序後只比較相鄰元素；tol 內視為相等
        levels = []
        seen   = set()
        for arr, tag in ((highs, "EQH"), (lows, "EQL")):
            sorted_arr = sorted(arr)
            for i in range(len(sorted_arr) - 1):
                a, b = sorted_arr[i], sorted_arr[i + 1]
                if abs(b - a) <= tol:
                    price = round((a + b) / 2, 2)
                    key   = (tag, price)
                    if key not in seen:
                        seen.add(key)
                        levels.append({"type": tag, "price": price})

        self._eqh_eql[symbol][tf] = levels

    # ── Session Filter ────────────────────────────────────────────────────────

    def _is_no_entry_session(self, ts) -> bool:
        """
        T-6 FIX: 實作 Session Filter。
        Asia_Low_Liquidity (20:00–01:00 UTC) 封鎖進場；
        Pre_London_Watch (05:00–07:00 UTC) 需有位移確認。
        """
        session_cfg = self._cfg.get("entry", {}).get("session_filter", {})
        if not session_cfg.get("enabled", False):
            return False

        no_entry_sessions = session_cfg.get("no_entry_sessions", [])
        if not no_entry_sessions or ts is None:
            return False

        if hasattr(ts, "tzinfo") and ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

        hm = ts.hour * 60 + ts.minute

        for session in no_entry_sessions:
            mode = session.get("mode", "block")
            if mode != "block":
                continue  # require_displacement 由訊號位移過濾處理

            try:
                sh, sm = map(int, session["utc_start"].split(":"))
                eh, em = map(int, session["utc_end"].split(":"))
            except (KeyError, ValueError):
                continue

            start = sh * 60 + sm
            end   = eh * 60 + em

            if start <= end:
                if start <= hm < end:
                    return True
            else:                   # 跨午夜（例如 20:00–01:00）
                if hm >= start or hm < end:
                    return True

        return False

    # ── LTF Signal Evaluation ─────────────────────────────────────────────────

    def _evaluate_ltf_signal(self, symbol: str, candle: dict) -> SMCSignal:
        # T-6 FIX: Session Filter
        ts = candle.get("timestamp")
        if self._is_no_entry_session(ts):
            return SMCSignal(reject_reason="SESSION_FILTER")

        htf_bias = self._htf_bias.get(symbol, "NEUTRAL")
        if htf_bias == "NEUTRAL":
            return SMCSignal(reject_reason="NO_HTF_BIAS")

        # 1. Check Sweep first (highest priority)
        sweep = self._check_sweep(
            symbol, candle, self._eqh_eql[symbol][self.ltf]
        )
        if sweep:
            if self.sweep_htf_align:
                if htf_bias == "BULLISH" and sweep["direction"] != "BULLISH":
                    return SMCSignal(reject_reason="SWEEP_HTF_MISALIGN")
                if htf_bias == "BEARISH" and sweep["direction"] != "BEARISH":
                    return SMCSignal(reject_reason="SWEEP_HTF_MISALIGN")
            direction = "BUY" if sweep["direction"] == "BULLISH" else "SELL"
            if not self._is_in_discount_zone(symbol, direction, candle["close"]):
                return SMCSignal(reject_reason="SWEEP_NOT_IN_DISCOUNT_ZONE")
            sig = self._build_sweep_signal(symbol, candle, sweep, htf_bias)
            if sig.direction != "HOLD" and self.ote_enabled:
                if not self._is_in_ote(symbol, direction, sig.entry_limit_price):
                    return SMCSignal(reject_reason="SWEEP_ENTRY_NOT_IN_OTE")
            return sig

        # 2. Check BOS / CHoCH
        ltf_trigger = self._detect_bos_choch(symbol, candle)
        if not ltf_trigger:
            return SMCSignal(reject_reason="NO_LTF_TRIGGER")

        # 3. Alignment check
        if htf_bias == "BULLISH" and ltf_trigger not in ("BULLISH_BOS", "BULLISH_CHOCH"):
            return SMCSignal(reject_reason="NO_VALID_LTF_TRIGGER")
        if htf_bias == "BEARISH" and ltf_trigger not in ("BEARISH_BOS", "BEARISH_CHOCH"):
            return SMCSignal(reject_reason="NO_VALID_LTF_TRIGGER")

        # 4. Build entry signal based on trigger type
        is_choch  = "CHOCH" in ltf_trigger
        direction = "BUY" if "BULLISH" in ltf_trigger else "SELL"

        # E1 Premium/Discount（以當前 close 做粗濾）
        if not self._is_in_discount_zone(symbol, direction, candle["close"]):
            return SMCSignal(reject_reason="NOT_IN_DISCOUNT_ZONE")

        # E6 OTE 在 _build_structure_signal 內以「entry 位置」精判
        sig = self._build_structure_signal(symbol, candle, ltf_trigger, htf_bias, is_choch)
        if sig.direction != "HOLD" and self.ote_enabled:
            if not self._is_in_ote(symbol, direction, sig.entry_limit_price):
                return SMCSignal(reject_reason="ENTRY_NOT_IN_OTE")
        return sig

    def _detect_bos_choch(self, symbol: str, candle: dict) -> Optional[str]:
        sh_list  = self._swing_highs[symbol][self.ltf]
        sl_list  = self._swing_lows[symbol][self.ltf]
        htf_bias = self._htf_bias.get(symbol, "NEUTRAL")

        if not sh_list or not sl_list:
            return None

        last_sh = sh_list[-1]
        last_sl = sl_list[-1]
        close   = candle["close"]

        # T-7 FIX: BOS 也需要位移確認，避免假突破
        if htf_bias == "BULLISH" and close > last_sh:
            if self._has_displacement(symbol, self.ltf, "BULLISH"):
                return "BULLISH_BOS"
        if htf_bias == "BEARISH" and close < last_sl:
            if self._has_displacement(symbol, self.ltf, "BEARISH"):
                return "BEARISH_BOS"

        # CHoCH: reversal
        if htf_bias == "BULLISH" and close < last_sl:
            if self._has_displacement(symbol, self.ltf, "BEARISH"):
                return "BEARISH_CHOCH"
        if htf_bias == "BEARISH" and close > last_sh:
            if self._has_displacement(symbol, self.ltf, "BULLISH"):
                return "BULLISH_CHOCH"

        return None

    def _has_displacement(self, symbol: str, tf: str, direction: str) -> bool:
        """BOS/CHoCH 需至少 N 根強勢位移 K 棒確認"""
        bars = list(self._bars[symbol][tf])
        n    = self.choch_displacement
        if len(bars) < n:
            return False
        recent = bars[-n:]
        if direction == "BULLISH":
            return all(b["close"] > b["open"] for b in recent)
        else:
            return all(b["close"] < b["open"] for b in recent)

    def _check_sweep(self, symbol: str, candle: dict, levels: list) -> Optional[dict]:
        body = abs(candle["close"] - candle["open"])
        if body == 0:
            return None

        lower_wick = min(candle["open"], candle["close"]) - candle["low"]
        upper_wick = candle["high"] - max(candle["open"], candle["close"])

        cfg = self._cfg.get("liquidity", {}).get("sweep", {})
        if not cfg.get("enabled", True):
            return None

        for level in levels:
            if (level["type"] == "EQL" and
                    candle["low"] < level["price"] and
                    candle["close"] > level["price"] and
                    lower_wick >= body * self.sweep_wick_ratio):
                return {
                    "direction":    "BULLISH",
                    "swept_level":  level["price"],
                    "strength":     lower_wick / body,
                }
            if (level["type"] == "EQH" and
                    candle["high"] > level["price"] and
                    candle["close"] < level["price"] and
                    upper_wick >= body * self.sweep_wick_ratio):
                return {
                    "direction":    "BEARISH",
                    "swept_level":  level["price"],
                    "strength":     upper_wick / body,
                }
        return None

    def _build_sweep_signal(self, symbol: str, candle: dict,
                             sweep: dict, htf_bias: str) -> SMCSignal:
        direction = "BUY" if sweep["direction"] == "BULLISH" else "SELL"
        price     = candle["close"]

        ob, fvg   = self._find_ob_fvg(symbol, direction, self.sweep_entry_model)
        entry, sl, tp1, tp2, inval, atr_val, tp_struct = self._calc_levels(
            direction, price, ob, fvg, candle, symbol=symbol,
        )
        hard_sl   = self._calc_hard_sl(direction, sl)
        rrr       = self._calc_rrr(direction, entry, sl, tp1)

        confluence = bool(ob and fvg and not (ob["high"] < fvg["low"] or ob["low"] > fvg["high"]))
        conds = ["SWEEP", "HTF_ALIGN"]
        if ob:  conds.append("OB")
        if fvg: conds.append("FVG")
        if confluence:     conds.append("CONFLUENCE")
        if tp_struct:      conds.append("STRUCT_TP")
        if atr_val:        conds.append("ATR_SL")

        return SMCSignal(
            direction          = direction,
            source             = "SWEEP",
            htf_bias           = htf_bias,
            entry_limit_price  = entry,
            stop_loss          = sl,
            hard_sl_price      = hard_sl,
            take_profit_1      = tp1,
            take_profit_2      = tp2,
            invalidation_level = inval,
            rrr                = rrr,
            price_at_signal    = price,
            timeframe          = self.ltf,
            swept_level        = sweep["swept_level"],
            ob_level           = ob["low"] if ob else None,
            fvg_range          = f"${fvg['low']:,.0f}–${fvg['high']:,.0f}" if fvg else None,
            conditions_met     = conds,
            atr_value          = atr_val,
            confluence         = confluence,
            tp_is_structural   = tp_struct,
        )

    def _build_structure_signal(self, symbol: str, candle: dict,
                                  ltf_trigger: str, htf_bias: str,
                                  is_choch: bool) -> SMCSignal:
        direction = "BUY" if "BULLISH" in ltf_trigger else "SELL"
        price     = candle["close"]

        entry_model = self.choch_entry_model if is_choch else self.bos_entry_model
        ob, fvg   = self._find_ob_fvg(symbol, direction, entry_model)

        # E3 Rejection candle：當前蠟燭需對 zone 有拒絕動作
        zone = ob or fvg
        if zone and self.rej_enabled:
            if not self._has_rejection(candle, direction, zone["low"], zone["high"]):
                return SMCSignal(reject_reason="NO_REJECTION_CANDLE")

        entry, sl, tp1, tp2, inval, atr_val, tp_struct = self._calc_levels(
            direction, price, ob, fvg, candle, symbol=symbol,
        )
        hard_sl   = self._calc_hard_sl(direction, sl)
        rrr       = self._calc_rrr(direction, entry, sl, tp1)
        source    = "CHOCH" if is_choch else "BOS"

        disp_bars = self.choch_displacement if is_choch else None

        # 計算 retrace 比例（供 Telegram 顯示）
        retrace = None
        imp = self._last_impulse(symbol, direction)
        if imp:
            swing_low, swing_high = imp
            rng = swing_high - swing_low
            if rng > 0:
                retrace = (swing_high - entry) / rng if direction == "BUY" else (entry - swing_low) / rng

        confluence = bool(ob and fvg and not (ob["high"] < fvg["low"] or ob["low"] > fvg["high"]))
        conds = [source, "HTF_ALIGN"]
        if ob:  conds.append("OB")
        if fvg: conds.append("FVG")
        if confluence:  conds.append("CONFLUENCE")
        if self.rej_enabled: conds.append("REJECTION")
        if tp_struct:   conds.append("STRUCT_TP")
        if atr_val:     conds.append("ATR_SL")

        return SMCSignal(
            direction          = direction,
            source             = source,
            htf_bias           = htf_bias,
            entry_limit_price  = entry,
            stop_loss          = sl,
            hard_sl_price      = hard_sl,
            take_profit_1      = tp1,
            take_profit_2      = tp2,
            invalidation_level = inval,
            rrr                = rrr,
            price_at_signal    = price,
            timeframe          = self.ltf,
            displacement_bars  = disp_bars,
            ob_level           = ob["low"] if ob else None,
            fvg_range          = f"${fvg['low']:,.0f}–${fvg['high']:,.0f}" if fvg else None,
            conditions_met     = conds,
            atr_value          = atr_val,
            retrace_pct        = retrace,
            confluence         = confluence,
            tp_is_structural   = tp_struct,
        )

    # ── Level Calculations ───────────────────────────────────────────────────

    def _find_ob_fvg(self, symbol: str, direction: str, entry_model: str = "ob_fvg"):
        """
        依 config 的 entry_model 決定回踩參考；
        若 confluence.enabled，優先選擇與 FVG 重疊的 OB，勝率較高。
        """
        obs  = self._order_blocks[symbol][self.ltf]
        fvgs = self._fvgs[symbol][self.ltf]

        ob_type  = "BULLISH" if direction == "BUY" else "BEARISH"
        fvg_type = "BULLISH" if direction == "BUY" else "BEARISH"

        ob  = next((o for o in reversed(obs)  if o["type"] == ob_type), None)
        fvg = next((f for f in reversed(fvgs) if f["type"] == fvg_type and not f["filled"]), None)

        # E2: Confluence — 若有多個 OB，優先挑與最新 FVG 重疊的
        if self.conf_enabled and fvg and obs:
            for o in reversed(obs):
                if o["type"] != ob_type:
                    continue
                # 價格區間重疊判定
                if not (o["high"] < fvg["low"] or o["low"] > fvg["high"]):
                    ob = o
                    break

        if entry_model == "fvg_only":
            return None, fvg
        if entry_model == "ob_only":
            return ob, None
        return ob, fvg

    # ── E1 Premium/Discount ──────────────────────────────────────────────────

    def _is_in_discount_zone(self, symbol: str, direction: str, price: float) -> bool:
        """
        BUY：價格在 HTF range 下半（discount）；
        SELL：價格在 HTF range 上半（premium）。
        Range 來源：最近 HTF swing high/low 配對。
        """
        if not self.pd_enabled:
            return True
        htf_bars = list(self._bars[symbol][self.htf])
        if len(htf_bars) < 20:
            return True  # 資料不足不擋
        recent = htf_bars[-self.pd_relax_bars:] if self.pd_relax_bars > 0 else htf_bars[-20:]
        hi = max(b["high"] for b in recent)
        lo = min(b["low"]  for b in recent)
        rng = hi - lo
        if rng <= 0:
            return True
        pos = (price - lo) / rng   # 0 = 最低、1 = 最高
        if direction == "BUY":
            return pos <= self.pd_threshold
        else:
            return pos >= (1 - self.pd_threshold)

    # ── E3 Rejection Candle ──────────────────────────────────────────────────

    def _has_rejection(self, candle: dict, direction: str,
                       zone_low: float, zone_high: float) -> bool:
        """
        BUY：下影線穿入 zone 但收盤回到 zone 之上，且下影 > 實體 × ratio；
        SELL：對稱。
        """
        if not self.rej_enabled:
            return True
        body  = abs(candle["close"] - candle["open"])
        if body == 0:
            return False
        low, high, op, cl = candle["low"], candle["high"], candle["open"], candle["close"]
        if direction == "BUY":
            lower_wick = min(op, cl) - low
            return (low <= zone_high and cl > zone_high
                    and lower_wick >= body * self.rej_wick_ratio)
        else:
            upper_wick = high - max(op, cl)
            return (high >= zone_low and cl < zone_low
                    and upper_wick >= body * self.rej_wick_ratio)

    # ── E5 ATR ───────────────────────────────────────────────────────────────

    def _calc_atr(self, symbol: str) -> Optional[float]:
        if not self.atr_enabled:
            return None
        bars = list(self._bars[symbol][self.atr_tf])[-(self.atr_period + 1):]
        if len(bars) < self.atr_period + 1:
            return None
        trs = []
        for i in range(1, len(bars)):
            prev_close = bars[i - 1]["close"]
            h, l = bars[i]["high"], bars[i]["low"]
            trs.append(max(h - l, abs(h - prev_close), abs(l - prev_close)))
        return sum(trs) / len(trs)

    # ── E6 OTE Fibonacci ─────────────────────────────────────────────────────

    def _last_impulse(self, symbol: str, direction: str):
        """回傳 (swing_low, swing_high) 作為最近 impulse 的起訖"""
        sh = list(self._swing_highs[symbol][self.ltf])
        sl = list(self._swing_lows[symbol][self.ltf])
        if not sh or not sl:
            return None
        # 取最新各一點作為 impulse 兩端
        return (sl[-1], sh[-1])

    def _is_in_ote(self, symbol: str, direction: str, price: float) -> bool:
        if not self.ote_enabled:
            return True
        imp = self._last_impulse(symbol, direction)
        if imp is None:
            return True
        swing_low, swing_high = imp
        rng = swing_high - swing_low
        if rng <= 0:
            return True
        if direction == "BUY":
            retrace = (swing_high - price) / rng  # 0 = 頂、1 = 底
        else:
            retrace = (price - swing_low) / rng
        return self.ote_min <= retrace <= self.ote_max

    def _calc_levels(self, direction: str, price: float,
                     ob: dict, fvg: dict, candle: dict,
                     symbol: str = None):
        """
        進場：
        - E4: ob_entry_mode = midpoint → entry = OB_low + 25% range（BUY）
        - edge（原邏輯）：entry = OB_low 邊緣
        SL：
        - E5: ATR × multiplier（優先），否則 fallback 0.5% buffer
        TP：
        - X1: 優先用結構（最近對向 swing、對向 EQH/EQL、對向 OB），fallback 2R / 3R
        """
        atr = self._calc_atr(symbol) if symbol else None

        # Entry 決定 ──────────────────────────────────────────────
        zone = ob or fvg
        if zone:
            if ob and self.ob_entry_mode == "midpoint":
                rng = ob["high"] - ob["low"]
                entry = ob["low"] + 0.25 * rng if direction == "BUY" else ob["high"] - 0.25 * rng
            else:
                entry = zone["low"] if direction == "BUY" else zone["high"]
        else:
            entry = price

        # SL 決定 ─────────────────────────────────────────────────
        zone_edge = (zone["low"] if direction == "BUY" else zone["high"]) if zone else price
        if atr:
            atr_sl_dist = atr * self.atr_multiplier
            pct         = atr_sl_dist / entry if entry else 0
            pct         = max(self.atr_min_pct, min(self.atr_max_pct, pct))
            if direction == "BUY":
                sl = zone_edge * (1 - pct)
            else:
                sl = zone_edge * (1 + pct)
        else:
            sl_buf = 0.005
            if direction == "BUY":
                sl = zone_edge * (1 - sl_buf)
            else:
                sl = zone_edge * (1 + sl_buf)

        # TP 決定（X1 結構目標） ───────────────────────────────────
        tp1, tp2, tp_structural = self._calc_structural_tps(
            symbol, direction, entry, sl
        )

        # INVALIDATION
        if direction == "BUY":
            inval = sl * (1 - self._hard_sl_buffer)
        else:
            inval = sl * (1 + self._hard_sl_buffer)

        return (round(entry, 2), round(sl, 2),
                round(tp1, 2), round(tp2, 2), round(inval, 2),
                atr, tp_structural)

    def _calc_structural_tps(self, symbol: str, direction: str,
                              entry: float, sl: float):
        """
        TP1：對向最近 swing（若不存在或太近，fallback 2R）
        TP2：對向 EQH/EQL 或對向 OB（若不存在，fallback 3R）
        回傳 (tp1, tp2, is_structural)
        """
        r = abs(entry - sl)
        fallback_tp1 = entry + r * 2 if direction == "BUY" else entry - r * 2
        fallback_tp2 = entry + r * 3 if direction == "BUY" else entry - r * 3
        if not symbol:
            return fallback_tp1, fallback_tp2, False

        sh = list(self._swing_highs[symbol][self.ltf])
        sl_pts = list(self._swing_lows[symbol][self.ltf])
        eqs = self._eqh_eql[symbol][self.ltf]
        obs = self._order_blocks[symbol][self.ltf]

        tp1 = None
        tp2 = None

        if direction == "BUY":
            # TP1 = 最近一個在 entry 上方的 swing high
            candidates = [s for s in sh if s > entry + r]  # 至少 1R 以上
            if candidates:
                tp1 = min(candidates)
            # TP2 = EQH 或對向（BEARISH）OB 的 low
            eq_hs = [e["price"] for e in eqs if e["type"] == "EQH" and e["price"] > (tp1 or entry)]
            if eq_hs:
                tp2 = min(eq_hs)
            else:
                op = [o["low"] for o in obs if o["type"] == "BEARISH" and o["low"] > (tp1 or entry)]
                if op:
                    tp2 = min(op)
        else:
            candidates = [s for s in sl_pts if s < entry - r]
            if candidates:
                tp1 = max(candidates)
            eq_ls = [e["price"] for e in eqs if e["type"] == "EQL" and e["price"] < (tp1 or entry)]
            if eq_ls:
                tp2 = max(eq_ls)
            else:
                op = [o["high"] for o in obs if o["type"] == "BULLISH" and o["high"] < (tp1 or entry)]
                if op:
                    tp2 = max(op)

        structural = tp1 is not None or tp2 is not None
        return (tp1 or fallback_tp1), (tp2 or fallback_tp2), structural

    def _calc_hard_sl(self, direction: str, sl: float) -> float:
        buf = self._hard_sl_buffer
        if direction == "BUY":
            return round(sl * (1 - buf), 2)
        else:
            return round(sl * (1 + buf), 2)

    def _calc_rrr(self, direction: str, entry: float,
                  sl: float, tp1: float) -> float:
        if entry == sl:
            return 0.0
        risk   = abs(entry - sl)
        reward = abs(tp1 - entry)
        return round(reward / risk, 2)
