"""
Order engine — unified abstraction over paper simulation and live Bybit v5 API.
Switching modes: set PAPER_MODE env var. Zero code change required.
"""

import asyncio
import hashlib
import hmac
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional
from urllib.parse import urlencode

import httpx

from config import RuntimeConfig, PAPER_MODE, get_bybit_base_url, mask_secret, BYBIT_API_KEY, BYBIT_API_SECRET
from engine.position_manager import Position, PositionState
from scanner.funding_scanner import FundingOpportunity

if TYPE_CHECKING:
    from engine.position_manager import PositionManager

logger = logging.getLogger(__name__)


class OrderEngine:
    """
    Single entry point for all order operations.
    Paper mode: simulates fills with slippage, monitors TP via polling.
    Live mode: calls Bybit v5 REST API with retry + exponential backoff.
    """

    def __init__(
        self,
        cfg: RuntimeConfig,
        http_client: httpx.AsyncClient,
        position_manager: "PositionManager",
    ) -> None:
        self._cfg = cfg
        self._http = http_client
        self._pm = position_manager
        self._paper_mode = PAPER_MODE
        logger.info(
            "OrderEngine init | mode=%s | api_key=%s",
            "PAPER" if self._paper_mode else "LIVE",
            mask_secret(BYBIT_API_KEY),
        )

    # ── Public API ────────────────────────────────────────────────────────────

    async def open_position(self, opp: FundingOpportunity) -> Position:
        """
        Execute entry order. Returns filled Position object.
        Raises on unrecoverable failure.
        """
        if self._paper_mode:
            return await self._paper_open(opp)
        else:
            return await self._live_open(opp)

    async def close_position(self, position: Position, close_type: str) -> Position:
        """
        Execute exit order. Updates and returns the Position.
        close_type: "T5_HARD_EXIT" | "FORCE" | "TIMEOUT"
        """
        if self._paper_mode:
            return await self._paper_close(position, close_type)
        else:
            return await self._live_close(position, close_type)

    async def fetch_mark_price(self, symbol: str) -> Optional[float]:
        """Fetch current mark price for a symbol."""
        try:
            base_url = get_bybit_base_url()
            url = f"{base_url}/v5/market/tickers"
            resp = await self._http.get(
                url, params={"category": "linear", "symbol": symbol}, timeout=5.0
            )
            resp.raise_for_status()
            data = resp.json()
            items = data.get("result", {}).get("list", [])
            if items:
                return float(items[0].get("markPrice", 0))
        except Exception:
            logger.exception("fetch_mark_price failed for %s", symbol)
        return None

    # ── Paper mode ────────────────────────────────────────────────────────────

    async def _paper_open(self, opp: FundingOpportunity) -> Position:
        slippage = self._cfg.slippage_pct
        # Fetch live mark price at entry time — opp.mark_price can be 30-90s stale
        live_mark = await self.fetch_mark_price(opp.symbol)
        mark = live_mark if live_mark else opp.mark_price

        if opp.direction == "LONG":
            entry_price = mark * (1 + slippage)
        else:
            entry_price = mark * (1 - slippage)

        size = self._cfg.position_size_usd / entry_price
        entry_notional = size * entry_price
        entry_fee = entry_notional * self._cfg.taker_fee_rate

        tp_pct = self._cfg.tp_pct
        if opp.direction == "LONG":
            tp_price = entry_price * (1 + tp_pct)
        else:
            tp_price = entry_price * (1 - tp_pct)

        trade_id = uuid.uuid4().hex

        position = Position(
            trade_id=trade_id,
            symbol=opp.symbol,
            direction=opp.direction,
            size=size,
            entry_price=entry_price,
            tp_price=tp_price,
            tp_order_id=f"paper_tp_{trade_id}",
            entry_time=datetime.now(timezone.utc),
            funding_time=opp.next_funding_time,
            funding_rate=opp.funding_rate,
            interval_hours=opp.interval_hours,
            entry_fee=entry_fee,
            state=PositionState.OPEN,
        )
        self._pm.add_position(position)

        logger.info(
            "PAPER OPEN: %s %s | entry=%.6f | size=%.4f | tp=%.6f | fee=$%.4f",
            opp.direction, opp.symbol,
            entry_price, size, tp_price, entry_fee,
        )

        # Spawn TP monitoring task
        asyncio.create_task(
            self._paper_monitor_tp(position),
            name=f"paper_tp_{opp.symbol}",
        )

        return position

    async def _paper_monitor_tp(self, position: Position) -> None:
        """Poll mark price every 1 second, trigger TP when crossed."""
        symbol = position.symbol
        tp_price = position.tp_price
        direction = position.direction

        logger.debug("Paper TP monitor started: %s tp=%.6f", symbol, tp_price)

        while True:
            await asyncio.sleep(1)

            if position.state != PositionState.OPEN:
                logger.debug("Paper TP monitor: %s no longer OPEN, stopping", symbol)
                return

            mark = await self.fetch_mark_price(symbol)
            if mark is None:
                continue

            tp_hit = (
                (direction == "LONG" and mark >= tp_price)
                or (direction == "SHORT" and mark <= tp_price)
            )

            if tp_hit:
                logger.info("Paper TP HIT: %s mark=%.6f tp=%.6f", symbol, mark, tp_price)
                exit_price = tp_price  # limit order fills at TP price
                exit_notional = position.size * exit_price
                exit_fee = exit_notional * self._cfg.maker_fee_rate

                await self._pm.record_tp_fill(
                    symbol=symbol,
                    exit_price=exit_price,
                    exit_fee=exit_fee,
                    close_type="TP",
                )
                return

    async def _paper_close(self, position: Position, close_type: str) -> Position:
        slippage = self._cfg.slippage_pct
        mark = await self.fetch_mark_price(position.symbol)
        if mark is None:
            mark = position.entry_price  # fallback

        if position.direction == "LONG":
            exit_price = mark * (1 - slippage)
        else:
            exit_price = mark * (1 + slippage)

        exit_notional = position.size * exit_price
        exit_fee = exit_notional * self._cfg.taker_fee_rate

        position.exit_price = exit_price
        position.exit_fee = exit_fee
        position.exit_time = datetime.now(timezone.utc)
        position.close_type = close_type
        position.state = PositionState.CLOSED

        logger.info(
            "PAPER CLOSE: %s %s | exit=%.6f | fee=$%.4f | type=%s",
            position.direction, position.symbol,
            exit_price, exit_fee, close_type,
        )
        return position

    # ── Live Bybit v5 mode ────────────────────────────────────────────────────

    async def _live_open(self, opp: FundingOpportunity) -> Position:
        symbol = opp.symbol
        direction = opp.direction
        base_url = get_bybit_base_url()

        # Set leverage first
        await self._bybit_request(
            "POST",
            "/v5/position/set-leverage",
            {
                "category": "linear",
                "symbol": symbol,
                "buyLeverage": str(self._cfg.max_leverage),
                "sellLeverage": str(self._cfg.max_leverage),
            },
        )

        # Calculate qty
        mark = opp.mark_price
        qty = self._cfg.position_size_usd / mark
        qty_str = self._format_qty(qty)

        side = "Buy" if direction == "LONG" else "Sell"
        order_params = {
            "category": "linear",
            "symbol": symbol,
            "side": side,
            "orderType": "Market",
            "qty": qty_str,
            "timeInForce": "IOC",
        }

        result = await self._bybit_request("POST", "/v5/order/create", order_params)
        order_id = result.get("orderId", "")

        # Poll for fill
        fill_price = await self._poll_fill(symbol, order_id)
        if fill_price is None:
            fill_price = mark  # best-effort fallback

        size = self._cfg.position_size_usd / fill_price
        entry_notional = size * fill_price
        entry_fee = entry_notional * self._cfg.taker_fee_rate

        tp_pct = self._cfg.tp_pct
        tp_price = fill_price * (1 + tp_pct) if direction == "LONG" else fill_price * (1 - tp_pct)

        # Place TP limit order
        tp_side = "Sell" if direction == "LONG" else "Buy"
        tp_result = await self._bybit_request(
            "POST",
            "/v5/order/create",
            {
                "category": "linear",
                "symbol": symbol,
                "side": tp_side,
                "orderType": "Limit",
                "qty": qty_str,
                "price": f"{tp_price:.6f}",
                "timeInForce": "PostOnly",
                "reduceOnly": True,
            },
        )
        tp_order_id = tp_result.get("orderId", "")

        trade_id = uuid.uuid4().hex
        position = Position(
            trade_id=trade_id,
            symbol=symbol,
            direction=direction,
            size=size,
            entry_price=fill_price,
            tp_price=tp_price,
            tp_order_id=tp_order_id,
            entry_time=datetime.now(timezone.utc),
            funding_time=opp.next_funding_time,
            funding_rate=opp.funding_rate,
            interval_hours=opp.interval_hours,
            entry_fee=entry_fee,
            state=PositionState.OPEN,
        )
        self._pm.add_position(position)

        logger.info(
            "LIVE OPEN: %s %s | fill=%.6f | size=%.4f | tp_order=%s",
            direction, symbol, fill_price, size, tp_order_id,
        )
        return position

    async def _live_close(self, position: Position, close_type: str) -> Position:
        symbol = position.symbol
        direction = position.direction

        # Step 1: cancel TP order
        if position.tp_order_id:
            try:
                await self._bybit_request(
                    "POST",
                    "/v5/order/cancel",
                    {
                        "category": "linear",
                        "symbol": symbol,
                        "orderId": position.tp_order_id,
                    },
                )
            except Exception as exc:
                # 404 means TP already filled — that's fine
                logger.info("%s: TP cancel result: %s (may be already filled)", symbol, exc)

        # Step 2: market close
        close_side = "Sell" if direction == "LONG" else "Buy"
        qty_str = self._format_qty(position.size)

        result = await self._bybit_request(
            "POST",
            "/v5/order/create",
            {
                "category": "linear",
                "symbol": symbol,
                "side": close_side,
                "orderType": "Market",
                "qty": qty_str,
                "timeInForce": "IOC",
                "reduceOnly": True,
            },
        )

        order_id = result.get("orderId", "")
        fill_price = await self._poll_fill(symbol, order_id)
        mark = await self.fetch_mark_price(symbol)
        exit_price = fill_price or mark or position.entry_price

        exit_notional = position.size * exit_price
        exit_fee = exit_notional * self._cfg.taker_fee_rate

        position.exit_price = exit_price
        position.exit_fee = exit_fee
        position.exit_time = datetime.now(timezone.utc)
        position.close_type = close_type
        position.state = PositionState.CLOSED

        logger.info(
            "LIVE CLOSE: %s %s | exit=%.6f | fee=$%.4f | type=%s",
            direction, symbol, exit_price, exit_fee, close_type,
        )
        return position

    # ── Bybit API helpers ─────────────────────────────────────────────────────

    async def _poll_fill(
        self, symbol: str, order_id: str, max_attempts: int = 3
    ) -> Optional[float]:
        """Poll order history to get fill price. Returns avg_price or None.
        Market IOC orders fill immediately and appear in /v5/order/history,
        NOT in /v5/order/realtime (which only shows open orders).
        """
        for attempt in range(max_attempts):
            await asyncio.sleep(0.5)
            try:
                result = await self._bybit_request(
                    "GET",
                    "/v5/order/history",
                    {"category": "linear", "symbol": symbol, "orderId": order_id},
                )
                items = result.get("list", [])
                for item in items:
                    if item.get("orderId") == order_id:
                        avg_price = item.get("avgPrice")
                        if avg_price:
                            return float(avg_price)
            except Exception:
                logger.exception("_poll_fill attempt %d for %s", attempt + 1, symbol)
        return None

    async def _bybit_request(
        self, method: str, endpoint: str, params: dict
    ) -> dict:
        """
        Authenticated Bybit v5 request with 3 retries and exponential backoff.
        Raises on final failure.
        """
        base_url = get_bybit_base_url()
        url = f"{base_url}{endpoint}"

        for attempt in range(3):
            try:
                headers = self._build_auth_headers(method, endpoint, params)
                if method == "GET":
                    resp = await self._http.get(url, params=params, headers=headers, timeout=10.0)
                else:
                    resp = await self._http.post(url, json=params, headers=headers, timeout=10.0)

                resp.raise_for_status()
                data = resp.json()
                ret_code = data.get("retCode", -1)
                if ret_code not in (0, 110025):  # 110025 = leverage unchanged
                    raise ValueError(
                        f"Bybit API retCode={ret_code}: {data.get('retMsg')} | endpoint={endpoint}"
                    )
                return data.get("result", {})

            except (httpx.HTTPError, ValueError) as exc:
                wait = 0.5 * (2 ** attempt)
                if attempt < 2:
                    logger.warning(
                        "Bybit %s %s attempt %d failed: %s — retry in %.1fs",
                        method, endpoint, attempt + 1, exc, wait,
                    )
                    await asyncio.sleep(wait)
                else:
                    logger.error(
                        "Bybit %s %s FAILED after 3 attempts: %s",
                        method, endpoint, exc,
                    )
                    raise

    def _build_auth_headers(self, method: str, endpoint: str, params: dict) -> dict:
        api_key = BYBIT_API_KEY
        api_secret = BYBIT_API_SECRET
        timestamp = str(int(time.time() * 1000))
        recv_window = "5000"

        if method == "GET":
            param_str = urlencode(sorted(params.items()))
        else:
            import json
            param_str = json.dumps(params, separators=(",", ":"))

        sign_str = timestamp + api_key + recv_window + param_str
        signature = hmac.new(
            api_secret.encode("utf-8"),
            sign_str.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()  # type: ignore[attr-defined]

        return {
            "X-BAPI-API-KEY": api_key,
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-SIGN": signature,
            "X-BAPI-RECV-WINDOW": recv_window,
            "Content-Type": "application/json",
        }

    @staticmethod
    def _format_qty(qty: float) -> str:
        """Format quantity — Bybit requires appropriate decimal precision."""
        if qty >= 1.0:
            return f"{qty:.3f}"
        elif qty >= 0.01:
            return f"{qty:.4f}"
        else:
            return f"{qty:.6f}"
