"""
Funding Momentum Sniper — main entry point.
Wires all components, starts async tasks, handles graceful shutdown.
"""

import asyncio
import logging
import signal
import sys
from datetime import datetime, timezone

import httpx

import config as cfg_module
from config import RuntimeConfig, LOG_LEVEL, PAPER_MODE, mask_secret, BYBIT_API_KEY, TELEGRAM_BOT_TOKEN

from scanner.funding_scanner import FundingScanner, FundingOpportunity
from timer.entry_timer import EntryTimer
from engine.pnl_engine import PnlEngine
from engine.position_manager import PositionManager
from engine.order_engine import OrderEngine
from risk.risk_manager import RiskManager
from database.db import Database
from telegram.bot import TelegramBot


def setup_logging() -> None:
    level = getattr(logging, LOG_LEVEL.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        stream=sys.stdout,
        force=True,
    )
    # Quiet down noisy libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("aiogram").setLevel(logging.WARNING)


logger = logging.getLogger(__name__)


async def main() -> None:
    setup_logging()

    logger.info("=" * 60)
    logger.info("FUNDING MOMENTUM SNIPER starting")
    logger.info("  Mode:    %s", "PAPER" if PAPER_MODE else "LIVE")
    logger.info("  API key: %s", mask_secret(BYBIT_API_KEY))
    logger.info("  TG bot:  %s", mask_secret(TELEGRAM_BOT_TOKEN))
    logger.info("=" * 60)

    # ── Build shared HTTP client ──────────────────────────────────────────────
    http_client = httpx.AsyncClient(
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        timeout=httpx.Timeout(10.0),
        headers={"User-Agent": "FundingSniper/1.0"},
    )

    # ── Construct components ──────────────────────────────────────────────────
    cfg = RuntimeConfig()

    pnl_engine = PnlEngine()
    position_manager = PositionManager()
    db = Database()
    tg_bot = TelegramBot(cfg)

    order_engine = OrderEngine(
        cfg=cfg,
        http_client=http_client,
        position_manager=position_manager,
    )

    risk_manager = RiskManager(
        cfg=cfg,
        position_manager=position_manager,
        pnl_engine=pnl_engine,
    )

    # Telegram alert callback
    async def send_alert(text: str) -> None:
        await tg_bot.send_alert(text)

    # Wire dependencies
    position_manager.wire(
        order_engine=order_engine,
        pnl_engine=pnl_engine,
        db=db,
        send_alert=send_alert,
    )

    scanner = FundingScanner(
        cfg=cfg,
        http_client=http_client,
        on_opportunity=_make_opportunity_handler(cfg, tg_bot, entry_timer_holder={}),
        get_active_symbols=position_manager.get_active_symbols,
    )

    entry_timer = EntryTimer(
        cfg=cfg,
        order_engine=order_engine,
        position_manager=position_manager,
        risk_manager=risk_manager,
        scanner=scanner,
        send_alert=send_alert,
    )

    # Now rebuild scanner with proper entry_timer reference
    async def on_opportunity(opp: FundingOpportunity) -> None:
        logger.info(
            "SCAN HIT: %s %s | rate=%.4f%% | OI=$%.0f | T-%.0fs",
            opp.direction, opp.symbol,
            opp.funding_rate * 100,
            opp.open_interest_usd,
            (opp.next_funding_time - datetime.now(timezone.utc)).total_seconds(),
        )
        await tg_bot.send_alert(
            f"SCAN HIT: {opp.symbol} | {opp.direction} | "
            f"rate={opp.funding_rate*100:+.4f}% | "
            f"OI=${opp.open_interest_usd/1e6:.1f}M | "
            f"T-{(opp.next_funding_time - datetime.now(timezone.utc)).total_seconds():.0f}s"
        )
        entry_timer.schedule(opp)

    scanner_final = FundingScanner(
        cfg=cfg,
        http_client=http_client,
        on_opportunity=on_opportunity,
        get_active_symbols=position_manager.get_active_symbols,
    )

    tg_bot.wire(
        position_manager=position_manager,
        pnl_engine=pnl_engine,
        order_engine=order_engine,
        scanner=scanner_final,
        risk_manager=risk_manager,
    )

    # ── Connect database ──────────────────────────────────────────────────────
    await db.connect()

    # ── Startup alert ─────────────────────────────────────────────────────────
    startup_msg = (
        f"Funding Sniper STARTED\n"
        f"Mode: {'PAPER' if PAPER_MODE else 'LIVE'} | "
        f"Threshold: {cfg.funding_threshold*100:.2f}% | "
        f"Size: ${cfg.position_size_usd:.0f} | "
        f"Leverage: {cfg.max_leverage}x"
    )

    # ── Start background tasks ────────────────────────────────────────────────
    tasks: list[asyncio.Task] = []

    async def safe_scanner() -> None:
        try:
            await scanner_final.run_forever()
        except asyncio.CancelledError:
            logger.info("Scanner task cancelled")
        except Exception:
            logger.exception("Scanner crashed unexpectedly")

    async def safe_telegram() -> None:
        try:
            await tg_bot.start()
        except asyncio.CancelledError:
            logger.info("Telegram task cancelled")
        except Exception:
            logger.exception("Telegram bot crashed")

    tasks.append(asyncio.create_task(safe_scanner(), name="scanner"))
    tasks.append(asyncio.create_task(safe_telegram(), name="telegram"))

    # Send startup message after a brief delay to let TG connect
    async def delayed_startup_alert() -> None:
        await asyncio.sleep(3)
        await send_alert(startup_msg)

    asyncio.create_task(delayed_startup_alert(), name="startup_alert")

    # ── Shutdown handler ──────────────────────────────────────────────────────
    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("Shutdown signal received")
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except (NotImplementedError, OSError):
            pass  # Windows

    logger.info("All components started. Running…")

    try:
        await shutdown_event.wait()
    except asyncio.CancelledError:
        pass

    # ── Graceful shutdown ─────────────────────────────────────────────────────
    logger.info("Shutting down…")

    scanner_final.stop()
    await tg_bot.stop()

    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    await db.close()
    await http_client.aclose()

    logger.info("Funding Sniper stopped cleanly.")


def _make_opportunity_handler(cfg, tg_bot, entry_timer_holder):
    """Placeholder — replaced by real closure in main(). Not used."""
    async def _noop(opp):
        pass
    return _noop


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
