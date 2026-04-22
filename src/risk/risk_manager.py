import os
import yaml
from datetime import date, datetime, timezone


class RiskManager:
    def __init__(self):
        config_dir = os.getenv("CONFIG_DIR", "/app/config")
        config_path = os.path.join(config_dir, "risk_config.yaml")
        with open(config_path, "r") as f:
            self._cfg = yaml.safe_load(f)

        self._account_equity = None

        # Daily loss tracking
        self._daily_pnl:  float = 0.0
        self._daily_date: date  = datetime.now(timezone.utc).date()

    def set_equity(self, equity: float):
        self._account_equity = equity

    def record_trade_pnl(self, pnl: float):
        """每次出場後呼叫，追蹤當日累計 PnL"""
        today = datetime.now(timezone.utc).date()
        if self._daily_date != today:
            self._daily_pnl  = 0.0
            self._daily_date = today
        self._daily_pnl += pnl

    def is_daily_loss_exceeded(self) -> bool:
        """當日虧損超過 max_daily_loss_pct × equity 時返回 True"""
        if self._account_equity is None or self._account_equity <= 0:
            return False
        max_pct = self._cfg.get("account", {}).get("max_daily_loss_pct", 0.02)
        return self._daily_pnl < -(self._account_equity * max_pct)

    def calc_notional(self, signal) -> float:
        """
        下單金額計算：
        - risk_per_trade_usd 有設定時，以固定虧損金額反推倉位大小
        - 否則 fallback 到 equity × risk_per_trade_pct
        notional = risk_amount / sl_distance_pct
        """
        min_notional = self._cfg["btcusd"]["min_notional_usd"]
        max_notional = self._cfg["btcusd"]["max_notional_usd"]

        # Fixed USD risk takes priority over percentage-based risk
        risk_usd_fixed = self._cfg.get("position", {}).get("risk_per_trade_usd", None)
        if risk_usd_fixed is not None:
            risk_amount = float(risk_usd_fixed)
        elif self._account_equity and self._account_equity > 0:
            risk_amount = self._account_equity * self._cfg["position"]["risk_per_trade_pct"]
        else:
            return min_notional

        entry = getattr(signal, "entry_limit_price", None)
        sl    = getattr(signal, "stop_loss", None)

        if entry and sl and entry != sl:
            sl_distance_pct = abs(entry - sl) / entry
            notional = risk_amount / sl_distance_pct if sl_distance_pct > 0 else risk_amount / 0.005
        else:
            notional = risk_amount / 0.005

        notional = max(min_notional, min(max_notional, notional))
        return round(notional, 2)

    def is_auto_trade_enabled(self) -> bool:
        return self._cfg.get("auto_trade", False)

    def is_market_order_mode(self) -> bool:
        return self._cfg.get("use_market_order", False)

    def get_min_rrr(self) -> float:
        return self._cfg.get("position", {}).get("min_rrr", 1.5)

    def get_hard_sl_buffer(self) -> float:
        return self._cfg.get("hard_sl", {}).get("buffer_pct", 0.003)

    def get_server_stop_buffer(self) -> float:
        return self._cfg.get("server_side_stop", {}).get("buffer_pct", 0.005)

    def get_limit_order_timeout(self) -> int:
        return self._cfg.get("btcusd", {}).get("limit_order_timeout_seconds", 300)

    def get_circuit_breaker_config(self) -> dict:
        return self._cfg.get("circuit_breaker", {})
