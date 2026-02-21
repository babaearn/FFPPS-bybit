"""
Risk manager — pre-trade gate checks.
All gates must pass before entry is allowed at T-90s.
"""

import logging
from typing import TYPE_CHECKING

from config import RuntimeConfig
from scanner.funding_scanner import FundingOpportunity

if TYPE_CHECKING:
    from engine.position_manager import PositionManager
    from engine.pnl_engine import PnlEngine

logger = logging.getLogger(__name__)


class RiskManager:
    """
    Stateless pre-trade gate evaluator.
    All state sourced from PositionManager and PnlEngine.
    """

    def __init__(
        self,
        cfg: RuntimeConfig,
        position_manager: "PositionManager",
        pnl_engine: "PnlEngine",
    ) -> None:
        self._cfg = cfg
        self._pm = position_manager
        self._pnl = pnl_engine

    async def check_pre_entry(
        self, opp: FundingOpportunity
    ) -> tuple[bool, str]:
        """
        Run all pre-trade gates.
        Returns (allowed: bool, reason: str).
        reason is empty string if allowed.
        """
        cfg = self._cfg

        # Gate 1: Emergency stop
        if cfg.emergency_stop:
            return False, "Emergency stop is active"

        # Gate 2: Funding rate still meets threshold
        if abs(opp.funding_rate) < cfg.funding_threshold:
            return False, (
                f"Funding rate {opp.funding_rate:.6f} below threshold {cfg.funding_threshold}"
            )

        # Gate 3: Open interest still sufficient
        if opp.open_interest_usd < cfg.min_oi_usd:
            return False, (
                f"OI ${opp.open_interest_usd:,.0f} below minimum ${cfg.min_oi_usd:,.0f}"
            )

        # Gate 4: Daily loss limit
        today_loss = self._pnl.today_net_loss()
        if today_loss >= cfg.max_daily_loss_usd:
            return False, (
                f"Daily loss limit hit: ${today_loss:.2f} >= ${cfg.max_daily_loss_usd:.2f} — "
                f"no new trades until 00:00 UTC"
            )

        # Gate 5: Symbol not already in active position (final check)
        if self._pm.is_open(opp.symbol):
            return False, f"Position already open for {opp.symbol}"

        logger.info(
            "RiskManager: all gates passed for %s %s (rate=%.4f%%, OI=$%.0f)",
            opp.direction, opp.symbol,
            opp.funding_rate * 100,
            opp.open_interest_usd,
        )
        return True, ""

    def daily_loss_exceeded(self) -> bool:
        """Quick check — used by scanner to suppress alerts after limit hit."""
        return self._pnl.today_net_loss() >= self._cfg.max_daily_loss_usd
