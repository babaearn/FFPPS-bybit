"""
Paper replay tool for funding-carry research.

Reads historical trade rows from DATABASE_URL and compares:
- recorded historical strategy outcome
- synthetic unhedged carry outcome
- synthetic hedged carry outcome

This script does not place orders or use capital.
"""

import os
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

import psycopg
from psycopg.rows import dict_row


def env_decimal(name: str, default: str) -> Decimal:
    raw = os.getenv(name, default).strip()
    try:
        return Decimal(raw)
    except Exception:
        return Decimal(default)


@dataclass
class ReplayConfig:
    position_size_usd: Decimal
    taker_fee_rate: Decimal
    hedge_fee_rate: Decimal
    funding_threshold: Decimal
    min_expected_net_edge_usd: Decimal
    hedge_ratio: Decimal


def load_config() -> ReplayConfig:
    return ReplayConfig(
        position_size_usd=env_decimal("POSITION_SIZE_USD", "500"),
        taker_fee_rate=env_decimal("TAKER_FEE_RATE", "0.00055"),
        hedge_fee_rate=env_decimal("HEDGE_FEE_RATE", "0.00055"),
        funding_threshold=env_decimal("FUNDING_THRESHOLD", "0.02"),
        min_expected_net_edge_usd=env_decimal("MIN_EXPECTED_NET_EDGE_USD", "1.5"),
        hedge_ratio=env_decimal("HEDGE_RATIO", "1.0"),
    )


