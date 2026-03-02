# Polymarket Crypto Scalping Radar

Real-time scalping radar for Polymarket crypto Up/Down markets (BTC, ETH, SOL, XRP — 5m/15m windows), powered by Binance price data via WebSocket and a 6-component signal engine with market regime detection.

## Features

- **Real-time data** — Binance WebSocket for sub-second price updates (HTTP fallback)
- **6-component signal engine** — RSI, MACD, VWAP, Bollinger Bands, divergence, S/R levels
- **Mean reversion alerts** — Beeps when RSI extreme + Bollinger touch + MID phase align (~65% win rate)
- **Auto-trading** — Fully automated execution with 11 configurable safety guards and a circuit breaker
- **Background TP/SL** — Non-blocking take-profit/stop-loss monitor; main loop never pauses
- **Auto-close** — Automatically closes positions before the market window expires
- **Position monitor** — TP/SL alerts with audio beeps for open positions
- **Market regime detection** — Classifies market as TREND_UP, TREND_DOWN, RANGE, or CHOP via ADX
- **Phase-aware trading** — Adjusts signal thresholds based on time remaining (EARLY/MID/LATE/CLOSING)
- **Split-screen terminal UI** — Static panel (top) with live stats + scrolling log (bottom)
- **Cross-platform** — Runs on Linux, macOS, and Windows 10+
- **Manual hotkey trading** — Press U/D/C/S/Q to buy UP, buy DOWN, close all, accept a signal, or exit
- **Multi-market support** — BTC, ETH, SOL, XRP with 5-minute or 15-minute windows
- **Position sync** — Auto-detects positions opened/closed directly on the Polymarket website
- **Session stats** — Win rate, P&L, profit factor, and max drawdown
- **Fully configurable** — 43 parameters via `.env`

## Project Structure

```
polymarket/
├── radar_poly.py                Main entry point (TradingSession, event loop)
├── src/                         Library modules
│   ├── signal_engine.py         Signal computation (6 indicators, regime, scenarios)
│   ├── auto_trader.py           Auto-trading engine (guards, circuit breaker, background TP/SL)
│   ├── ui_panel.py              Terminal UI (static panel, scrolling log)
│   ├── trade_executor.py        Trade execution (buy, sell, close, TP/SL)
│   ├── input_handler.py         Cross-platform keyboard input
│   ├── session_stats.py         Session statistics and summary
│   ├── colors.py                Shared ANSI color constants
│   ├── binance_api.py           Binance API + indicators (RSI, MACD, VWAP, BB, ADX)
│   ├── ws_binance.py            Binance WebSocket client (auto-reconnect, HTTP fallback)
│   ├── polymarket_api.py        Polymarket CLOB API (auth, orders, positions)
│   ├── market_config.py         Market configuration (asset, window, derived values)
│   └── logger.py                CSV logging (signals, trades, sessions)
├── docs/                        Documentation
│   ├── index.md                 Documentation hub
│   ├── TRADING_GUIDE.md         How to trade with the radar
│   ├── configuration.md         All 43 .env parameters
│   ├── development-guide.md     Technical reference for developers
│   └── backlog.md               Feature backlog and roadmap
├── sync_github.sh               Auto-commit and push script (see GitHub Sync section)
├── com.polymarket.sync.plist    macOS launchd agent for scheduled sync
├── .env.example                 Config template (copy to .env)
├── requirements.txt             Python dependencies
├── setup.sh / setup.bat         Setup scripts (Linux/macOS / Windows)
└── logs/                        Auto-generated CSV logs
```

## Quick Start

### 1. Setup

```bash
# Linux/macOS
chmod +x setup.sh && ./setup.sh

# Windows
setup.bat
```

This creates a virtual environment, installs dependencies, and copies `.env.example` to `.env`.

### 2. Configure

Export your private key from **https://polymarket.com/settings?tab=export-private-key**, then edit `.env`:

```
POLYMARKET_API_KEY=0xYOUR_PRIVATE_KEY_HERE
```

> **Security:** Never share your private key. After pasting in `.env`, clear your clipboard.

See [docs/configuration.md](docs/configuration.md) for all 29 parameters.

### 3. Run

```bash
source venv/bin/activate        # Linux/macOS (required every session)
# venv\Scripts\activate.bat     # Windows

python radar_poly.py            # Default $4 trades (from .env)
python radar_poly.py 10         # $10 per trade
```

## Hotkeys

| Key | Action |
|-----|--------|
| `U` | Buy UP (market order) |
| `D` | Buy DOWN (market order) |
| `S` | Accept suggested signal trade |
| `C` | Emergency close all positions |
| `Q` | Exit (prints session summary) |

## Screen Layout

