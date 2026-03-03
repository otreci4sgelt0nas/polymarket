# Agent Handoff — Polymarket Scalp Radar

> Read this before touching any code. This is a complete context dump for the incoming agent.

---

## Project Overview

**Repo:** `https://github.com/otreci4sgelt0nas/polymarket.git`
**Branch:** `myWay`
**Language:** Python 3
**Entry point:** `polymarket/radar_poly.py`
**Current version:** `v1.2.5` (+ one hotfix commit on top)

A fully autonomous terminal-based scalping radar for Polymarket UP/DOWN binary markets. It:
- Reads real-time BTC/ETH/SOL/XRP price from Binance via WebSocket (HTTP fallback)
- Computes a 6-component signal (RSI, MACD, VWAP, Bollinger, S/R, Divergence)
- Detects market regime (TREND_UP, TREND_DOWN, RANGE, CHOP) and phase (EARLY, MID, LATE, CLOSING)
- Auto-executes trades on Polymarket via `py_clob_client` with no human intervention required
- Monitors TP/SL in real time, closes positions, logs everything to CSV

---

## Project Structure

```
polymarket/
├── radar_poly.py          # Main entry point — UI loop, trade orchestration
├── src/
│   ├── binance_api.py     # Binance HTTP + indicator computation (RSI, MACD, VWAP, BB, ADX)
│   ├── ws_binance.py      # Binance WebSocket client with auto-reconnect
│   ├── signal_engine.py   # 6-component scoring engine, phase/regime thresholds
│   ├── trade_executor.py  # Buy/sell/close/monitor_tp_sl execution
│   ├── polymarket_api.py  # Polymarket CLOB client wrappers
│   ├── market_config.py   # Asset/window config (BTC/ETH/SOL/XRP, 5m/15m)
│   ├── session_stats.py   # P&L stats calculation and summary display
│   ├── ui_panel.py        # Terminal split-screen panel rendering
│   ├── logger.py          # CSV logging (signals, trades, sessions)
│   ├── colors.py          # ANSI color constants
│   └── input_handler.py   # Non-blocking key reads, sleep_with_key
├── logs/                  # Runtime CSV output (gitignored)
│   ├── signals_YYYY-MM-DD.csv
│   ├── trades_YYYY-MM-DD.csv
│   └── sessions.csv
├── CHANGELOG.md           # Full version history
├── AGENT_HANDOFF.md       # This file
└── .env.example           # All configurable env vars
```

---

## Git Commit History (most recent first)

```
909598e fix: missing closing quote on f-string (line 889 SyntaxError)
b5fe8a2 v1.2.5 — fix MACD zero, stale data guard, min entry price
0699c62 v1.2.4 — auto-execute Mean Reversion trades, remove manual-only prompt
f1c2440 v1.2.3 — remove 3s countdown, fire auto-trades instantly
ca1531e v1.2.2 — decouple beep from execution threshold, fix ghost position PF
a2b481f v1.2.1 — hotfix: UnboundLocalError crash + monitor polling wrong price side
0dd37ea v1.2.0 — session forensics: widen SL/TP, fix SL direction, loss cooldown, orphan guard
```

---

## All Fixes Applied This Session (v1.1.0 → v1.2.5)

### v1.2.5
- **MACD always zero** (`binance_api.py`): Radar polls at 0.5s but uses 1-min candles. All 120 sub-second rows within a candle share the same `close` price → EMAs collapse → `macd_hist=0` forever. Fixed by deduplicating candles by timestamp inside `compute_macd` before EMA calculation. Was confirmed zero across 84,843 rows (100% of day).
- **Stale Binance data guard** (`radar_poly.py`): If `rsi=0` or `bb_pos=0.0` is detected before `compute_signal`, the cycle is skipped with a log message and retried in 2s. One session trade had `rsi=0, bb=0.00` and still fired.
- **Minimum entry price $0.25** (`radar_poly.py`): Tokens below $0.25 have <25% win probability and near-zero SL room. Worst trade was entry=$0.17 → -$2.82. Guard added before `handle_buy` in the signal execution path.

### v1.2.4
- **Mean Reversion auto-execution** (`radar_poly.py`): The MR alert (RSI extreme + BB touch + cheap token) was display-only, printing "Press D to buy". Now auto-fires through the same execution path as signal trades when all guards pass. Falls back to manual prompt only when `SIGNAL_ENABLED=0`.

### v1.2.3
- **Removed 3-second countdown** (`radar_poly.py`): Pre-trade countdown was pure dead time — signal already validated, all guards passed. Removed entirely. Trades fire in the same cycle (~0.5s on WS).

