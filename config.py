"""
Central configuration — reads from environment variables with sensible defaults.
All mutable runtime config lives in RuntimeConfig (singleton).
"""

import os
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


def _env_float(key: str, default: float) -> float:
    val = os.getenv(key)
    if val is None:
        return default
    try:
        return float(val)
    except ValueError:
        logger.warning("Invalid float for %s=%r, using default %s", key, val, default)
        return default


def _env_int(key: str, default: int) -> int:
    val = os.getenv(key)
    if val is None:
        return default
    try:
        return int(val)
    except ValueError:
        logger.warning("Invalid int for %s=%r, using default %s", key, val, default)
        return default


def _env_bool(key: str, default: bool) -> bool:
    val = os.getenv(key)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes")


def _env_str(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


# ── Static config (read once at startup) ─────────────────────────────────────

# Telegram
TELEGRAM_BOT_TOKEN: str = _env_str("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID: str = _env_str("TELEGRAM_CHAT_ID")

# Bybit
BYBIT_API_KEY: str = _env_str("BYBIT_API_KEY")
BYBIT_API_SECRET: str = _env_str("BYBIT_API_SECRET")
BYBIT_TESTNET: bool = _env_bool("BYBIT_TESTNET", False)

# Database
DATABASE_URL: Optional[str] = os.getenv("DATABASE_URL") or None

# Mode
PAPER_MODE: bool = _env_bool("PAPER_MODE", True)

# Logging
LOG_LEVEL: str = _env_str("LOG_LEVEL", "INFO")

# Bybit base URLs
BYBIT_MAINNET_URL = "https://api.bybit.com"
BYBIT_TESTNET_URL = "https://api-testnet.bybit.com"


def get_bybit_base_url() -> str:
    return BYBIT_TESTNET_URL if BYBIT_TESTNET else BYBIT_MAINNET_URL


def mask_secret(value: str) -> str:
    """Return first 6 chars + '...' — safe for logging."""
    if not value:
        return "<not set>"
    return value[:6] + "..."


# ── Runtime config (mutable, holds live-updatable params) ────────────────────

@dataclass
class RuntimeConfig:
    """
    Single mutable config object — passed around by reference.
    All /set Telegram commands update this object directly.
    No global mutable state outside this class.
    """
    # Capital / sizing
    paper_capital: float = field(default_factory=lambda: _env_float("PAPER_CAPITAL", 1000.0))
    position_size_usd: float = field(default_factory=lambda: _env_float("POSITION_SIZE_USD", 500.0))
    max_leverage: int = field(default_factory=lambda: _env_int("MAX_LEVERAGE", 10))

    # Strategy timing
    entry_window_sec: int = field(default_factory=lambda: _env_int("ENTRY_WINDOW_SEC", 20))
    confirm_gate_sec: int = field(default_factory=lambda: _env_int("CONFIRM_GATE_SEC", 30))
    hard_exit_sec: int = field(default_factory=lambda: _env_int("HARD_EXIT_SEC", 15))
    funding_settle_grace_sec: int = field(default_factory=lambda: _env_int("FUNDING_SETTLE_GRACE_SEC", 10))

    # Strategy thresholds
    funding_threshold: float = field(default_factory=lambda: _env_float("FUNDING_THRESHOLD", 0.02))
    tp_pct: float = field(default_factory=lambda: _env_float("TP_PCT", 0.0032))
    slippage_pct: float = field(default_factory=lambda: _env_float("SLIPPAGE_PCT", 0.0003))
    min_expected_net_edge_usd: float = field(default_factory=lambda: _env_float("MIN_EXPECTED_NET_EDGE_USD", 1.5))
    estimated_hedge_cost_usd: float = field(default_factory=lambda: _env_float("ESTIMATED_HEDGE_COST_USD", 0.0))

    # Risk
    min_oi_usd: float = field(default_factory=lambda: _env_float("MIN_OI_USD", 500_000.0))
    max_daily_loss_usd: float = field(default_factory=lambda: _env_float("MAX_DAILY_LOSS_USD", 50.0))

    # Hedge
    hedge_enabled: bool = field(default_factory=lambda: _env_bool("HEDGE_ENABLED", True))
    hedge_ratio: float = field(default_factory=lambda: _env_float("HEDGE_RATIO", 1.0))
    hedge_fee_rate: float = field(default_factory=lambda: _env_float("HEDGE_FEE_RATE", 0.00055))
    hedge_slippage_pct: float = field(default_factory=lambda: _env_float("HEDGE_SLIPPAGE_PCT", 0.0003))

    # Scanner
    scan_interval_sec: int = field(default_factory=lambda: _env_int("SCAN_INTERVAL_SEC", 30))

    # Fees (Bybit linear)
    taker_fee_rate: float = field(default_factory=lambda: _env_float("TAKER_FEE_RATE", 0.00055))
    maker_fee_rate: float = field(default_factory=lambda: _env_float("MAKER_FEE_RATE", 0.0002))

    # Emergency stop state
    emergency_stop: bool = False

    def validate_and_set(self, param: str, value: str) -> tuple[bool, str]:
        """
        Validate and set a parameter by name.
        Returns (success, message).
        """
        param = param.lower().strip()
        try:
            if param == "threshold":
                new_val = float(value)
                if not (0.0001 <= new_val <= 0.1):
                    return False, "threshold must be between 0.0001 and 0.1"
                old = self.funding_threshold
                self.funding_threshold = new_val
                return True, f"threshold updated: {old} → {new_val}"

            elif param == "position_size":
                new_val = float(value)
                if not (1.0 <= new_val <= 100_000.0):
                    return False, "position_size must be between 1 and 100000"
                old = self.position_size_usd
                self.position_size_usd = new_val
                return True, f"position_size updated: {old} → {new_val}"

            elif param == "leverage":
                new_val = int(value)
                if not (1 <= new_val <= 100):
                    return False, "leverage must be between 1 and 100"
                old = self.max_leverage
                self.max_leverage = new_val
                return True, f"leverage updated: {old} → {new_val}"

            elif param == "daily_loss":
                new_val = float(value)
                if not (1.0 <= new_val <= 100_000.0):
                    return False, "daily_loss must be between 1 and 100000"
                old = self.max_daily_loss_usd
                self.max_daily_loss_usd = new_val
                return True, f"daily_loss updated: {old} → {new_val}"

            elif param == "min_oi":
                new_val = float(value)
                if not (0.0 <= new_val <= 1_000_000_000.0):
                    return False, "min_oi must be between 0 and 1000000000"
                old = self.min_oi_usd
                self.min_oi_usd = new_val
                return True, f"min_oi updated: {old} → {new_val}"

            elif param == "min_edge":
                new_val = float(value)
                if not (0.0 <= new_val <= 10_000.0):
                    return False, "min_edge must be between 0 and 10000"
                old = self.min_expected_net_edge_usd
                self.min_expected_net_edge_usd = new_val
                return True, f"min_edge updated: {old} → {new_val}"

            elif param == "hedge_cost":
                new_val = float(value)
                if not (0.0 <= new_val <= 10_000.0):
                    return False, "hedge_cost must be between 0 and 10000"
                old = self.estimated_hedge_cost_usd
                self.estimated_hedge_cost_usd = new_val
                return True, f"hedge_cost updated: {old} → {new_val}"

            elif param == "hedge_ratio":
                new_val = float(value)
                if not (0.0 <= new_val <= 2.0):
                    return False, "hedge_ratio must be between 0 and 2"
                old = self.hedge_ratio
                self.hedge_ratio = new_val
                return True, f"hedge_ratio updated: {old} → {new_val}"

            elif param == "hedge_enabled":
                normalized = value.strip().lower()
                if normalized not in ("1", "0", "true", "false", "yes", "no"):
                    return False, "hedge_enabled must be true/false"
                old = self.hedge_enabled
                self.hedge_enabled = normalized in ("1", "true", "yes")
                return True, f"hedge_enabled updated: {old} → {self.hedge_enabled}"

            else:
                supported = "threshold, position_size, leverage, daily_loss, min_oi, min_edge, hedge_cost, hedge_ratio, hedge_enabled"
                return False, f"Unknown param '{param}'. Supported: {supported}"

        except (ValueError, TypeError) as exc:
            return False, f"Invalid value '{value}': {exc}"

    def to_dict(self) -> dict:
        return {
            "paper_capital": self.paper_capital,
            "position_size_usd": self.position_size_usd,
            "max_leverage": self.max_leverage,
            "funding_threshold": self.funding_threshold,
            "tp_pct": self.tp_pct,
            "slippage_pct": self.slippage_pct,
            "min_oi_usd": self.min_oi_usd,
            "max_daily_loss_usd": self.max_daily_loss_usd,
            "hedge_enabled": self.hedge_enabled,
            "hedge_ratio": self.hedge_ratio,
            "hedge_fee_rate": self.hedge_fee_rate,
            "hedge_slippage_pct": self.hedge_slippage_pct,
            "scan_interval_sec": self.scan_interval_sec,
            "entry_window_sec": self.entry_window_sec,
            "confirm_gate_sec": self.confirm_gate_sec,
            "hard_exit_sec": self.hard_exit_sec,
            "funding_settle_grace_sec": self.funding_settle_grace_sec,
            "taker_fee_rate": self.taker_fee_rate,
            "maker_fee_rate": self.maker_fee_rate,
            "min_expected_net_edge_usd": self.min_expected_net_edge_usd,
            "estimated_hedge_cost_usd": self.estimated_hedge_cost_usd,
            "paper_mode": PAPER_MODE,
            "emergency_stop": self.emergency_stop,
        }
