"""
PnL engine — per-trade calculations and aggregate statistics.
Pure computation — no I/O, fully testable.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from engine.position_manager import Position

logger = logging.getLogger(__name__)


@dataclass
class TradeResult:
    trade_id: str
    symbol: str
    funding_time: datetime
    detected_at: datetime
    funding_rate: float
    interval_hours: float
    direction: str
    size: float
    entry_price: float
    tp_price: float
    exit_price: float
    close_type: str
    entry_fee: float
    exit_fee: float
    funding_pnl: float
    hedge_pnl: float
    hedge_entry_fee: float
    hedge_exit_fee: float
    raw_pnl: float
    net_pnl: float
    net_pnl_pct: float
    mark_price_snapshot: float
    open_interest_usd: float
    volume_24h_usd: float
    turnover_24h_usd: float
    expected_net_edge_usd: float
    expected_net_edge_bps: float
    entry_time: datetime
    exit_time: datetime
    duration_sec: int
    timing_drift_ms: int


@dataclass
class DailyStats:
    date: str                   # ISO date string YYYY-MM-DD
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    total_net_pnl: float = 0.0
    total_fees: float = 0.0
    best_trade_pnl: float = 0.0
    worst_trade_pnl: float = 0.0

    @property
    def win_rate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return self.wins / self.total_trades * 100

    @property
    def roi_pct(self) -> float:
        return self.total_net_pnl  # as USD absolute; caller computes vs capital


@dataclass
class AllTimeStats:
    total_trades: int = 0
    total_net_pnl: float = 0.0
    total_fees: float = 0.0
    wins: int = 0
    hold_durations: list[int] = field(default_factory=list)

    @property
    def win_rate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return self.wins / self.total_trades * 100

    @property
    def avg_hold_duration_sec(self) -> float:
        if not self.hold_durations:
            return 0.0
        return sum(self.hold_durations) / len(self.hold_durations)


class PnlEngine:
    """
    Calculates per-trade PnL and maintains aggregate stats.
    All state in-memory; DB persistence happens in PositionManager.
    """

    def __init__(self) -> None:
        self._all_time = AllTimeStats()
        self._today_stats: dict[str, DailyStats] = {}   # date_str → DailyStats
        self._trade_log: list[TradeResult] = []

    def calculate(self, position: "Position") -> dict:
        """
        Compute full PnL for a closed position.
        Returns a dict (also saved as TradeResult internally).
        """
        entry_price = position.entry_price
        exit_price = position.exit_price or entry_price
        size = position.size
        direction = position.direction

        entry_notional = size * entry_price
        exit_notional = size * exit_price

        if direction == "LONG":
            raw_pnl = (exit_price - entry_price) * size
        else:
            raw_pnl = (entry_price - exit_price) * size

        entry_fee = position.entry_fee
        exit_fee = position.exit_fee or 0.0
        funding_pnl = position.funding_pnl
        hedge_pnl = position.hedge_pnl
        hedge_entry_fee = position.hedge_entry_fee
        hedge_exit_fee = position.hedge_exit_fee
        net_pnl = raw_pnl + funding_pnl + hedge_pnl - entry_fee - exit_fee
        net_pnl_pct = net_pnl / entry_notional * 100 if entry_notional else 0.0

        exit_time = position.exit_time or datetime.now(timezone.utc)
        entry_time = position.entry_time
        duration_sec = int((exit_time - entry_time).total_seconds())

        result = TradeResult(
            trade_id=position.trade_id,
            symbol=position.symbol,
            funding_time=position.funding_time,
            detected_at=position.detected_at,
            funding_rate=position.funding_rate,
            interval_hours=position.interval_hours,
            direction=direction,
            size=size,
            entry_price=entry_price,
            tp_price=position.tp_price,
            exit_price=exit_price,
            close_type=position.close_type or "UNKNOWN",
            entry_fee=entry_fee,
            exit_fee=exit_fee,
            funding_pnl=funding_pnl,
            hedge_pnl=hedge_pnl,
            hedge_entry_fee=hedge_entry_fee,
            hedge_exit_fee=hedge_exit_fee,
            raw_pnl=raw_pnl,
            net_pnl=net_pnl,
            net_pnl_pct=net_pnl_pct,
            mark_price_snapshot=position.mark_price_snapshot,
            open_interest_usd=position.open_interest_usd,
            volume_24h_usd=position.volume_24h_usd,
            turnover_24h_usd=position.turnover_24h_usd,
            expected_net_edge_usd=position.expected_net_edge_usd,
            expected_net_edge_bps=position.expected_net_edge_bps,
            entry_time=entry_time,
            exit_time=exit_time,
            duration_sec=duration_sec,
            timing_drift_ms=position.timing_drift_ms,
        )

        self._update_stats(result)
        self._trade_log.append(result)

        logger.info(
            "PnL: %s %s | net_pnl=$%.4f (%.4f%%) | close=%s | dur=%ds",
            direction, position.symbol,
            net_pnl, net_pnl_pct,
            result.close_type, duration_sec,
        )

        return {
            "trade_id": result.trade_id,
            "symbol": result.symbol,
            "funding_time": result.funding_time,
            "detected_at": result.detected_at,
            "direction": result.direction,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "size": size,
            "raw_pnl": raw_pnl,
            "net_pnl": net_pnl,
            "net_pnl_pct": net_pnl_pct,
            "entry_fee": entry_fee,
            "exit_fee": exit_fee,
            "funding_pnl": funding_pnl,
            "hedge_pnl": hedge_pnl,
            "hedge_entry_fee": hedge_entry_fee,
            "hedge_exit_fee": hedge_exit_fee,
            "close_type": result.close_type,
            "duration_sec": duration_sec,
            "timing_drift_ms": result.timing_drift_ms,
            "funding_rate": result.funding_rate,
            "interval_hours": result.interval_hours,
            "mark_price_snapshot": result.mark_price_snapshot,
            "open_interest_usd": result.open_interest_usd,
            "volume_24h_usd": result.volume_24h_usd,
            "turnover_24h_usd": result.turnover_24h_usd,
            "expected_net_edge_usd": result.expected_net_edge_usd,
            "expected_net_edge_bps": result.expected_net_edge_bps,
            "tp_price": result.tp_price,
            "entry_time": result.entry_time,
            "exit_time": result.exit_time,
        }

    def _update_stats(self, result: TradeResult) -> None:
        date_str = result.entry_time.date().isoformat()
        if date_str not in self._today_stats:
            self._today_stats[date_str] = DailyStats(date=date_str)
        daily = self._today_stats[date_str]

        daily.total_trades += 1
        daily.total_net_pnl += result.net_pnl
        daily.total_fees += result.entry_fee + result.exit_fee + result.hedge_entry_fee + result.hedge_exit_fee

        if result.net_pnl > 0:
            daily.wins += 1
        else:
            daily.losses += 1

        if result.net_pnl > daily.best_trade_pnl:
            daily.best_trade_pnl = result.net_pnl
        if result.net_pnl < daily.worst_trade_pnl:
            daily.worst_trade_pnl = result.net_pnl

        self._all_time.total_trades += 1
        self._all_time.total_net_pnl += result.net_pnl
        self._all_time.total_fees += result.entry_fee + result.exit_fee + result.hedge_entry_fee + result.hedge_exit_fee
        if result.net_pnl > 0:
            self._all_time.wins += 1
        self._all_time.hold_durations.append(result.duration_sec)

    # ── Query methods ─────────────────────────────────────────────────────────

    def today_stats(self) -> DailyStats:
        date_str = datetime.now(timezone.utc).date().isoformat()
        return self._today_stats.get(date_str, DailyStats(date=date_str))

    def all_time_stats(self) -> AllTimeStats:
        return self._all_time

    def today_net_loss(self) -> float:
        """Total losses (negative PnL) for today — used by risk manager."""
        stats = self.today_stats()
        # Return total negative PnL as absolute value (losses only)
        today_date = datetime.now(timezone.utc).date().isoformat()
        loss_total = 0.0
        for result in self._trade_log:
            if result.entry_time.date().isoformat() == today_date and result.net_pnl < 0:
                loss_total += abs(result.net_pnl)
        return loss_total

    def recent_trades(self, n: int = 10) -> list[TradeResult]:
        return list(reversed(self._trade_log[-n:]))

    def best_trade_today(self) -> Optional[TradeResult]:
        today = datetime.now(timezone.utc).date().isoformat()
        today_trades = [
            r for r in self._trade_log
            if r.entry_time.date().isoformat() == today
        ]
        return max(today_trades, key=lambda r: r.net_pnl, default=None)

    def worst_trade_today(self) -> Optional[TradeResult]:
        today = datetime.now(timezone.utc).date().isoformat()
        today_trades = [
            r for r in self._trade_log
            if r.entry_time.date().isoformat() == today
        ]
        return min(today_trades, key=lambda r: r.net_pnl, default=None)
