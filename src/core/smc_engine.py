import os
import yaml
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Optional


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
    timeframe:          str   = "M15"
    conditions_met:     list  = field(default_factory=list)
    reject_reason:      str   = ""
    # BOS / CHoCH specific
    displacement_bars:  Optional[int]   = None
    # Sweep specific
    swept_level:        Optional[float] = None
    # OB / FVG levels for notification
    ob_level:           Optional[float] = None
    fvg_range:          Optional[str]   = None


class SMCContext:
    def __init__(self, signal: SMCSignal):
        self._signal = signal

    def has_candidate(self) -> bool:
        return self._signal.direction != "HOLD"

    def get_signal(self) -> SMCSignal:
        return self._signal


class SMCEngine:
    def __init__(self):
        config_dir  = os.getenv("CONFIG_DIR", "/app/config")
        config_path = os.path.join(config_dir, "smc_config.yaml")
        with open(config_path, "r") as f:
            self._cfg = yaml.safe_load(f)

        self.htf = self._cfg["timeframes"]["htf"]   # H4
        self.mtf = self._cfg["timeframes"]["mtf"]   # H1
        self.ltf = self._cfg["timeframes"]["ltf"]   # M15

        # 每個 symbol 的 K 棒緩存
        self._bars:       dict = defaultdict(lambda: defaultdict(lambda: deque(maxlen=300)))
        self._htf_bias:   dict = {}
        self._htf_bias_flipped: dict = {}

        # Structure state
        self._swing_highs: dict = defaultdict(lambda: defaultdict(list))
        self._swing_lows:  dict = defaultdict(lambda: defaultdict(list))
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

        hard_sl_cfg              = {}  # loaded from risk_config in position_manager
        self._hard_sl_buffer     = 0.003

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
            return SMCContext(signal)

        return SMCContext(SMCSignal())

    def htf_bias_changed(self, symbol: str) -> bool:
        return self._htf_bias_flipped.get(symbol, False)

    def get_htf_bias(self, symbol: str) -> str:
        return self._htf_bias.get(symbol, "NEUTRAL")

    def seed_bars(self, symbol: str, tf: str, bars: list):
        for bar in bars:
            self._bars[symbol][tf].append(bar)
        if tf == self.htf:
            self._update_htf_bias(symbol)
        else:
            self._update_structure(symbol, tf)

    # ── HTF Bias ─────────────────────────────────────────────────────────────

    def _update_htf_bias(self, symbol: str):
        bars = list(self._bars[symbol][self.htf])
        if len(bars) < 10:
            self._htf_bias[symbol] = "NEUTRAL"
            return

        highs = [b["high"] for b in bars[-20:]]
        lows  = [b["low"]  for b in bars[-20:]]

        hh = highs[-1] > max(highs[:-1])
        hl = lows[-1]  > min(lows[:-1])
        lh = highs[-1] < max(highs[:-1])
        ll = lows[-1]  < min(lows[:-1])

        if hh and hl:
            self._htf_bias[symbol] = "BULLISH"
        elif lh and ll:
            self._htf_bias[symbol] = "BEARISH"
        # else keep previous

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
            if len(self._swing_highs[symbol][tf]) > 50:
                self._swing_highs[symbol][tf].pop(0)

        if all(pivot["low"] < b["low"] for b in left + right):
            self._swing_lows[symbol][tf].append(pivot["low"])
            if len(self._swing_lows[symbol][tf]) > 50:
                self._swing_lows[symbol][tf].pop(0)

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

        bar1, _, bar3 = bars[-3], bars[-2], bars[-1]

        # Bullish FVG: bar1.high < bar3.low
        if bar3["low"] - bar1["high"] >= self.fvg_min_gap:
            self._fvgs[symbol][tf].append({
                "type": "BULLISH",
                "high": bar3["low"],
                "low":  bar1["high"],
                "filled": False,
            })

        # Bearish FVG: bar3.high < bar1.low
        if bar1["low"] - bar3["high"] >= self.fvg_min_gap:
            self._fvgs[symbol][tf].append({
                "type": "BEARISH",
                "high": bar1["low"],
                "low":  bar3["high"],
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

        levels = []
        for i, h in enumerate(highs):
            for j in range(i + 1, len(highs)):
                if abs(highs[j] - h) <= tol:
                    levels.append({"type": "EQH", "price": (h + highs[j]) / 2})
                    break
        for i, lo in enumerate(lows):
            for j in range(i + 1, len(lows)):
                if abs(lows[j] - lo) <= tol:
                    levels.append({"type": "EQL", "price": (lo + lows[j]) / 2})
                    break

        self._eqh_eql[symbol][tf] = levels

    # ── LTF Signal Evaluation ─────────────────────────────────────────────────

    def _evaluate_ltf_signal(self, symbol: str, candle: dict) -> SMCSignal:
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
            return self._build_sweep_signal(symbol, candle, sweep, htf_bias)

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
        is_choch = "CHOCH" in ltf_trigger
        return self._build_structure_signal(symbol, candle, ltf_trigger, htf_bias, is_choch)

    def _detect_bos_choch(self, symbol: str, candle: dict) -> Optional[str]:
        bars      = list(self._bars[symbol][self.ltf])
        sh_list   = self._swing_highs[symbol][self.ltf]
        sl_list   = self._swing_lows[symbol][self.ltf]
        htf_bias  = self._htf_bias.get(symbol, "NEUTRAL")

        if not sh_list or not sl_list:
            return None

        last_sh = sh_list[-1]
        last_sl = sl_list[-1]
        close   = candle["close"]

        # BOS: continuation of HTF bias
        if htf_bias == "BULLISH" and close > last_sh:
            return "BULLISH_BOS"
        if htf_bias == "BEARISH" and close < last_sl:
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
        """CHoCH 需至少 N 根強勢位移 K 棒確認"""
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

        ob, fvg   = self._find_ob_fvg(symbol, direction)
        entry, sl, tp1, tp2, inval = self._calc_levels(
            direction, price, ob, fvg, candle
        )
        hard_sl   = self._calc_hard_sl(direction, sl)
        rrr       = self._calc_rrr(direction, entry, sl, tp1)

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
            conditions_met     = ["SWEEP", "HTF_ALIGN", "OB" if ob else "FVG"],
        )

    def _build_structure_signal(self, symbol: str, candle: dict,
                                  ltf_trigger: str, htf_bias: str,
                                  is_choch: bool) -> SMCSignal:
        direction = "BUY" if "BULLISH" in ltf_trigger else "SELL"
        price     = candle["close"]

        ob, fvg   = self._find_ob_fvg(symbol, direction)
        entry, sl, tp1, tp2, inval = self._calc_levels(
            direction, price, ob, fvg, candle
        )
        hard_sl   = self._calc_hard_sl(direction, sl)
        rrr       = self._calc_rrr(direction, entry, sl, tp1)
        source    = "CHOCH" if is_choch else "BOS"

        disp_bars = None
        if is_choch:
            disp_bars = self.choch_displacement

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
            conditions_met     = [source, "HTF_ALIGN", "OB" if ob else "", "FVG" if fvg else ""],
        )

    # ── Level Calculations ───────────────────────────────────────────────────

    def _find_ob_fvg(self, symbol: str, direction: str):
        obs  = self._order_blocks[symbol][self.ltf]
        fvgs = self._fvgs[symbol][self.ltf]

        ob_type  = "BULLISH" if direction == "BUY" else "BEARISH"
        fvg_type = "BULLISH" if direction == "BUY" else "BEARISH"

        ob  = next((o for o in reversed(obs)  if o["type"] == ob_type), None)
        fvg = next((f for f in reversed(fvgs) if f["type"] == fvg_type and not f["filled"]), None)
        return ob, fvg

    def _calc_levels(self, direction: str, price: float,
                     ob: dict, fvg: dict, candle: dict):
        if direction == "BUY":
            if ob:
                entry = ob["low"]
                sl    = ob["low"] * 0.998
            elif fvg:
                entry = fvg["low"]
                sl    = fvg["low"] * 0.998
            else:
                entry = price
                sl    = price * 0.985

            inval  = sl
            tp1    = entry + (entry - sl) * 2
            tp2    = entry + (entry - sl) * 3
        else:
            if ob:
                entry = ob["high"]
                sl    = ob["high"] * 1.002
            elif fvg:
                entry = fvg["high"]
                sl    = fvg["high"] * 1.002
            else:
                entry = price
                sl    = price * 1.015

            inval  = sl
            tp1    = entry - (sl - entry) * 2
            tp2    = entry - (sl - entry) * 3

        entry = round(entry, 2)
        sl    = round(sl, 2)
        tp1   = round(tp1, 2)
        tp2   = round(tp2, 2)
        inval = round(inval, 2)
        return entry, sl, tp1, tp2, inval

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
