import logging
import os
from datetime import date, datetime, timezone

from src.config.loader import get_risk_config

log = logging.getLogger(__name__)


def _iso_week_key(d: date) -> str:
    iso = d.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


class RiskManager:
    def __init__(self):
        self._cfg            = get_risk_config()
        self._account_equity = None
        self._db             = None   # 由 load_state(db) 注入

        today = datetime.now(timezone.utc).date()
        # Daily loss tracking
        self._daily_pnl:   float = 0.0
        self._daily_date:  date  = today

        # AT-H5: Weekly loss tracking；與 db.py 存的 weekly_iso 對齊
        self._weekly_pnl:  float = 0.0
        self._weekly_iso:  str   = _iso_week_key(today)

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def daily_pnl(self) -> float:
        """BE-H4 FIX: 公開屬性，外部不應直接存取 _daily_pnl"""
        self._check_date_rollover()
        return self._daily_pnl

    @property
    def weekly_pnl(self) -> float:
        self._check_date_rollover()
        return self._weekly_pnl

    # ── Date Rollover ─────────────────────────────────────────────────────────

    def _check_date_rollover(self):
        now      = datetime.now(timezone.utc)
        today    = now.date()
        week_key = _iso_week_key(today)

        if self._daily_date != today:
            self._daily_pnl  = 0.0
            self._daily_date = today

        if self._weekly_iso != week_key:
            self._weekly_pnl = 0.0
            self._weekly_iso = week_key

    # ── Persistence（對應 db.py 的 load_risk_state / save_risk_state）──────────

    async def load_state(self, db):
        """啟動時從 DB 恢復每日 / 每週 PnL，跨重啟保護風控上限"""
        self._db = db
        try:
            row = await db.load_risk_state()
        except Exception as e:
            log.warning("load_risk_state query failed: %s", e)
            return
        if not row:
            return

        today      = datetime.now(timezone.utc).date()
        today_iso  = today.isoformat()
        today_week = _iso_week_key(today)

        # 只在 DB 記錄與今天同日 / 同週時恢復數字；跨期則維持 0
        if row.get("daily_date") == today_iso:
            self._daily_pnl = float(row.get("daily_pnl") or 0.0)
        if row.get("weekly_iso") == today_week:
            self._weekly_pnl = float(row.get("weekly_pnl") or 0.0)

    async def _persist(self):
        """寫回當前每日 / 每週 PnL；供 record_trade_pnl 與 graceful shutdown 呼叫"""
        if not self._db:
            return
        try:
            await self._db.save_risk_state(
                self._daily_pnl,
                self._daily_date.isoformat(),
                self._weekly_pnl,
                self._weekly_iso,
            )
        except Exception as e:
            log.warning("save_risk_state failed: %s", e)

    # ── Equity ────────────────────────────────────────────────────────────────

    def set_equity(self, equity: float):
        self._account_equity = equity

    def get_equity(self) -> float:
        return self._account_equity or 0.0

    # ── PnL Recording ─────────────────────────────────────────────────────────

    def record_trade_pnl(self, pnl: float):
        """每次出場後呼叫，追蹤當日 + 本週累計 PnL，並非阻塞寫回 DB"""
        import asyncio
        self._check_date_rollover()
        self._daily_pnl  += pnl
        self._weekly_pnl += pnl
        if self._db is not None:
            try:
                asyncio.create_task(self._persist())
            except RuntimeError:
                # 無 running loop（不應發生，但保底）
                pass

    # ── Loss Guards ───────────────────────────────────────────────────────────

    def is_daily_loss_exceeded(self) -> bool:
        """當日虧損超過 max_daily_loss_pct × equity 時返回 True"""
        if self._account_equity is None or self._account_equity <= 0:
            return False
        self._check_date_rollover()
        max_pct = self._cfg.get("account", {}).get("max_daily_loss_pct", 0.02)
        return self._daily_pnl < -(self._account_equity * max_pct)

    def is_weekly_loss_exceeded(self) -> bool:
        """AT-H5 FIX: 本週虧損超過 max_weekly_loss_pct × equity 時返回 True"""
        if self._account_equity is None or self._account_equity <= 0:
            return False
        self._check_date_rollover()
        max_pct = self._cfg.get("account", {}).get("max_weekly_loss_pct", 0.05)
        return self._weekly_pnl < -(self._account_equity * max_pct)

    # ── Position Sizing ───────────────────────────────────────────────────────

    def calc_notional(self, signal) -> float:
        """
        下單金額計算：
        - risk_per_trade_usd 有設定時，以固定虧損金額反推倉位大小
        - 否則 fallback 到 equity × risk_per_trade_pct
        notional = risk_amount / sl_distance_pct
        """
        min_notional = self._cfg["btcusd"]["min_notional_usd"]
        max_notional = self._cfg["btcusd"]["max_notional_usd"]

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

        return round(max(min_notional, min(max_notional, notional)), 2)

    # ── Config Accessors ──────────────────────────────────────────────────────

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

    def get_spread_filter_pct(self) -> float:
        return self._cfg.get("btcusd", {}).get("spread_filter_pct", 0.001)

    def get_circuit_breaker_config(self) -> dict:
        return self._cfg.get("circuit_breaker", {})

    # ── Snapshot ──────────────────────────────────────────────────────────────

    def snapshot(self) -> dict:
        self._check_date_rollover()
        return {
            "daily_pnl":  self._daily_pnl,
            "weekly_pnl": self._weekly_pnl,
            "equity":     self._account_equity or 0.0,
        }