def fetch_rows(database_url: str) -> list[dict]:
    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                select symbol, direction, funding_rate, size, entry_price, exit_price,
                       created_at, funding_time, detected_at, open_interest_usd,
                       volume_24h_usd, expected_net_edge_usd
                from trades
                where entry_price is not null
                  and exit_price is not null
                  and size is not null
                  and funding_rate is not null
                order by created_at
                """
            )
            return list(cur.fetchall())


def compute_raw_pnl(direction: str, size: Decimal, entry_price: Decimal, exit_price: Decimal) -> Decimal:
    if direction == "LONG":
        return (exit_price - entry_price) * size
    return (entry_price - exit_price) * size


def summarize_rows(rows: list[dict], cfg: ReplayConfig) -> dict:
    scenarios = {
        "historical_recorded": {"trades": 0, "net": Decimal("0"), "wins": 0},
        "carry_unhedged_all": {"trades": 0, "net": Decimal("0"), "wins": 0},
        "carry_hedged_all": {"trades": 0, "net": Decimal("0"), "wins": 0},
        "carry_unhedged_filtered": {"trades": 0, "net": Decimal("0"), "wins": 0},
        "carry_hedged_filtered": {"trades": 0, "net": Decimal("0"), "wins": 0},
    }
    symbol_stats: dict[str, dict[str, Decimal | int]] = defaultdict(
        lambda: {"trades": 0, "hedged": Decimal("0"), "unhedged": Decimal("0")}
    )
    by_hour: dict[int, dict[str, Decimal | int]] = defaultdict(
        lambda: {"trades": 0, "avg_abs_funding_total": Decimal("0"), "spikes_2pct": 0, "spikes_3pct": 0}
    )

    for row in rows:
        size = Decimal(str(row["size"]))
        entry_price = Decimal(str(row["entry_price"]))
        exit_price = Decimal(str(row["exit_price"]))
        funding_rate = Decimal(str(row["funding_rate"]))
        direction = row["direction"]

        raw_pnl = compute_raw_pnl(direction, size, entry_price, exit_price)

        entry_notional = size * entry_price
        exit_notional = size * exit_price
        historical_net = raw_pnl - entry_notional * cfg.taker_fee_rate - exit_notional * cfg.taker_fee_rate

        funding_credit = cfg.position_size_usd * abs(funding_rate)
        carry_entry_fee = cfg.position_size_usd * cfg.taker_fee_rate
        carry_exit_fee = cfg.position_size_usd * cfg.taker_fee_rate

        unhedged_net = raw_pnl + funding_credit - carry_entry_fee - carry_exit_fee
        hedge_raw = -raw_pnl * cfg.hedge_ratio
        hedge_entry_fee = cfg.position_size_usd * cfg.hedge_ratio * cfg.hedge_fee_rate
        hedge_exit_fee = cfg.position_size_usd * cfg.hedge_ratio * cfg.hedge_fee_rate
        hedged_net = (
            raw_pnl + funding_credit + hedge_raw - carry_entry_fee - carry_exit_fee - hedge_entry_fee - hedge_exit_fee
        )

        for name, value in (
            ("historical_recorded", historical_net),
            ("carry_unhedged_all", unhedged_net),
            ("carry_hedged_all", hedged_net),
        ):
            scenarios[name]["trades"] += 1
            scenarios[name]["net"] += value
            scenarios[name]["wins"] += int(value > 0)

        expected_edge = funding_credit - (cfg.position_size_usd * cfg.taker_fee_rate * 2)
        if abs(funding_rate) >= cfg.funding_threshold and expected_edge >= cfg.min_expected_net_edge_usd:
            for name, value in (
                ("carry_unhedged_filtered", unhedged_net),
                ("carry_hedged_filtered", hedged_net),
            ):
                scenarios[name]["trades"] += 1
                scenarios[name]["net"] += value
                scenarios[name]["wins"] += int(value > 0)

            stats = symbol_stats[row["symbol"]]
            stats["trades"] += 1
            stats["unhedged"] += unhedged_net
            stats["hedged"] += hedged_net

        timing_source = row.get("funding_time") or row["created_at"]
        utc_hour = int(timing_source.hour)
        hour_stats = by_hour[utc_hour]
        hour_stats["trades"] += 1
        hour_stats["avg_abs_funding_total"] += abs(funding_rate)
        hour_stats["spikes_2pct"] += int(abs(funding_rate) >= Decimal("0.02"))
        hour_stats["spikes_3pct"] += int(abs(funding_rate) >= Decimal("0.03"))

    return {
        "scenarios": scenarios,
        "symbol_stats": symbol_stats,
        "by_hour": by_hour,
    }


def print_report(report: dict) -> None:
    print("SCENARIOS")
    for name, stats in report["scenarios"].items():
        trades = stats["trades"]
        avg = stats["net"] / trades if trades else Decimal("0")
        win_rate = Decimal(stats["wins"]) / trades * 100 if trades else Decimal("0")
        print(
            name,
            {
                "trades": trades,
                "net": round(stats["net"], 6),
                "avg_per_trade": round(avg, 6),
                "win_rate": round(win_rate, 2),
            },
        )

    ranked_symbols = sorted(
        report["symbol_stats"].items(),
        key=lambda item: item[1]["hedged"],
        reverse=True,
    )

    print("BEST_FILTERED_SYMBOLS")
    for symbol, stats in ranked_symbols[:10]:
        print(
            symbol,
            {
                "trades": stats["trades"],
                "unhedged": round(stats["unhedged"], 6),
                "hedged": round(stats["hedged"], 6),
            },
        )

    print("BY_HOUR_UTC")
    for hour in sorted(report["by_hour"]):
        stats = report["by_hour"][hour]
        trades = int(stats["trades"])
        avg_abs_funding = stats["avg_abs_funding_total"] / trades if trades else Decimal("0")
        print(
            hour,
            {
                "trades": trades,
                "avg_abs_funding": round(avg_abs_funding, 6),
                "spikes_2pct": stats["spikes_2pct"],
                "spikes_3pct": stats["spikes_3pct"],
            },
        )


def main() -> None:
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise SystemExit("DATABASE_URL is required")

    cfg = load_config()
    rows = fetch_rows(database_url)
    report = summarize_rows(rows, cfg)
    print_report(report)


if __name__ == "__main__":
    main()
