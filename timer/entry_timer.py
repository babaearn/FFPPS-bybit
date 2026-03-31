"""
Entry timer — funding-carry lifecycle manager.
Enters shortly before settlement, holds through funding, then exits.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable, Awaitable

from scanner.funding_scanner import FundingOpportunity
from config import RuntimeConfig

if TYPE_CHECKING:
    from engine.order_engine import OrderEngine
    from engine.position_manager import PositionManager
    from risk.risk_manager import RiskManager
    from scanner.funding_scanner import FundingScanner

logger = logging.getLogger(__name__)

AlertCallback = Callable[[str], Awaitable[None]]


class EntryTimer:
    """
    Receives FundingOpportunity objects and manages precision countdown tasks.
    One asyncio.Task per opportunity — fully concurrent.
    """

    def __init__(
        self,
        cfg: RuntimeConfig,
        order_engine: "OrderEngine",
        position_manager: "PositionManager",
        risk_manager: "RiskManager",
        scanner: "FundingScanner",
        send_alert: AlertCallback,
    ) -> None:
        self._cfg = cfg
        self._order_engine = order_engine
        self._position_manager = position_manager
        self._risk_manager = risk_manager
        self._scanner = scanner
        self._send_alert = send_alert
        self._active_tasks: dict[str, asyncio.Task] = {}  # symbol → task

    def schedule(self, opp: FundingOpportunity) -> None:
        """
        Schedule a countdown task for this opportunity.
        Ignores duplicates (same symbol already scheduled for same funding time).
        """
        task_key = f"{opp.symbol}:{opp.next_funding_time.isoformat()}"
        if task_key in self._active_tasks and not self._active_tasks[task_key].done():
            logger.debug("EntryTimer: already scheduled %s", task_key)
            return

        task = asyncio.create_task(
            self._countdown(opp),
            name=f"timer_{opp.symbol}",
        )
        self._active_tasks[task_key] = task
        task.add_done_callback(lambda t: self._on_task_done(task_key, t))
        logger.info(
            "EntryTimer: scheduled %s %s | rate=%.4f%% | funding_in=%.1fs",
            opp.direction,
            opp.symbol,
            opp.funding_rate * 100,
            (opp.next_funding_time - datetime.now(timezone.utc)).total_seconds(),
        )

    def _on_task_done(self, key: str, task: asyncio.Task) -> None:
        self._active_tasks.pop(key, None)
        if task.cancelled():
            logger.debug("EntryTimer task cancelled: %s", key)
        elif task.exception():
            logger.error(
                "EntryTimer task raised unhandled exception for %s: %s",
                key, task.exception(),
            )

    # ── Countdown logic ───────────────────────────────────────────────────────

    async def _countdown(self, opp: FundingOpportunity) -> None:
        symbol = opp.symbol
        funding_time = opp.next_funding_time
        confirm_gate_sec = self._cfg.confirm_gate_sec
        entry_window_sec = self._cfg.entry_window_sec
        hard_exit_sec = self._cfg.hard_exit_sec
        settle_grace_sec = self._cfg.funding_settle_grace_sec

        # ── Step 1: sleep until T-95s ─────────────────────────────────────────
        now = datetime.now(timezone.utc)
        time_to_funding = (funding_time - now).total_seconds()

        sleep_until_gate = time_to_funding - confirm_gate_sec
        if sleep_until_gate > 0:
            logger.debug(
                "%s: sleeping %.1fs until T-%ds gate",
                symbol, sleep_until_gate, confirm_gate_sec,
            )
            await asyncio.sleep(sleep_until_gate)

        logger.info("%s: confirmation gate — re-verifying funding rate", symbol)
        live_rate = await self._scanner.fetch_live_rate(symbol)

        if live_rate is None or abs(live_rate) < self._cfg.funding_threshold:
            msg = (
                f"CONFIRMATION FAILED: {symbol} | "
                f"rate dropped to {(live_rate or 0)*100:.4f}% | event skipped"
            )
            logger.warning(msg)
            await self._send_alert(f"⚠️ {msg}")
            return  # abort entirely

        logger.info(
            "%s: confirmation passed — rate=%.4f%%",
            symbol, live_rate * 100,
        )

        updated_rate = live_rate
        opp.funding_rate = updated_rate
        opp.direction = "SHORT" if updated_rate > 0 else "LONG"
        opp.estimated_funding_pnl_usd = self._cfg.position_size_usd * abs(updated_rate)
        opp.estimated_total_cost_usd = (
            self._cfg.position_size_usd * self._cfg.taker_fee_rate * 2
            + self._cfg.estimated_hedge_cost_usd
        )
        opp.expected_net_edge_usd = opp.estimated_funding_pnl_usd - opp.estimated_total_cost_usd
        opp.expected_net_edge_bps = (
            opp.expected_net_edge_usd / self._cfg.position_size_usd * 10_000
            if self._cfg.position_size_usd
            else 0.0
        )

        gap = confirm_gate_sec - entry_window_sec  # 5s
        await asyncio.sleep(gap)

        # ── Step 4: FIRE ENTRY ────────────────────────────────────────────────
        # Race condition guard
        if self._position_manager.is_open(symbol):
            logger.warning("%s: position already open — skip entry (race guard)", symbol)
            return

        # Risk manager pre-trade gate
        ok, reason = await self._risk_manager.check_pre_entry(opp)
        if not ok:
            logger.warning("%s: risk gate blocked entry: %s", symbol, reason)
            await self._send_alert(f"BLOCKED: {symbol} — {reason}")
            return

        intended_entry_time = datetime.now(timezone.utc)
        logger.info("%s: entry window — opening funding receiver leg (%s)", symbol, opp.direction)

        try:
            position = await self._order_engine.open_position(opp)
        except Exception:
            logger.exception("%s: order_engine.open_position failed", symbol)
            await self._send_alert(
                f"CRITICAL: {symbol} entry failed — check logs immediately"
            )
            return

        actual_entry_time = datetime.now(timezone.utc)
        drift_ms = int(
            (actual_entry_time - intended_entry_time).total_seconds() * 1000
        )
        if drift_ms > 500:
            logger.warning(
                "%s: timing drift WARNING — %dms (>500ms threshold)",
                symbol, drift_ms,
            )
        else:
            logger.info("%s: entry fired | drift=%dms", symbol, drift_ms)

        # Store drift on position for DB
        position.timing_drift_ms = drift_ms

        # Notify Telegram
        funding_in_sec = (funding_time - actual_entry_time).total_seconds()
        funding_at = funding_time.strftime("%H:%M:%S") + " UTC"
        unwind_at = (
            funding_time.replace(microsecond=0) if funding_time.microsecond else funding_time
        ).strftime("%H:%M:%S") + f" UTC (+{hard_exit_sec}s)"
        await self._send_alert(
            f"CARRY ENTRY FIRED: {symbol} | receive via {opp.direction} | "
            f"entry={position.entry_price:.6f} | "
            f"size={position.size:.4f} | notional=${position.entry_price * position.size:.2f}\n"
            f"Funding={opp.funding_rate*100:+.4f}% | "
            f"expected_edge=${opp.expected_net_edge_usd:.2f} ({opp.expected_net_edge_bps:.1f} bps)\n"
            f"Funding time: {funding_at} | Planned unwind: {unwind_at}"
        )

        now2 = datetime.now(timezone.utc)
        until_settlement = (funding_time - now2).total_seconds()
        if until_settlement > 0:
            await asyncio.sleep(until_settlement)

        if settle_grace_sec > 0:
            await asyncio.sleep(settle_grace_sec)

        await self._position_manager.apply_funding_credit(
            symbol=symbol,
            funding_pnl=opp.estimated_funding_pnl_usd,
        )
        await self._send_alert(
            f"FUNDING CAPTURED: {symbol} | estimated_credit=${opp.estimated_funding_pnl_usd:.4f}"
        )

        now3 = datetime.now(timezone.utc)
        remaining = (funding_time - now3).total_seconds() + hard_exit_sec
        if remaining > 0:
            logger.debug("%s: sleeping %.1fs until post-funding exit", symbol, remaining)
            await asyncio.sleep(remaining)

        logger.info("%s: post-funding exit triggered", symbol)
        await self._position_manager.force_close(symbol, reason="FUNDING_CAPTURE_EXIT")
