"""
Position manager — owns the in-memory position state machine.
Thread-safe via asyncio (single-threaded event loop).
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from engine.order_engine import OrderEngine
    from engine.pnl_engine import PnlEngine
    from database.db import Database

logger = logging.getLogger(__name__)


class PositionState(str, Enum):
    PENDING = "PENDING"
    OPEN = "OPEN"
    TP_ACTIVE = "TP_ACTIVE"   # TP order placed and monitoring
    CLOSED = "CLOSED"


@dataclass
class Position:
    trade_id: str
    symbol: str
    direction: str          # "LONG" | "SHORT"
    size: float
    entry_price: float
    tp_price: float
    tp_order_id: str
    entry_time: datetime    # UTC
    funding_time: datetime  # UTC — the settlement time
    funding_rate: float
    interval_hours: float
    entry_fee: float
    state: PositionState = PositionState.PENDING

    # Filled after close
    exit_price: Optional[float] = None
    exit_fee: Optional[float] = None
    exit_time: Optional[datetime] = None
    close_type: Optional[str] = None   # "TP" | "T5_HARD_EXIT" | "FORCE" | "TIMEOUT"
    timing_drift_ms: int = 0

    def duration_seconds(self) -> Optional[int]:
        if self.entry_time and self.exit_time:
            return int((self.exit_time - self.entry_time).total_seconds())
        return None

    def unrealized_pnl_pct(self, current_price: float) -> float:
        if self.direction == "LONG":
            return (current_price - self.entry_price) / self.entry_price * 100
        else:
            return (self.entry_price - current_price) / self.entry_price * 100

    def seconds_to_hard_exit(self, hard_exit_sec: int = 5) -> float:
        now = datetime.now(timezone.utc)
        return max(0.0, (self.funding_time - now).total_seconds() - hard_exit_sec)


AlertCallback = type(None)  # defined at runtime to avoid circular


class PositionManager:
    """
    Manages all open positions. Single source of truth for position state.
    Wires in order_engine and pnl_engine for close operations.
    """

    def __init__(self) -> None:
        self._positions: dict[str, Position] = {}   # symbol → Position
        self._closed: list[Position] = []           # audit trail
        self._order_engine: Optional["OrderEngine"] = None
        self._pnl_engine: Optional["PnlEngine"] = None
        self._db: Optional["Database"] = None
        self._send_alert: Optional[callable] = None
        self._lock = asyncio.Lock()

    def wire(
        self,
        order_engine: "OrderEngine",
        pnl_engine: "PnlEngine",
        db: "Database",
        send_alert: callable,
    ) -> None:
        """Inject dependencies after construction (breaks circular deps)."""
        self._order_engine = order_engine
        self._pnl_engine = pnl_engine
        self._db = db
        self._send_alert = send_alert

    # ── Position state access ─────────────────────────────────────────────────

    def add_position(self, position: Position) -> None:
        self._positions[position.symbol] = position
        logger.debug("PositionManager: added %s", position.symbol)

    def is_open(self, symbol: str) -> bool:
        pos = self._positions.get(symbol)
        return pos is not None and pos.state != PositionState.CLOSED

    def get_position(self, symbol: str) -> Optional[Position]:
        return self._positions.get(symbol)

    def get_all_open(self) -> list[Position]:
        return [
            p for p in self._positions.values()
            if p.state != PositionState.CLOSED
        ]

    def get_active_symbols(self) -> set[str]:
        return {
            sym for sym, pos in self._positions.items()
            if pos.state != PositionState.CLOSED
        }

    def get_closed_today(self) -> list[Position]:
        today = datetime.now(timezone.utc).date()
        return [
            p for p in self._closed
            if p.exit_time and p.exit_time.date() == today
        ]

    def get_all_closed(self) -> list[Position]:
        return list(self._closed)

    # ── Position lifecycle ────────────────────────────────────────────────────

    async def force_close(self, symbol: str, reason: str) -> None:
        """
        Force-close a position at market.
        If already CLOSED (TP filled), returns immediately — no double-close.
        """
        async with self._lock:
            pos = self._positions.get(symbol)
            if pos is None:
                logger.debug("force_close: no position found for %s", symbol)
                return
            if pos.state == PositionState.CLOSED:
                logger.debug("force_close: %s already CLOSED (likely TP)", symbol)
                return

            logger.info("force_close: closing %s reason=%s", symbol, reason)

        # Close outside lock to avoid blocking event loop
        try:
            closed_pos = await self._order_engine.close_position(pos, reason)
        except Exception:
            logger.exception("CRITICAL: force_close(%s) order failed", symbol)
            if self._send_alert:
                await self._send_alert(
                    f"CRITICAL: Failed to close {symbol} — manual intervention required!"
                )
            return

        async with self._lock:
            # Mark closed and move to audit trail
            closed_pos.state = PositionState.CLOSED
            self._positions.pop(symbol, None)
            self._closed.append(closed_pos)

        await self._finalize_position(closed_pos)

    async def record_tp_fill(
        self,
        symbol: str,
        exit_price: float,
        exit_fee: float,
        close_type: str = "TP",
    ) -> None:
        """Called by order engine when paper TP monitoring detects a fill."""
        async with self._lock:
            pos = self._positions.get(symbol)
            if pos is None or pos.state == PositionState.CLOSED:
                return

            pos.exit_price = exit_price
            pos.exit_fee = exit_fee
            pos.exit_time = datetime.now(timezone.utc)
            pos.close_type = close_type
            pos.state = PositionState.CLOSED
            self._positions.pop(symbol, None)
            self._closed.append(pos)

        await self._finalize_position(pos)

    async def close_all(self, reason: str = "FORCE") -> list[Position]:
        """Close all open positions (used by /forceclose Telegram command)."""
        symbols = list(self.get_active_symbols())
        results = []
        for sym in symbols:
            await self.force_close(sym, reason)
            pos = next((p for p in self._closed if p.symbol == sym), None)
            if pos:
                results.append(pos)
        return results

    # ── Post-close processing ─────────────────────────────────────────────────

    async def _finalize_position(self, pos: Position) -> None:
        """Calculate PnL, save to DB, send Telegram alert."""
        try:
            trade_result = self._pnl_engine.calculate(pos)
        except Exception:
            logger.exception("pnl_engine.calculate failed for %s", pos.symbol)
            return

        # Persist to DB
        if self._db:
            try:
                await self._db.save_trade(trade_result)
            except Exception:
                logger.exception("db.save_trade failed for %s", pos.symbol)

        # Send Telegram alert
        if self._send_alert:
            try:
                msg = self._format_close_alert(pos, trade_result)
                await self._send_alert(msg)
            except Exception:
                logger.exception("send_alert failed for %s close", pos.symbol)

    def _format_close_alert(self, pos: Position, result: dict) -> str:
        net_pnl = result.get("net_pnl", 0.0)
        net_pnl_pct = result.get("net_pnl_pct", 0.0)
        duration = result.get("duration_sec", 0)
        fees = result.get("entry_fee", 0.0) + result.get("exit_fee", 0.0)

        if pos.close_type == "TP":
            emoji = "TP HIT"
            body = (
                f"{pos.symbol} | {pos.direction} | "
                f"entry={pos.entry_price:.6f} exit={pos.exit_price:.6f}\n"
                f"net_pnl=${net_pnl:.4f} ({net_pnl_pct:+.4f}%) | "
                f"duration={duration}s | fees=${fees:.4f}"
            )
        else:
            win_loss = "WIN" if net_pnl > 0 else "LOSS"
            emoji = "T-5s HARD EXIT"
            body = (
                f"{pos.symbol} | {pos.direction} | {win_loss}\n"
                f"entry={pos.entry_price:.6f} exit={pos.exit_price:.6f}\n"
                f"net_pnl=${net_pnl:.4f} ({net_pnl_pct:+.4f}%)"
            )

        return f"{emoji}: {body}"
