# Funding Carry Farmer

A Bybit linear perpetual funding-rate bot that targets the funding-receiving side, holds through settlement, and exits shortly after funding is credited.

---

## Strategy

### Core Thesis

This version is designed to behave more like a funding farmer than a pre-funding momentum sniper.

- If funding is positive, shorts receive funding, so the bot prefers `SHORT`.
- If funding is negative, longs receive funding, so the bot prefers `LONG`.
- The bot only trades when estimated funding income is expected to exceed entry/exit fees plus an optional hedge-cost estimate.

This version now includes a virtual paper hedge leg so you can estimate carry behavior with reduced directional exposure. It is suitable for paper trading and carry research, but it is **not yet a full institutional live delta-neutral hedge stack**.

### Trade Lifecycle

| Step | Time | Action |
|------|------|--------|
| 1 | Continuous | Scan Bybit linear perpetuals every 30s |
| 2 | Detection | Filter by `abs(funding_rate) >= threshold`, `OI >= min_oi`, and minimum expected net edge |
| 3 | T-30s | Re-check live funding rate |
| 4 | T-20s | Enter on the funding-receiving side |
| 5 | Funding time | Hold through settlement |
| 6 | Funding+10s | Apply paper funding credit |
| 7 | Funding+15s | Exit at market |

### Current Defaults

- Funding threshold: `2.0%`
- Position size: `$500`
- Entry fee model: taker
- Exit fee model: taker
- Minimum expected net edge: `$1.50`
- Estimated hedge cost: `$0.00`

---

## Architecture

```
FFPPS-bybit/
├── main.py
├── config.py
├── scanner/
│   └── funding_scanner.py
├── timer/
│   └── entry_timer.py
├── engine/
│   ├── order_engine.py
│   ├── position_manager.py
│   └── pnl_engine.py
├── risk/
│   └── risk_manager.py
├── database/
│   └── db.py
├── telegram/
│   └── bot.py
├── Dockerfile
├── railway.toml
├── requirements.txt
└── .env.example
```

---

## Local Setup

```bash
git clone <repo>
cd FFPPS-bybit

python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Then copy:

```bash
cp .env.example .env
```

Set at minimum:

```env
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
PAPER_MODE=true
```

Run:

```bash
python main.py
```

---

## Telegram Commands

| Command | Description |
|---------|-------------|
| `/status` | Active carry positions, planned exits, and config snapshot |
| `/pnl` | Today and all-time PnL |
| `/journal [n]` | Last `n` trades |
| `/scan` | Live funding-carry candidates |
| `/next` | Upcoming qualifying funding events |
| `/config` | Current runtime config |
| `/set <param> <value>` | Update runtime config live |
| `/forceclose` | Close all open positions |
| `/emergencystop` | Block new entries |
| `/resume` | Resume entries |

Supported `/set` params:

- `threshold`
- `position_size`
- `leverage`
- `daily_loss`
- `min_oi`
- `min_edge`
- `hedge_cost`
- `hedge_ratio`
- `hedge_enabled`

---

## Replay Research

Use [`replay_carry.py`](/Users/mudrex/Desktop/fundingrate-%20faming%20/FFPPS-bybit/replay_carry.py) to run a paper-only replay from `DATABASE_URL`.

Example:

```bash
DATABASE_URL=postgresql://... python replay_carry.py
```

This script does not place orders or use capital.

---

## Configuration Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `PAPER_MODE` | `true` | Simulated or live mode |
| `PAPER_CAPITAL` | `1000` | Paper capital used for ROI |
| `POSITION_SIZE_USD` | `500` | Per-trade notional |
| `MAX_LEVERAGE` | `10` | Max leverage |
| `FUNDING_THRESHOLD` | `0.02` | Min absolute funding rate |
| `CONFIRM_GATE_SEC` | `30` | Live funding re-check time |
| `ENTRY_WINDOW_SEC` | `20` | Entry before funding |
| `FUNDING_SETTLE_GRACE_SEC` | `10` | Delay before applying paper funding credit |
| `HARD_EXIT_SEC` | `15` | Exit after settlement |
| `SLIPPAGE_PCT` | `0.0003` | Paper slippage |
| `MIN_EXPECTED_NET_EDGE_USD` | `1.5` | Minimum estimated net carry edge |
| `ESTIMATED_HEDGE_COST_USD` | `0.0` | Extra per-trade hedge-cost estimate |
| `HEDGE_ENABLED` | `true` | Enable paper hedge leg |
| `HEDGE_RATIO` | `1.0` | Hedge size relative to funding leg |
| `HEDGE_FEE_RATE` | `0.00055` | Hedge fee assumption |
| `HEDGE_SLIPPAGE_PCT` | `0.0003` | Hedge slippage assumption |
| `MIN_OI_USD` | `500000` | Minimum open interest |
| `MAX_DAILY_LOSS_USD` | `50` | Daily loss cap |
| `SCAN_INTERVAL_SEC` | `30` | Scanner poll interval |
| `TAKER_FEE_RATE` | `0.00055` | Taker fee assumption |
| `MAKER_FEE_RATE` | `0.0002` | Reserved for future maker logic |
| `LOG_LEVEL` | `INFO` | Logging level |

---

## Notes

- Paper mode now includes both a funding-credit component and a virtual hedge leg in PnL.
- DB persistence now stores `entry_time`, `exit_time`, `funding_pnl`, and hedge metrics.
- A true institutional setup still needs live hedge execution, hedge reconciliation, and venue-aware basis routing.

---

## Risk Disclosure

This project is for research and educational use only.

- Funding can change before settlement.
- A one-leg carry trade still carries directional risk.
- Live carry farming without a hedge is not institutional-grade delta-neutral execution.
- Test thoroughly in paper mode before considering any live deployment.