### v1.2.2
- **SIGNAL_STRENGTH_BEEP overriding phase thresholds** (`radar_poly.py`): `effective_threshold = max(SIGNAL_STRENGTH_BEEP=50, phase_threshold)` meant the audio beep setting acted as execution floor, blocking EARLY=35 and MID=30 trades. Session 5 had 98 valid MID signals (str 30–49) — all blocked. Split into `SIGNAL_STRENGTH_MIN=30` (execution) and `SIGNAL_STRENGTH_BEEP=50` (audio only).
- **Ghost position profit_factor=703.28** (`trade_executor.py`): Floating-point dust (<0.01 shares) surviving after TP/SL close was passed to `close_all_positions`, logged as a loss, inflating profit_factor. Added `shares < 0.01` skip guard.

### v1.2.1
- **UnboundLocalError crash** (`radar_poly.py`): A `from trade_executor import execute_close_market` inside the orphan-position `if` block caused Python to treat the name as local throughout `main()`. Any signal execution hit `UnboundLocalError` — trades were opened but monitor never started, positions held to market expiry. Removed the redundant local import.
- **monitor_tp_sl polled BUY price** (`trade_executor.py`): Monitor watched ask (BUY) side. Exit is always at bid (SELL). All price fetches in `monitor_tp_sl` changed from `"BUY"` → `"SELL"`.

### v1.2.0
- **SL too tight** (`signal_engine.py`): Token stdev=$0.267, SL was $0.06 (4.4× too tight). Widened `SL_DEFAULT` 0.06→0.14, `TP_BASE_SPREAD` 0.05→0.10.
- **SL direction inverted** (`trade_executor.py`): `sl_above=False` branch used `price >= sl` (always true for a long above its stop) → instant SL on first tick. Fixed to `price <= sl`.
- **Loss cooldown** (`radar_poly.py`): `LOSS_COOLDOWN_SEC=120` stamps `session.last_sl_at` on every SL exit. Blocks new trades for 2 minutes after a loss. Separate from the existing 30s `TRADE_COOLDOWN_SEC`.
- **Orphan position guard** (`radar_poly.py`): `sync_positions` at startup now shows a timed non-blocking prompt (auto-adopts in 10s) so leftover shares from previous windows are always handled. Root cause of -$5.09 loss (9.62 shares resolved at $0.001).
- **Phase thresholds too high** (`signal_engine.py`): EARLY 50→35, LATE 70→50. Only 0.1% of signals ever hit ≥50.
- **suggestion generated from strength ≥20** (`signal_engine.py`): Was 30; silent block on 20–29 signals.
- **execute_close_market API-lag guard** (`trade_executor.py`): First-iteration zero-position result now retries once before declaring no positions.
- **Skip-reason logging** (`radar_poly.py`): Every suppressed signal prints exact reason to scroll log.

---

## Key Architecture Decisions

### Signal Engine (`signal_engine.py`)
6 weighted components, all configurable via `.env`:
| Component | Weight | Variable |
|---|---|---|
| BTC Momentum (RSI + candle score) | 30% | `W_MOMENTUM` |
| Divergence (BTC vs Poly) | 20% | `W_DIVERGENCE` |
| Support/Resistance | 10% | `W_SR` |
| MACD histogram delta | 15% | `W_MACD` |
| VWAP position + slope | 15% | `W_VWAP` |
| Bollinger position | 10% | `W_BOLLINGER` |

Regime multipliers applied after scoring:
- `CHOP` → score × 0.50
- `TREND_UP/DOWN` (with-trend) → score × 1.15
- `TREND_UP/DOWN` (counter-trend) → score × 0.70

Phase thresholds (min strength to execute):
- `EARLY` (first 66% of window): 35
- `MID` (33–66%): 30
- `LATE` (6–33%): 50
- `CLOSING` (<6%): 999 (blocked)

Effective threshold = `max(SIGNAL_STRENGTH_MIN, phase_threshold)`

### Trade Execution Flow
```
Signal clears threshold
→ Guards: loss_cooldown, trade_cooldown, open_position, balance, entry_price >= $0.25, stale_data
→ handle_buy() fires immediately
→ monitor_tp_sl() polls SELL price every 0.5s
→ TP/SL/CANCEL/EXPIRED/TIMEOUT → execute_close_market()
→ P&L logged, cooldowns stamped
```

### Mean Reversion Path (separate from signal engine)
Fires when: `current_phase == 'MID'` AND `rsi <= 15 AND bb <= 0.10` (UP) or `rsi >= 85 AND bb >= 0.90` (DOWN) AND `token_price < 0.70`.
Uses same guards and execution path as signal trades. TP = `entry + TP_BASE_SPREAD + 0.05`, SL = `entry - SL_DEFAULT`.

### Autonomy
The bot is **fully autonomous** when launched from an activated venv:
- Trades fire automatically with no human input
- The orphan position prompt auto-adopts after 10s if unattended
- The only human gates are: `Q` to quit, `C` to emergency close, `U`/`D` for manual override

---

## Key Configuration Variables (`.env`)

