"""
Hedge manager.
Provides a paper-mode virtual hedge so funding carry can be evaluated with
reduced directional exposure before building a true multi-venue hedge stack.
"""

from dataclasses import dataclass
from typing import Optional, TYPE_CHECKING

from config import PAPER_MODE, RuntimeConfig

if TYPE_CHECKING:
    from engine.order_engine import OrderEngine
    from engine.position_manager import Position


@dataclass
class HedgeLeg:
    direction: str
    size: float
    entry_price: float
    entry_fee: float
    exit_price: Optional[float] = None
    exit_fee: float = 0.0
    raw_pnl: float = 0.0
    net_pnl: float = 0.0
    active: bool = True


class HedgeManager:
    """Creates and closes a virtual hedge leg in paper mode."""

    def __init__(self, cfg: RuntimeConfig, order_engine: "OrderEngine") -> None:
        self._cfg = cfg
        self._order_engine = order_engine

    async def open_hedge(self, symbol: str, direction: str, size: float) -> Optional[HedgeLeg]:
        if not self._cfg.hedge_enabled:
            return None
        if not PAPER_MODE:
            raise NotImplementedError("Live hedge execution is not implemented yet")

        hedge_direction = "SHORT" if direction == "LONG" else "LONG"
        mark = await self._order_engine.fetch_mark_price(symbol)
        if mark is None:
            return None

        if hedge_direction == "LONG":
            entry_price = mark * (1 + self._cfg.hedge_slippage_pct)
        else:
            entry_price = mark * (1 - self._cfg.hedge_slippage_pct)

        hedge_size = size * self._cfg.hedge_ratio
        entry_fee = hedge_size * entry_price * self._cfg.hedge_fee_rate
        return HedgeLeg(
            direction=hedge_direction,
            size=hedge_size,
            entry_price=entry_price,
            entry_fee=entry_fee,
        )

    async def close_hedge(self, symbol: str, hedge: Optional[HedgeLeg]) -> Optional[HedgeLeg]:
        if hedge is None or not hedge.active:
            return hedge

        mark = await self._order_engine.fetch_mark_price(symbol)
        if mark is None:
            mark = hedge.entry_price

        if hedge.direction == "LONG":
            exit_price = mark * (1 - self._cfg.hedge_slippage_pct)
            raw_pnl = (exit_price - hedge.entry_price) * hedge.size
        else:
            exit_price = mark * (1 + self._cfg.hedge_slippage_pct)
            raw_pnl = (hedge.entry_price - exit_price) * hedge.size

        exit_fee = hedge.size * exit_price * self._cfg.hedge_fee_rate
        hedge.exit_price = exit_price
        hedge.exit_fee = exit_fee
        hedge.raw_pnl = raw_pnl
        hedge.net_pnl = raw_pnl - hedge.entry_fee - exit_fee
        hedge.active = False
        return hedge
