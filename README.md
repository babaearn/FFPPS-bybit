# Funding Momentum Sniper

A production-grade cryptocurrency funding rate momentum bot for Bybit linear perpetuals.

---

## Strategy

### Core Thesis

In the final 90 seconds before a funding settlement, traders rush to open positions to collect the funding payment. This creates predictable short-term price momentum in the direction of the funding bias. The bot enters at T-90s, rides the momentum, and exits at T-5s (hard stop). **We are not holding to collect funding — we are trading the momentum caused by others trying to collect it.**

### Trade Rules

| Step | Time | Action |
|------|------|--------|
| 1 | Continuous | Scan all Bybit linear perpetuals every 30s |
| 2 | Detection | Filter: `abs(funding_rate) >= 0.5%` AND `OI >= $500k` |
| 3 | T-95s | Re-verify funding rate still meets threshold — skip if dropped |
| 4 | T-90s | Fire MARKET entry order |
| 5 | T-90s | Place LIMIT take-profit at +0.32% (reduceOnly) |
| 6 | Any | If TP fills before T-5s — trade closed immediately |
| 7 | T-5s | Hard exit: cancel TP, fire MARKET close — no exceptions |

**Direction:**
- `funding_rate >= +0.5%` → SHORT (shorts pay longs, longs rush in → upward momentum)
- `funding_rate <= -0.5%` → LONG  (longs pay shorts, shorts rush in → downward momentum)

### Position Sizing

- Fixed notional per trade: `$500` (configurable)
- Paper capital: `$1000` (for ROI% calculation)
- Max leverage: `10x` (configurable)
- Sizing is flat — not scaled by funding rate magnitude

---

## Architecture

```
funding-sniper-bot/
├── main.py                    # Entry point, component wiring
├── config.py                  # Static + mutable runtime config
├── scanner/
│   └── funding_scanner.py     # Bybit tickers poll, opportunity detection
├── timer/
│   └── entry_timer.py         # Precision countdown, T-95s gate, T-90s entry
├── engine/
│   ├── order_engine.py        # Paper/live order abstraction
│   ├── position_manager.py    # Position state machine
│   └── pnl_engine.py         # Per-trade PnL + aggregate stats
├── risk/
│   └── risk_manager.py        # Pre-trade gate checks
├── database/
│   └── db.py                  # asyncpg persistence (optional)
├── telegram/
│   └── bot.py                 # aiogram v3 commands + proactive alerts
├── Dockerfile
├── railway.toml
├── requirements.txt
└── .env.example
```

---

## Local Setup

### Prerequisites

