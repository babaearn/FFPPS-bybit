"""
Database layer — asyncpg-backed persistence.
Graceful degradation: if DATABASE_URL is not set, all operations are no-ops.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from config import DATABASE_URL

logger = logging.getLogger(__name__)

# asyncpg is optional — guard import
try:
    import asyncpg
    _ASYNCPG_AVAILABLE = True
except ImportError:
    _ASYNCPG_AVAILABLE = False
    logger.warning("asyncpg not installed — database persistence disabled")


CREATE_TRADES_TABLE = """
CREATE TABLE IF NOT EXISTS trades (
    id              SERIAL PRIMARY KEY,
    trade_id        TEXT UNIQUE NOT NULL,
    symbol          TEXT NOT NULL,
    funding_time    TIMESTAMPTZ,
    detected_at     TIMESTAMPTZ,
    funding_rate    NUMERIC(10,6),
    interval_hours  NUMERIC(4,2),
    direction       TEXT,
    size            NUMERIC(20,8),
    entry_price     NUMERIC(20,8),
    mark_price_snapshot NUMERIC(20,8),
    open_interest_usd   NUMERIC(20,8),
    volume_24h_usd      NUMERIC(20,8),
    turnover_24h_usd    NUMERIC(20,8),
    expected_net_edge_usd NUMERIC(20,8),
    expected_net_edge_bps NUMERIC(20,8),
    tp_price        NUMERIC(20,8),
    exit_price      NUMERIC(20,8),
    close_type      TEXT,
    entry_fee       NUMERIC(20,8),
    exit_fee        NUMERIC(20,8),
    funding_pnl     NUMERIC(20,8),
    hedge_pnl       NUMERIC(20,8),
    hedge_entry_fee NUMERIC(20,8),
    hedge_exit_fee  NUMERIC(20,8),
    raw_pnl         NUMERIC(20,8),
    net_pnl         NUMERIC(20,8),
    net_pnl_pct     NUMERIC(10,6),
    entry_time      TIMESTAMPTZ,
    exit_time       TIMESTAMPTZ,
    duration_sec    INTEGER,
    timing_drift_ms INTEGER,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
"""

ADD_FUNDING_PNL_COLUMN = """
ALTER TABLE trades
ADD COLUMN IF NOT EXISTS funding_pnl NUMERIC(20,8);
"""

ADD_HEDGE_COLUMNS = """
ALTER TABLE trades
ADD COLUMN IF NOT EXISTS hedge_pnl NUMERIC(20,8),
ADD COLUMN IF NOT EXISTS hedge_entry_fee NUMERIC(20,8),
ADD COLUMN IF NOT EXISTS hedge_exit_fee NUMERIC(20,8);
"""

ADD_MARKET_CONTEXT_COLUMNS = """
ALTER TABLE trades
ADD COLUMN IF NOT EXISTS funding_time TIMESTAMPTZ,
ADD COLUMN IF NOT EXISTS detected_at TIMESTAMPTZ,
ADD COLUMN IF NOT EXISTS mark_price_snapshot NUMERIC(20,8),
ADD COLUMN IF NOT EXISTS open_interest_usd NUMERIC(20,8),
ADD COLUMN IF NOT EXISTS volume_24h_usd NUMERIC(20,8),
ADD COLUMN IF NOT EXISTS turnover_24h_usd NUMERIC(20,8),
ADD COLUMN IF NOT EXISTS expected_net_edge_usd NUMERIC(20,8),
ADD COLUMN IF NOT EXISTS expected_net_edge_bps NUMERIC(20,8);
"""

CREATE_DAILY_STATS_TABLE = """
CREATE TABLE IF NOT EXISTS daily_stats (
    date            DATE PRIMARY KEY,
    total_trades    INTEGER DEFAULT 0,
    wins            INTEGER DEFAULT 0,
    losses          INTEGER DEFAULT 0,
    win_rate        NUMERIC(5,2),
    total_net_pnl   NUMERIC(20,8),
    total_fees      NUMERIC(20,8),
    roi_pct         NUMERIC(10,4),
    best_trade_pnl  NUMERIC(20,8),
    worst_trade_pnl NUMERIC(20,8),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);
"""

UPSERT_TRADE = """
INSERT INTO trades (
    trade_id, symbol, funding_time, detected_at, funding_rate, interval_hours, direction,
    size, entry_price, mark_price_snapshot, open_interest_usd, volume_24h_usd,
    turnover_24h_usd, expected_net_edge_usd, expected_net_edge_bps, tp_price, exit_price, close_type,
    entry_fee, exit_fee, funding_pnl, hedge_pnl, hedge_entry_fee, hedge_exit_fee,
    raw_pnl, net_pnl, net_pnl_pct, entry_time, exit_time, duration_sec, timing_drift_ms
) VALUES (
    $1, $2, $3, $4, $5, $6, $7,
    $8, $9, $10, $11, $12,
    $13, $14, $15, $16, $17, $18,
    $19, $20, $21, $22, $23, $24,
    $25, $26, $27, $28, $29, $30, $31
)
ON CONFLICT (trade_id) DO UPDATE SET
    funding_time    = EXCLUDED.funding_time,
    detected_at     = EXCLUDED.detected_at,
    exit_price      = EXCLUDED.exit_price,
    close_type      = EXCLUDED.close_type,
    mark_price_snapshot = EXCLUDED.mark_price_snapshot,
    open_interest_usd   = EXCLUDED.open_interest_usd,
    volume_24h_usd      = EXCLUDED.volume_24h_usd,
    turnover_24h_usd    = EXCLUDED.turnover_24h_usd,
    expected_net_edge_usd = EXCLUDED.expected_net_edge_usd,
    expected_net_edge_bps = EXCLUDED.expected_net_edge_bps,
    exit_fee        = EXCLUDED.exit_fee,
    funding_pnl     = EXCLUDED.funding_pnl,
    hedge_pnl       = EXCLUDED.hedge_pnl,
    hedge_entry_fee = EXCLUDED.hedge_entry_fee,
    hedge_exit_fee  = EXCLUDED.hedge_exit_fee,
    raw_pnl         = EXCLUDED.raw_pnl,
    net_pnl         = EXCLUDED.net_pnl,
    net_pnl_pct     = EXCLUDED.net_pnl_pct,
    exit_time       = EXCLUDED.exit_time,
    duration_sec    = EXCLUDED.duration_sec;
"""

UPSERT_DAILY_STATS = """
INSERT INTO daily_stats (
    date, total_trades, wins, losses, win_rate,
    total_net_pnl, total_fees, roi_pct,
    best_trade_pnl, worst_trade_pnl, updated_at
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, NOW())
ON CONFLICT (date) DO UPDATE SET
    total_trades    = EXCLUDED.total_trades,
    wins            = EXCLUDED.wins,
    losses          = EXCLUDED.losses,
    win_rate        = EXCLUDED.win_rate,
    total_net_pnl   = EXCLUDED.total_net_pnl,
    total_fees      = EXCLUDED.total_fees,
    roi_pct         = EXCLUDED.roi_pct,
    best_trade_pnl  = EXCLUDED.best_trade_pnl,
    worst_trade_pnl = EXCLUDED.worst_trade_pnl,
    updated_at      = NOW();
"""


class Database:
    """
    Async PostgreSQL persistence via asyncpg.
    If DATABASE_URL is None or asyncpg unavailable — all methods are no-ops.
    """

    def __init__(self) -> None:
        self._pool: Optional["asyncpg.Pool"] = None
        self._enabled = bool(DATABASE_URL) and _ASYNCPG_AVAILABLE

    async def connect(self) -> None:
        if not self._enabled:
            logger.info("Database: disabled (no DATABASE_URL or asyncpg missing)")
            return
        try:
            self._pool = await asyncpg.create_pool(  # type: ignore[attr-defined]
                DATABASE_URL,
                min_size=1,
                max_size=5,
                command_timeout=30,
            )
            async with self._pool.acquire() as conn:
                await conn.execute(CREATE_TRADES_TABLE)
                await conn.execute(ADD_FUNDING_PNL_COLUMN)
                await conn.execute(ADD_HEDGE_COLUMNS)
                await conn.execute(ADD_MARKET_CONTEXT_COLUMNS)
                await conn.execute(CREATE_DAILY_STATS_TABLE)
            logger.info("Database: connected and schema ready")
        except Exception:
            logger.exception("Database: failed to connect — running without persistence")
            self._pool = None
            self._enabled = False

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            logger.info("Database: pool closed")

    async def save_trade(self, result: dict) -> None:
        if not self._enabled or not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    UPSERT_TRADE,
                    result["trade_id"],
                    result["symbol"],
                    result.get("funding_time"),
                    result.get("detected_at"),
                    result.get("funding_rate", 0.0),
                    result.get("interval_hours", 0.0),
                    result["direction"],
                    result["size"],
                    result["entry_price"],
                    result.get("mark_price_snapshot", 0.0),
                    result.get("open_interest_usd", 0.0),
                    result.get("volume_24h_usd", 0.0),
                    result.get("turnover_24h_usd", 0.0),
                    result.get("expected_net_edge_usd", 0.0),
                    result.get("expected_net_edge_bps", 0.0),
                    result.get("tp_price"),
                    result.get("exit_price"),
                    result["close_type"],
                    result.get("entry_fee", 0.0),
                    result.get("exit_fee", 0.0),
                    result.get("funding_pnl", 0.0),
                    result.get("hedge_pnl", 0.0),
                    result.get("hedge_entry_fee", 0.0),
                    result.get("hedge_exit_fee", 0.0),
                    result.get("raw_pnl", 0.0),
                    result.get("net_pnl", 0.0),
                    result.get("net_pnl_pct", 0.0),
                    result.get("entry_time"),
                    result.get("exit_time"),
                    result.get("duration_sec", 0),
                    result.get("timing_drift_ms", 0),
                )
            logger.debug("DB: trade saved %s", result["trade_id"])
        except Exception:
            logger.exception("DB: save_trade failed for %s", result.get("trade_id"))

    async def save_daily_stats(
        self,
        date: str,
        total_trades: int,
        wins: int,
        losses: int,
        win_rate: float,
        total_net_pnl: float,
        total_fees: float,
        roi_pct: float,
        best_trade_pnl: float,
        worst_trade_pnl: float,
    ) -> None:
        if not self._enabled or not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    UPSERT_DAILY_STATS,
                    date,
                    total_trades,
                    wins,
                    losses,
                    round(win_rate, 2),
                    total_net_pnl,
                    total_fees,
                    round(roi_pct, 4),
                    best_trade_pnl,
                    worst_trade_pnl,
                )
            logger.debug("DB: daily_stats saved for %s", date)
        except Exception:
            logger.exception("DB: save_daily_stats failed for %s", date)

    async def get_recent_trades(self, n: int = 10) -> list[dict]:
        if not self._enabled or not self._pool:
            return []
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT * FROM trades ORDER BY created_at DESC LIMIT $1", n
                )
                return [dict(r) for r in rows]
        except Exception:
            logger.exception("DB: get_recent_trades failed")
            return []

    @property
    def is_enabled(self) -> bool:
        return self._enabled and self._pool is not None
