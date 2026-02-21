"""
Funding rate scanner — single API call to Bybit tickers endpoint,
filters for high-funding opportunities, emits FundingOpportunity objects.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Awaitable, Optional

import httpx

from config import RuntimeConfig, get_bybit_base_url

logger = logging.getLogger(__name__)

TICKERS_ENDPOINT = "/v5/market/tickers"


@dataclass
class FundingOpportunity:
    symbol: str
    funding_rate: float
    next_funding_time: datetime           # UTC
    mark_price: float
    open_interest_usd: float
    direction: str                        # "LONG" | "SHORT"
    detected_at: datetime                 # UTC
    interval_hours: float                 # derived from nextFundingTime gap


OpportunityCallback = Callable[[FundingOpportunity], Awaitable[None]]


class FundingScanner:
    """
    Polls Bybit linear perpetual tickers every SCAN_INTERVAL_SEC seconds.
    For each qualifying symbol emits a FundingOpportunity via callback.
    """

    def __init__(
        self,
        cfg: RuntimeConfig,
        http_client: httpx.AsyncClient,
        on_opportunity: OpportunityCallback,
        get_active_symbols: Callable[[], set[str]],
    ) -> None:
        self._cfg = cfg
        self._http = http_client
        self._on_opportunity = on_opportunity
        self._get_active_symbols = get_active_symbols
        self._running = False
        self._known_events: dict[str, datetime] = {}   # symbol → next_funding_time already scheduled

    # ── Public API ────────────────────────────────────────────────────────────

    async def run_forever(self) -> None:
        """Main scan loop — runs until cancelled."""
        self._running = True
        logger.info("FundingScanner started (interval=%ds)", self._cfg.scan_interval_sec)
        while self._running:
            try:
                await self._scan_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("FundingScanner: unhandled error in scan loop")
            await asyncio.sleep(self._cfg.scan_interval_sec)

    async def scan_now(self) -> list[FundingOpportunity]:
        """
        Perform an immediate scan and return all qualifying opportunities
        (used by /scan and /next Telegram commands — does NOT trigger callbacks).
        """
        try:
            raw = await self._fetch_tickers()
        except Exception:
            logger.exception("scan_now: failed to fetch tickers")
            return []
        return self._parse_opportunities(raw, emit=False)

    async def fetch_live_rate(self, symbol: str) -> Optional[float]:
        """
        Re-fetch the current funding rate for a single symbol.
        Used by EntryTimer at T-95s confirmation gate.
        """
        try:
            raw = await self._fetch_tickers(symbol=symbol)
        except Exception:
            logger.exception("fetch_live_rate: failed for %s", symbol)
            return None
        items = raw.get("result", {}).get("list", [])
        for item in items:
            if item.get("symbol") == symbol:
                try:
                    return float(item["fundingRate"])
                except (KeyError, ValueError):
                    return None
        return None

    def stop(self) -> None:
        self._running = False

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _scan_once(self) -> None:
        raw = await self._fetch_tickers()
        opps = self._parse_opportunities(raw, emit=True)
        active = self._get_active_symbols()
        logger.info(
            "Scan complete: %d symbols, %d qualifying, %d active positions",
            len(raw.get("result", {}).get("list", [])),
            len(opps),
            len(active),
        )

    async def _fetch_tickers(self, symbol: Optional[str] = None) -> dict:
        """Single call to Bybit tickers endpoint."""
        params: dict = {"category": "linear"}
        if symbol:
            params["symbol"] = symbol

        base_url = get_bybit_base_url()
        url = f"{base_url}{TICKERS_ENDPOINT}"

        for attempt in range(3):
            try:
                resp = await self._http.get(url, params=params, timeout=10.0)
                resp.raise_for_status()
                data = resp.json()
                ret_code = data.get("retCode", -1)
                if ret_code != 0:
                    raise ValueError(
                        f"Bybit API error retCode={ret_code}: {data.get('retMsg')}"
                    )
                return data
            except (httpx.HTTPError, ValueError) as exc:
                wait = 0.5 * (2 ** attempt)
                if attempt < 2:
                    logger.warning(
                        "Tickers fetch attempt %d failed: %s — retrying in %.1fs",
                        attempt + 1, exc, wait,
                    )
                    await asyncio.sleep(wait)
                else:
                    logger.error("Tickers fetch failed after 3 attempts: %s", exc)
                    raise

    def _parse_opportunities(
        self, raw: dict, emit: bool
    ) -> list[FundingOpportunity]:
        now = datetime.now(timezone.utc)
        active_symbols = self._get_active_symbols()
        items: list[dict] = raw.get("result", {}).get("list", [])
        found: list[FundingOpportunity] = []

        for item in items:
            opp = self._evaluate_item(item, now, active_symbols)
            if opp is None:
                continue
            found.append(opp)
            if emit:
                event_key = f"{opp.symbol}:{opp.next_funding_time.isoformat()}"
                if event_key in self._known_events:
                    continue  # already scheduled
                self._known_events[event_key] = opp.next_funding_time
                # Schedule but don't await — fire and forget into event loop
                asyncio.create_task(
                    self._emit(opp),
                    name=f"scan_emit_{opp.symbol}",
                )

        # Prune stale known_events
        cutoff = now
        self._known_events = {
            k: v for k, v in self._known_events.items() if v > cutoff
        }

        return found

    def _evaluate_item(
        self,
        item: dict,
        now: datetime,
        active_symbols: set[str],
    ) -> Optional[FundingOpportunity]:
        try:
            symbol: str = item["symbol"]
            funding_rate = float(item.get("fundingRate") or 0)
            next_funding_ms = int(item.get("nextFundingTime") or 0)
            mark_price = float(item.get("markPrice") or 0)
            oi_value = float(item.get("openInterestValue") or 0)
        except (KeyError, ValueError, TypeError):
            return None

        if next_funding_ms == 0 or mark_price == 0:
            return None

        next_funding_time = datetime.fromtimestamp(
            next_funding_ms / 1000, tz=timezone.utc
        )
        time_to_funding = (next_funding_time - now).total_seconds()

        # Filter: between 91s and 3600s away
        if not (91 < time_to_funding <= 3600):
            return None

        # Filter: minimum absolute funding rate
        if abs(funding_rate) < self._cfg.funding_threshold:
            return None

        # Filter: minimum open interest
        if oi_value < self._cfg.min_oi_usd:
            return None

        # Filter: not already in active position
        if symbol in active_symbols:
            return None

        direction = "SHORT" if funding_rate >= self._cfg.funding_threshold else "LONG"

        # Derive approximate interval in hours from time_to_funding
        # Common Bybit intervals: 1h, 2h, 4h, 8h
        interval_hours = self._infer_interval(time_to_funding)

        return FundingOpportunity(
            symbol=symbol,
            funding_rate=funding_rate,
            next_funding_time=next_funding_time,
            mark_price=mark_price,
            open_interest_usd=oi_value,
            direction=direction,
            detected_at=now,
            interval_hours=interval_hours,
        )

    @staticmethod
    def _infer_interval(time_to_funding_sec: float) -> float:
        hours = time_to_funding_sec / 3600
        for candidate in (1.0, 2.0, 4.0, 8.0):
            if hours <= candidate + 0.1:
                return candidate
        return round(hours, 2)

    async def _emit(self, opp: FundingOpportunity) -> None:
        try:
            await self._on_opportunity(opp)
        except Exception:
            logger.exception("Error in opportunity callback for %s", opp.symbol)