- Python 3.11+
- A Bybit account (or testnet account for paper mode)
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- Your Telegram chat ID (send `/start` to [@userinfobot](https://t.me/userinfobot))

### Install

```bash
git clone <repo>
cd funding-sniper-bot

python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate

pip install -r requirements.txt
```

### Configure

```bash
cp .env.example .env
# Edit .env with your values
```

Minimum required fields:
```
TELEGRAM_BOT_TOKEN=<your bot token>
TELEGRAM_CHAT_ID=<your chat id>
PAPER_MODE=true
```

### Run

```bash
python main.py
```

The bot will log to stdout and send a startup message to your Telegram chat.

---

## Railway Deployment

### 1. Create Railway project

```bash
npm install -g @railway/cli
railway login
railway init
```

### 2. Set environment variables

In the Railway dashboard → your project → Variables, add all variables from `.env.example`.

Or via CLI:
```bash
railway variables set TELEGRAM_BOT_TOKEN=xxx
railway variables set TELEGRAM_CHAT_ID=xxx
railway variables set PAPER_MODE=true
# ... etc
```

### 3. Add PostgreSQL (optional)

In Railway dashboard: New → Database → PostgreSQL. The `DATABASE_URL` variable is injected automatically.

### 4. Deploy

```bash
railway up
```

Railway uses the `Dockerfile` as defined in `railway.toml`. Logs stream in real time from the dashboard.

---

## Telegram Commands

| Command | Description |
|---------|-------------|
| `/status` | Active positions with entry price, TP, time to hard exit. Bot state (ACTIVE / EMERGENCY_STOP). Config snapshot. |
| `/pnl` | Today's P&L: net PnL, trades, wins, losses, win rate, fees, best/worst trade. All-time totals and ROI%. |
| `/journal [n]` | Last `n` trades (default 10). Shows symbol, direction, close type, net PnL%, duration. |
| `/scan` | Trigger an immediate scan. Returns top qualifying symbols sorted by funding rate magnitude. |
| `/next` | All qualifying events in the next 60 minutes, sorted by time to funding. This is the live trade queue. |
| `/config` | Display all current configuration parameters. |
| `/set <param> <value>` | Update a config parameter live without restart (see below). |
| `/forceclose` | Immediately close ALL open positions at market. No confirmation. |
| `/emergencystop` | Block all new entries. Existing positions unaffected. |
| `/resume` | Re-enable entries after emergency stop. |

### `/set` Parameters

```
/set threshold 0.006        → Funding threshold (e.g. 0.006 = 0.6%)
/set position_size 300      → Position size in USD
/set leverage 5             → Max leverage
/set daily_loss 100         → Daily loss limit in USD
/set min_oi 1000000         → Minimum open interest in USD
```

### Proactive Alerts

The bot pushes the following alerts automatically:

| Alert | Trigger |
|-------|---------|
| `SCAN HIT` | New qualifying opportunity detected |
| `CONFIRMATION FAILED` | T-95s gate: funding rate dropped below threshold |
| `ENTRY FIRED` | T-90s market order placed with fill details |
| `TP HIT` | Take-profit limit order filled |
| `T-5s HARD EXIT` | Position force-closed at market |
| `DAILY LOSS LIMIT` | Daily loss cap reached, new entries blocked |

---

## Configuration Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `PAPER_MODE` | `true` | `true` = simulate, `false` = live trading |
| `PAPER_CAPITAL` | `1000` | Simulated account size (USD) |
| `POSITION_SIZE_USD` | `500` | Fixed notional per trade (USD) |
| `MAX_LEVERAGE` | `10` | Max leverage for exchange margin |
| `FUNDING_THRESHOLD` | `0.005` | Min abs funding rate (0.5%) |
| `ENTRY_WINDOW_SEC` | `90` | Enter at T-90s before settlement |
| `CONFIRM_GATE_SEC` | `95` | Re-verify at T-95s |
| `HARD_EXIT_SEC` | `5` | Force close at T-5s |
| `TP_PCT` | `0.0032` | Take-profit target (0.32%) |
| `SLIPPAGE_PCT` | `0.0003` | Paper mode slippage simulation (0.03%) |
| `MIN_OI_USD` | `500000` | Minimum open interest filter |
| `MAX_DAILY_LOSS_USD` | `50` | Daily loss cap |
| `SCAN_INTERVAL_SEC` | `30` | Scanner polling interval |
| `TAKER_FEE_RATE` | `0.00055` | Bybit linear taker fee |
| `MAKER_FEE_RATE` | `0.0002` | Bybit linear maker fee |
| `LOG_LEVEL` | `INFO` | Logging level |

---

## Risk Disclosure

**This software is provided for educational and research purposes. Cryptocurrency trading involves substantial risk of loss.**

- Past performance of any strategy does not guarantee future results.
- Funding rates can change rapidly — the bot may enter positions that immediately go against you.
- Market orders in volatile conditions may fill at significantly worse prices than expected.
- The T-5s hard exit is the only position risk control. There is no stop loss.
- Maximum exposure per symbol is approximately 85 seconds.
- Multiple concurrent positions can be open simultaneously.
- Run in `PAPER_MODE=true` and observe behavior thoroughly before enabling live trading.
- Start with very small position sizes when going live.
- Never risk capital you cannot afford to lose.

The authors accept no liability for trading losses arising from use of this software.
