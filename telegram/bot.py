"""
Telegram bot — aiogram v3.
All commands silently ignore unauthorized senders.
Proactive alerts are pushed via send_alert().
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command
from aiogram.types import Message

import config as cfg_module
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, RuntimeConfig, PAPER_MODE

if TYPE_CHECKING:
    from engine.position_manager import PositionManager
    from engine.pnl_engine import PnlEngine
    from engine.order_engine import OrderEngine
    from scanner.funding_scanner import FundingScanner
    from risk.risk_manager import RiskManager

logger = logging.getLogger(__name__)


def _authorized(message: Message) -> bool:
    """Return True if message is from the configured chat ID."""
    if not TELEGRAM_CHAT_ID:
        return False
    return str(message.chat.id) == str(TELEGRAM_CHAT_ID)


class TelegramBot:
    """
    Wraps aiogram Bot and Dispatcher.
    Exposes send_alert() for proactive notifications.
    Registers all command handlers.
    """

    def __init__(self, cfg: RuntimeConfig) -> None:
        self._cfg = cfg
        self._bot: Optional[Bot] = None
        self._dp: Optional[Dispatcher] = None
        self._router = Router()
        self._position_manager: Optional["PositionManager"] = None
        self._pnl_engine: Optional["PnlEngine"] = None
        self._order_engine: Optional["OrderEngine"] = None
        self._scanner: Optional["FundingScanner"] = None
        self._risk_manager: Optional["RiskManager"] = None
        self._enabled = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)

    def wire(
        self,
        position_manager: "PositionManager",
        pnl_engine: "PnlEngine",
        order_engine: "OrderEngine",
        scanner: "FundingScanner",
        risk_manager: "RiskManager",
    ) -> None:
        self._position_manager = position_manager
        self._pnl_engine = pnl_engine
        self._order_engine = order_engine
        self._scanner = scanner
        self._risk_manager = risk_manager

    async def start(self) -> None:
        if not self._enabled:
            logger.warning("Telegram: BOT_TOKEN or CHAT_ID not set — bot disabled")
            return
        self._bot = Bot(token=TELEGRAM_BOT_TOKEN)
        self._dp = Dispatcher()
        self._register_handlers()
        self._dp.include_router(self._router)
        logger.info("Telegram bot starting (polling)…")
        await self._dp.start_polling(self._bot, allowed_updates=["message"])

    async def stop(self) -> None:
        if self._dp:
            await self._dp.stop_polling()
        if self._bot:
            await self._bot.session.close()

    async def send_alert(self, text: str) -> None:
        """Push a proactive message to the configured chat."""
        if not self._enabled or not self._bot:
            logger.info("TG alert (not sent — bot disabled): %s", text[:120])
            return
        try:
            await self._bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=text,
                parse_mode=None,
            )
        except Exception:
            logger.exception("send_alert failed: %s", text[:120])

    # ── Command handlers ──────────────────────────────────────────────────────

    def _register_handlers(self) -> None:
        r = self._router

        @r.message(Command("status"))
        async def cmd_status(message: Message) -> None:
            if not _authorized(message):
                return
            await message.answer(self._build_status())

        @r.message(Command("pnl"))
        async def cmd_pnl(message: Message) -> None:
            if not _authorized(message):
                return
            await message.answer(self._build_pnl())

        @r.message(Command("journal"))
        async def cmd_journal(message: Message) -> None:
            if not _authorized(message):
                return
            parts = (message.text or "").split()
            n = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 10
            await message.answer(self._build_journal(n))

        @r.message(Command("scan"))
        async def cmd_scan(message: Message) -> None:
            if not _authorized(message):
                return
            await message.answer("Scanning…")
            opps = await self._scanner.scan_now()
            if not opps:
                await message.answer("No qualifying opportunities found.")
                return
            opps_sorted = sorted(opps, key=lambda o: o.expected_net_edge_usd, reverse=True)
            lines = ["LIVE SCAN — funding carry candidates:\n"]
            for opp in opps_sorted[:20]:
                ttf = (opp.next_funding_time - datetime.now(timezone.utc)).total_seconds()
                lines.append(
                    f"{opp.symbol} | receive via {opp.direction} | "
                    f"rate={opp.funding_rate*100:+.4f}% | "
                    f"edge=${opp.expected_net_edge_usd:.2f} | "
                    f"OI=${opp.open_interest_usd/1e6:.1f}M | "
                    f"T-{ttf:.0f}s"
                )
            await message.answer("\n".join(lines))

        @r.message(Command("next"))
        async def cmd_next(message: Message) -> None:
            if not _authorized(message):
                return
            await message.answer("Fetching upcoming events…")
            opps = await self._scanner.scan_now()
            now = datetime.now(timezone.utc)
            # Show events within next 60 minutes
            upcoming = [
                o for o in opps
                if 0 < (o.next_funding_time - now).total_seconds() <= 3600
            ]
            upcoming.sort(key=lambda o: o.next_funding_time)
            if not upcoming:
                await message.answer("No qualifying events in next 60 minutes.")
                return
            lines = ["UPCOMING FUNDING QUEUE (next 60min):\n"]
            for opp in upcoming[:20]:
                ttf = (opp.next_funding_time - now).total_seconds()
                lines.append(
                    f"{opp.symbol} | receive via {opp.direction} | "
                    f"rate={opp.funding_rate*100:+.4f}% | "
                    f"edge=${opp.expected_net_edge_usd:.2f} | "
                    f"T-{ttf:.0f}s | "
                    f"OI=${opp.open_interest_usd/1e6:.1f}M"
                )
            await message.answer("\n".join(lines))

        @r.message(Command("config"))
        async def cmd_config(message: Message) -> None:
            if not _authorized(message):
                return
            await message.answer(self._build_config())

        @r.message(Command("set"))
        async def cmd_set(message: Message) -> None:
            if not _authorized(message):
                return
            parts = (message.text or "").split(maxsplit=2)
            if len(parts) < 3:
                await message.answer(
                    "Usage: /set <param> <value>\n"
                    "Params: threshold, position_size, leverage, daily_loss, min_oi, min_edge, hedge_cost, hedge_ratio, hedge_enabled"
                )
                return
            param, value = parts[1], parts[2]
            success, msg = self._cfg.validate_and_set(param, value)
            prefix = "✅" if success else "❌"
            await message.answer(f"{prefix} {msg}")

        @r.message(Command("forceclose"))
        async def cmd_forceclose(message: Message) -> None:
            if not _authorized(message):
                return
            await message.answer("Force closing ALL open positions…")
            closed = await self._position_manager.close_all(reason="FORCE")
            if not closed:
                await message.answer("No open positions to close.")
            else:
                await message.answer(
                    f"Closed {len(closed)} position(s): "
                    + ", ".join(p.symbol for p in closed)
                )

        @r.message(Command("emergencystop"))
        async def cmd_emergency_stop(message: Message) -> None:
            if not _authorized(message):
                return
            self._cfg.emergency_stop = True
            await message.answer(
                "EMERGENCY STOP ACTIVE. No new entries will be placed.\n"
                "Use /resume to re-enable."
            )
            logger.warning("Emergency stop activated via Telegram")

        @r.message(Command("resume"))
        async def cmd_resume(message: Message) -> None:
            if not _authorized(message):
                return
            self._cfg.emergency_stop = False
            await message.answer("Bot resumed. New entries are now allowed.")
            logger.info("Emergency stop cleared via Telegram")

        @r.message(Command("start", "help"))
        async def cmd_help(message: Message) -> None:
            if not _authorized(message):
                return
            help_text = (
                "FUNDING CARRY FARMER — Commands\n\n"
                "/status       — Active funding captures + bot state\n"
                "/pnl          — PnL summary today + all time\n"
                "/journal [n]  — Last n trades (default 10)\n"
                "/scan         — Live scan now\n"
                "/next         — Upcoming trade queue (60min)\n"
                "/config       — Show all config params\n"
                "/set <p> <v>  — Update config param live\n"
                "/forceclose   — Close ALL open positions\n"
                "/emergencystop — Block all new entries\n"
                "/resume       — Re-enable after stop"
            )
            await message.answer(help_text)

    # ── Message builders ──────────────────────────────────────────────────────

    def _build_status(self) -> str:
        lines = ["BOT STATUS\n"]
        mode = "PAPER" if PAPER_MODE else "LIVE"
        state = "EMERGENCY_STOP" if self._cfg.emergency_stop else "ACTIVE"
        lines.append(f"Mode: {mode} | State: {state}\n")

        open_positions = self._position_manager.get_all_open()
        if not open_positions:
            lines.append("Positions: none\n")
        else:
            lines.append(f"Positions ({len(open_positions)} open):\n")
            for pos in open_positions:
                ttf = pos.seconds_to_planned_exit(self._cfg.hard_exit_sec)
                lines.append(
                    f"  {pos.symbol} | {pos.direction} | "
                    f"entry={pos.entry_price:.6f} | "
                    f"funding_credit=${pos.funding_pnl:.4f} | "
                    f"hedge={pos.hedge_direction or 'off'}:{pos.hedge_size:.4f} | "
                    f"planned_exit_in={ttf:.0f}s"
                )

        lines.append(
            f"\nConfig: threshold={self._cfg.funding_threshold:.4f} | "
            f"size=${self._cfg.position_size_usd:.0f} | "
            f"leverage={self._cfg.max_leverage}x | "
            f"min_edge=${self._cfg.min_expected_net_edge_usd:.2f}"
        )
        return "\n".join(lines)

    def _build_pnl(self) -> str:
        today = self._pnl_engine.today_stats()
        at = self._pnl_engine.all_time_stats()

        best = self._pnl_engine.best_trade_today()
        worst = self._pnl_engine.worst_trade_today()
        best_str = f"${best.net_pnl:.4f}" if best else "n/a"
        worst_str = f"${worst.net_pnl:.4f}" if worst else "n/a"

        losses_str = str(today.total_trades - today.wins)
        at_roi = at.total_net_pnl / self._cfg.paper_capital * 100

        return (
            f"PNL REPORT\n\n"
            f"TODAY:\n"
            f"  Net PnL:  ${today.total_net_pnl:.4f}\n"
            f"  Trades:   {today.total_trades}\n"
            f"  Wins:     {today.wins}\n"
            f"  Losses:   {losses_str}\n"
            f"  Win rate: {today.win_rate:.1f}%\n"
            f"  Fees:     ${today.total_fees:.4f}\n"
            f"  Best:     {best_str}\n"
            f"  Worst:    {worst_str}\n\n"
            f"ALL TIME:\n"
            f"  Net PnL:  ${at.total_net_pnl:.4f}\n"
            f"  Trades:   {at.total_trades}\n"
            f"  Win rate: {at.win_rate:.1f}%\n"
            f"  ROI:      {at_roi:.2f}%\n"
            f"  Avg hold: {at.avg_hold_duration_sec:.0f}s"
        )

    def _build_journal(self, n: int) -> str:
        trades = self._pnl_engine.recent_trades(n)
        if not trades:
            return "No trades recorded yet."
        lines = [f"LAST {min(n, len(trades))} TRADES:\n"]
        for t in trades:
            pnl_str = f"${t.net_pnl:+.4f} ({t.net_pnl_pct:+.4f}%)"
            lines.append(
                f"{t.symbol} | {t.direction} | {t.close_type} | "
                f"{pnl_str} | {t.duration_sec}s"
            )
        return "\n".join(lines)

    def _build_config(self) -> str:
        d = self._cfg.to_dict()
        lines = ["CURRENT CONFIG:\n"]
        for k, v in d.items():
            if isinstance(v, float):
                lines.append(f"  {k}: {v:.6g}")
            else:
                lines.append(f"  {k}: {v}")
        return "\n".join(lines)