```
 ═══════════════════════════════════════════════════════════════════════════════════════════════
 RADAR POLYMARKET │ 14:32:15 │ Balance: $52.30 │ Trade: $4
 ═══════════════════════════════════════════════════════════════════════════════════════════════
 BINANCE │ BTC: $98,432.50 │ UP (score:+0.35 conf:70%) │ RSI:42 │ Vol:normal │ TREND▲ │ WebSocket
 MARKET  │ btc-updown-15m-1740000 │ Closes in: 8.2min │ MID │ Beat: $98,200.00 (+232.50)
 POLY    │ BTC: $98,432.50 │ UP: $0.52/$0.48 (52%) │ DOWN: $0.48/$0.52 (48%)
 POSITION│ None │ P&L: +$0.00 (0 trades)
 ACTION  │ ─
 SIGNAL  │ ▲ UP      62% [██████░░░░] │ RSI:42↑ │ T:+0.4 │ MACD:+1.2 │ VW:+0.03 │ BB:45%
 ALERT   │ ─
 ─────────────────────────────────────────────────────────────────────────────────────────────
 U=buy UP │ D=buy DOWN │ C=close all │ S=accept signal │ Q=exit
 ═══════════════════════════════════════════════════════════════════════════════════════════════
       UP       DN │  RSI  │  SIGNAL   STRENGTH  │ VOL │ TREND │  MACD │  VWAP │  BB  │     S/R     │ RG
   UP:$0.52 DN:$0.48 │ RSI:42↑ │ ▲ UP  62% [██████░░░░] │ VOL↑ │ T:+0.4⬆ │ +1.2▲ │ +0.03↑ │ MD45% │ SR:+0.3→+0.2 │ T▲
```

## Documentation

| Document | Description |
|---|---|
| **[Trading Guide](docs/TRADING_GUIDE.md)** | Indicators explained, entry/exit rules, scenarios, risk management |
| **[Configuration](docs/configuration.md)** | All 29 `.env` parameters with defaults |
| **[Development Guide](docs/development-guide.md)** | Architecture, signal engine internals, concurrency, extension points |
| **[Backlog](docs/backlog.md)** | Feature roadmap |

## Auto-Trading

Auto-trading is **disabled by default**. Set `AUTO_TRADE_ENABLED=1` in `.env` to opt in.

When enabled, the radar automatically executes trades whenever the signal engine fires an opportunity that passes all safety guards — no keypress required.

### Safety Guards

| Guard | Variable | Default |
|---|---|---|
| Minimum signal strength | `AUTO_MIN_STRENGTH` | `65` |
| Allowed regimes | `AUTO_ALLOWED_REGIMES` | `TREND_UP,TREND_DOWN,RANGE` |
| Allowed phases | `AUTO_ALLOWED_PHASES` | `EARLY,MID,LATE` |
| Max token entry price | `MAX_ENTRY_PRICE` | `0.85` |
| Min token entry price | `MIN_ENTRY_PRICE` | `0.08` |
| Max open positions | `AUTO_MAX_POSITIONS` | `1` |
| Session P&L floor | `AUTO_MAX_LOSS` | `-20` |
| Loss cooldown | `AUTO_LOSS_COOLDOWN` | `120s` |
| Win cooldown | `AUTO_WIN_COOLDOWN` | `30s` |
| Auto-close before expiry | `AUTO_CLOSE_SECONDS` | `45s` |
| TP/SL monitor timeout | `TPSL_TIMEOUT` | `600s` |

When a guard blocks auto-trade, the radar falls back to the standard **manual prompt** (10-second `S`/`U`/`D` window) and shows the skip reason in grey:

```
  auto skipped: loss cooldown (87s left)
  auto skipped: entry $0.88 > max $0.85
```

See [docs/configuration.md](docs/configuration.md) for all parameters.

---

## GitHub Sync

`sync_github.sh` auto-commits and pushes any local changes to GitHub.

### Manual sync

```bash
chmod +x sync_github.sh

./sync_github.sh                   # auto-generates commit message
./sync_github.sh "my message"      # custom commit message
```

### Scheduled sync (macOS — every 30 minutes)

**1. Authenticate git once** (only needed the first time):

```bash
# Option A — Personal Access Token (recommended)
git config credential.helper store
git push   # enter username + PAT when prompted; credentials are saved

# Option B — SSH key (no password prompts)
# Follow: https://docs.github.com/en/authentication/connecting-to-github-with-ssh
```

**2. Install the launchd agent:**

```bash
# Update the path in the plist if your repo is not at /home/camper/polymarket
cp com.polymarket.sync.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.polymarket.sync.plist
```

**3. Verify it's running:**

```bash
launchctl list | grep polymarket
tail -f logs/sync_github.log
```

**4. To stop / uninstall:**

```bash
launchctl unload ~/Library/LaunchAgents/com.polymarket.sync.plist
rm ~/Library/LaunchAgents/com.polymarket.sync.plist
```

> The interval is set to **1800 seconds (30 min)** in the plist. Edit `<integer>1800</integer>` to change it (e.g. `900` = 15 min, `3600` = 1 hour).

---

## Support the Developer

If this tool helps you trade, consider sending a tip:

**https://polymarket.com/profile/0xa27Bf6B2B26594f8A1BF6Ab50B00Ae0e503d71F6**

## License

This project is licensed under the [GNU General Public License v3.0](LICENSE).