| Variable | Default | Purpose |
|---|---|---|
| `TRADE_AMOUNT` | 4 | USD per trade |
| `SIGNAL_ENABLED` | 1 | Enable/disable auto-trading |
| `SIGNAL_STRENGTH_MIN` | 30 | Minimum strength for execution |
| `SIGNAL_STRENGTH_BEEP` | 50 | Audio beep threshold (separate from execution) |
| `TRADE_COOLDOWN_SEC` | 30 | Cooldown after any completed trade |
| `LOSS_COOLDOWN_SEC` | 120 | Extra cooldown after SL exit |
| `SL_DEFAULT` | 0.14 | Stop-loss offset from entry |
| `TP_BASE_SPREAD` | 0.10 | Base take-profit spread |
| `MARKET_ASSET` | btc | Asset to trade (btc/eth/sol/xrp) |
| `MARKET_WINDOW` | 15 | Window in minutes (5 or 15) |
| `PHASE_EARLY_THRESHOLD` | 35 | Min strength in EARLY phase |
| `PHASE_MID_THRESHOLD` | 30 | Min strength in MID phase |
| `PHASE_LATE_THRESHOLD` | 50 | Min strength in LATE phase |
| `LOSS_COOLDOWN_SEC` | 120 | Post-SL lockout |

---

## Known Remaining Issues / Next Improvements

These were identified from log analysis but NOT yet implemented:

1. **Win rate still low (20–50%)** — signal strength 30–38 is weak. Consider raising `SIGNAL_STRENGTH_MIN` to 35 and watching whether trade frequency vs quality improves.

2. **CHOP regime fires 30% of session time** — signals in CHOP are halved but still sometimes clear threshold. Could add a hard CHOP block (like CLOSING) to prevent any trade when `regime == 'CHOP'`.

3. **R:R still slightly inverted on SL exits** — avg win $0.81, avg loss $1.33 across last sessions. Widening SL further or tightening TP to be more achievable could improve this. Token volatility stdev ~$0.25 — SL=$0.14 is better but still sometimes caught by noise.

4. **No max-trades-per-session limit** — session 8 had 5 trades in 44 min, all after losses. Consider a max daily loss limit (e.g. stop trading after -$5 session P&L).

5. **MACD thresholds (0.5 / 0.1) vs BTC dollar units** — now that MACD is deduped and live, the thresholds in `signal_engine.py` (designed for normalized values) always fire at max score. The MACD component effectively contributes ±0.15 on every trade. This is not wrong but could be normalized (divide histogram by ATR) for finer granularity.

6. **sessions.csv still shows old ghost profit_factor=703.28** — this is a historical log entry, not a live bug. The fix is in place from v1.2.2 onward.

---

## Session Performance History (2026-03-03)

| Session | Version | Trades | W/L | Win% | P&L |
|---|---|---|---|---|---|
| 22:38–07:10 | pre-fix | 6 | 1/5 | 17% | -$1.27 |
| 07:28–09:16 | pre-fix | 2 | 0/2 | 0% | -$5.28 |
| 09:28–09:53 | pre-fix | 3 | 0/3 | 0% | -$1.69 |
| 12:33–13:01 | v1.2.1 | 2 | 1/1 | 50% | +$1.28 |
| 13:20–14:14 | v1.2.2 | 2 | 1/1 | 50% | -$0.35 |
| 14:19–15:03 | v1.2.2 | 5 | 1/4 | 20% | -$4.69 |
| 16:39–19:33 | v1.2.5 | 3 | 2/1 | 67% | **+$1.92** |
| 19:33–19:45 | v1.2.5 | 2 | 1/1 | 50% | **+$2.66** |

Sessions after v1.2.5 are trending positive. The two most recent sessions (post all fixes) both made money.

---

## How to Run

```bash
cd /home/camper/oneTwo/polymarket
source venv/bin/activate
python radar_poly.py          # uses TRADE_AMOUNT from .env (default $4)
python radar_poly.py 10       # $10 per trade
```

Logs are written to `logs/` automatically. Check `logs/sessions.csv` for session summaries and `logs/trades_YYYY-MM-DD.csv` for individual trade detail.

---

## What the Next Agent Should Do

1. **Run a session and check logs** — use the forensic analysis pattern from this session (see below).
2. **Log analysis pattern:**
```bash
cat logs/sessions.csv
cat logs/trades_YYYY-MM-DD.csv
# Then run the python analysis scripts used throughout this session to check:
# - SL timing (were exits premature or correct?)
# - MACD values (now should be non-zero)
# - Signal strength at each buy
# - Phase/regime at each buy
# - Price path after each SL (did price recover? = signal was right, SL too tight)
```
3. **After any code change:** run `diagnostics`, then `git add` → `git commit` → `git push origin myWay`, then update `CHANGELOG.md`.
4. **Do not touch** `polymarket_api.py` or `py_clob_client` internals without understanding the Polymarket CLOB API — the order submission flow is fragile and has already been debugged extensively.