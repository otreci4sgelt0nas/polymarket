# Agent Handoff — Polymarket Scalp Radar

> Read this before touching any code. This is a complete context dump for the incoming agent.
> Previous handoff covered v1.2.5. This document supersedes it entirely and covers v1.3.3.

---

## Project Overview

**Repo:** `https://github.com/otreci4sgelt0nas/polymarket.git`
**Branch:** `myWay`
**Language:** Python 3
**Entry point:** `polymarket/radar_poly.py`
**Current version:** `v1.3.3`

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
(pending) v1.3.3 — MR path entry guards: min floor $0.25, MAX_ENTRY_PRICE ceiling, SL/entry ratio
540e090 v1.3.2 — MAX_ENTRY_PRICE ceiling guard ($0.85), block near-resolved token entries
4ed092c v1.3.1 — RANGE hard block, counter-trend block, SL/entry ratio floor
7865322 v1.3.0 — fix MACD buffer (MAX_CANDLES 30→60), hard CHOP block, max session loss limit
d3d0b1f docs: add AGENT_HANDOFF.md — full context for incoming agent
909598e fix: missing closing quote on f-string (line 889 SyntaxError)
b5fe8a2 v1.2.5 — fix MACD zero, stale data guard, min entry price
0699c62 v1.2.4 — auto-execute Mean Reversion trades, remove manual-only prompt
f1c2440 v1.2.3 — remove 3s countdown, fire auto-trades instantly
ca1531e v1.2.2 — decouple beep from execution threshold, fix ghost position PF
a2b481f v1.2.1 — hotfix: UnboundLocalError crash + monitor polling wrong price side
0dd37ea v1.2.0 — session forensics: widen SL/TP, fix SL direction, loss cooldown, orphan guard
```

---

## All Fixes Applied This Session (v1.3.0 → v1.3.3)

### v1.3.3
- **Mean Reversion path missing entry price guards** (`radar_poly.py`): The MR execution path lacked the three entry-price guards that the signal path has had since v1.3.1/v1.3.2. Added the same three guards to the MR `elif` chain immediately before `handle_buy()`:
  1. `mr_token_price < 0.25` → skip (min floor)
  2. `mr_token_price > MAX_ENTRY_PRICE` → skip (upper ceiling, TP room diagnostic printed)
  3. `SL_DEFAULT / mr_token_price > MIN_ENTRY_SL_RATIO` → skip (SL-ratio floor, ratio printed)
  All three respect the same `.env` knobs as the signal path. This prevents the worst class of MR losses: at `entry=$0.10` with `TRADE_AMOUNT=$5`, a SL at `$0.03` loses `$3.50`. The two largest MR losses in history (`-$3.97` at `$0.15`, `-$3.50` at `$0.10`) are now blocked.

### v1.3.2
- **Near-resolved token entries passing all guards** (`radar_poly.py`): 44% of qualifying signals in the 88-min zero-trade session had `entry > $0.75` — near-settled markets. At `entry=$0.90`, TP is capped at `TP_MAX_PRICE=0.95`, leaving only $0.05 reward vs $0.14 SL (R:R=0.35:1). At `entry=$0.96`, `sug_tp - sug_entry` goes *negative*, producing an inverted `tp_above=False` flag on a long trade — a silent monitor error. Added `MAX_ENTRY_PRICE=0.85` (= `TP_MAX_PRICE − TP_BASE_SPREAD`). Guarantees at least $0.10 TP room on every entry. Configurable via `.env`.

### v1.3.1
- **RANGE regime hard block** (`radar_poly.py`): Log analysis of last 2 sessions showed 0/2 win rate on RANGE-regime signal trades. RANGE has no directional multiplier — signal is taken at face value with no confirmation. Added `RANGE_BLOCK_TRADES=1`. RANGE is now treated identically to CHOP and CLOSING.
- **Counter-trend hard block** (`radar_poly.py`): Counter-trend trades were dampened 0.70× but still executed. Analysis showed DOWN signals in TREND_UP and UP signals in TREND_DOWN consistently losing. Added `COUNTER_TREND_BLOCK=1`. Any signal opposing the confirmed trend is unconditionally blocked.
- **SL-to-entry ratio floor** (`radar_poly.py`): Flat $0.25 floor was insufficient. At `entry=$0.33` with `SL=$0.14`, the SL is 42% of entry — the token nearly halves before SL triggers. Added `MIN_ENTRY_SL_RATIO=0.40`. If `SL_DEFAULT / entry > 0.40`, trade is rejected. Effective floor ≈ $0.35.
- **Guard simulation verified**: All 4 losing signal trades from sessions 10–11 blocked. The 1 winner (str=60, TREND_DOWN, entry=$0.45) passes cleanly.

### v1.3.0
- **MACD still zero despite v1.2.5 dedup fix** (`ws_binance.py`): The v1.2.5 fix deduped candles correctly but the root cause was `MAX_CANDLES=30` in `ws_binance.py` — too small for `MACD_SLOW=26 + MACD_SIGNAL=9 = 35` minimum candles. `compute_macd` always hit `len(closes) < slow + signal_period` and returned `(0.0, 0.0, 0.0, 0.0)`. Confirmed zero across all 101,430 signal rows. Fixed: `MAX_CANDLES` 30 → 60.
- **Hard CHOP block** (`radar_poly.py`): CHOP was 33% of session time. Previous behaviour halved score but still allowed trades through. Added `CHOP_BLOCK_TRADES=1`. CHOP now treated like CLOSING — zero trades.
- **Max session loss limit** (`radar_poly.py`): Added `MAX_SESSION_LOSS=8`. Once `session_pnl <= -$8`, all auto-trades (both signal and MR paths) suspend for the session. Prevents revenge-trading spirals like session 8 (5 trades in 44 min, all after losses).

---

## ⚠️ CRITICAL: MACD Has Never Been Live — Restart Required

**The MACD weight (10%, `W_MACD=0.10`) has contributed zero to every signal score ever computed.** It was broken from day one (candle dedup in v1.2.5 didn't help, buffer was too small). The v1.3.0 fix (`MAX_CANDLES=60`) was committed while the bot was already running — `MAX_CANDLES` is a module-level constant and only reloads on process restart. All post-v1.3.0 signal rows in the log still show `macd_hist=0.0000`.

**Action: restart the bot before the next session.** MACD will be live for the first time.

---

## ✅ FIXED (v1.3.3): Mean Reversion Path Entry Guards

The signal execution path has a full guard stack (see below). The Mean Reversion path previously did **not**. As of v1.3.3 the MR path now has the same three entry-price guards inserted before `handle_buy()` in the `elif` chain (`radar_poly.py` ~L713–L721):

1. `mr_token_price < 0.25` → skip (min floor)
2. `mr_token_price > MAX_ENTRY_PRICE ($0.85)` → skip (upper ceiling)
3. `SL_DEFAULT / mr_token_price > MIN_ENTRY_SL_RATIO (0.40)` → skip (SL-ratio floor)

These would have blocked all five of the worst MR losses:

| Timestamp | Entry | Stake | Shares | SL hit | P&L | Guard that blocks |
|---|---|---|---|---|---|---|
| 2026-03-03 23:53 | $0.15 | $4.97 | 33 | $0.03 | **-$3.97** | SL/entry ratio 93% > 40% |
| 2026-03-04 00:20 | $0.10 | $5.00 | 50 | $0.03 | **-$3.50** | min floor < $0.25 |
| 2026-03-04 01:35 | $0.10 | $1.00 | 10 | $0.03 | -$0.70 | min floor < $0.25 |
| 2026-03-04 02:50 | $0.12 | $0.99 | 8 | $0.03 | -$0.74 | min floor < $0.25 |
| 2026-03-04 03:36 | $0.05 | $1.00 | 20 | $0.03 | -$0.40 | min floor < $0.25 |

**Still missing from MR path:** CHOP/RANGE/counter-trend regime blocks (lower priority — MR only fires in MID phase which has stronger regime signal, and the existing RSI/BB extremes are already a regime-agnostic filter). Monitor MR performance before adding regime blocks.

---

## Key Architecture Decisions

### Signal Engine (`signal_engine.py`)
6 weighted components, all configurable via `.env`:
| Component | Weight | Variable |
|---|---|---|
| BTC Momentum (RSI + candle score) | 25% | `W_MOMENTUM` |
| Divergence (BTC vs Poly) | 25% | `W_DIVERGENCE` |
| Support/Resistance | 10% | `W_SUPPORT_RESISTANCE` |
| MACD histogram delta | 10% | `W_MACD` |
| VWAP position + slope | 15% | `W_VWAP` |
| Bollinger position | 15% | `W_BOLLINGER` |

Regime multipliers applied after scoring:
- `CHOP` → score × 0.50 (but CHOP_BLOCK_TRADES=1 means this never fires)
- `TREND_UP/DOWN` (with-trend) → score × 1.15
- `TREND_UP/DOWN` (counter-trend) → score × 0.70 (but COUNTER_TREND_BLOCK=1 means this never fires)

Phase thresholds (min strength to execute):
- `EARLY` (>66% time left): 35 (from `PHASE_EARLY_THRESHOLD` in `.env`: currently 55 — see note below)
- `MID` (33–66%): 25 (from `PHASE_MID_THRESHOLD` in `.env`: currently 25)
- `LATE` (6–33%): 70 (from `PHASE_LATE_THRESHOLD` in `.env`: currently 70)
- `CLOSING` (<6%): 999 (hard blocked)

> **Note:** The `.env` has `PHASE_EARLY_THRESHOLD=55`, which overrides the code default of 35. This means EARLY phase requires strength ≥55. With mean strength ~23, almost no EARLY trades fire. This may be intentional (operator-set) or forgotten. Worth reviewing.

Effective threshold = `max(SIGNAL_STRENGTH_MIN=30, phase_threshold)`

### Signal Path — Full Guard Stack (v1.3.2)
```
Stale data check (rsi=0 or bb=0.0) → skip cycle
→ CLOSING phase → hard block
→ CHOP regime (CHOP_BLOCK_TRADES=1) → hard block
→ RANGE regime (RANGE_BLOCK_TRADES=1) → hard block
→ Counter-trend (COUNTER_TREND_BLOCK=1) → hard block
→ MAX_SESSION_LOSS exceeded → hard block
→ strength < effective_threshold → skip
→ loss_cooldown active → skip
→ trade_cooldown active → skip
→ open position → skip
→ insufficient balance → skip
→ entry_price < $0.25 → skip
→ entry_price > MAX_ENTRY_PRICE ($0.85) → skip
→ SL_DEFAULT / entry_price > MIN_ENTRY_SL_RATIO (0.40) → skip
→ handle_buy() fires immediately  [MR path now has all three guards above too — v1.3.3]
→ monitor_tp_sl() polls SELL price every 0.5s
→ TP/SL/CANCEL/EXPIRED/TIMEOUT → execute_close_market()
→ P&L logged, cooldowns stamped
```

### Mean Reversion Path (separate from signal engine)
Fires when: `current_phase == 'MID'` AND `(rsi <= 15 AND bb <= 0.10)` (UP) or `(rsi >= 85 AND bb >= 0.90)` (DOWN) AND `token_price < 0.70`.

**Current guards (v1.3.3):** `SIGNAL_ENABLED`, `MAX_SESSION_LOSS`, `session.positions`, `loss_cooldown`, `trade_cooldown`, `balance`, `entry < $0.25` floor, `entry > MAX_ENTRY_PRICE` ceiling, `SL_DEFAULT / entry > MIN_ENTRY_SL_RATIO` ratio floor. Entry price guards are now **identical** to the signal path.

TP = `entry + TP_BASE_SPREAD + 0.05`, SL = `entry - SL_DEFAULT`.

### Autonomy
The bot is **fully autonomous** when launched from an activated venv:
- Trades fire automatically with no human input
- The orphan position prompt auto-adopts after 10s if unattended
- Human gates: `Q` to quit, `C` to emergency close, `U`/`D` for manual override

---

## Current Configuration Variables (`.env`)

| Variable | Live Value | Default | Purpose |
|---|---|---|---|
| `TRADE_AMOUNT` | 4 | 4 | USD per trade |
| `SIGNAL_ENABLED` | 1 | 1 | Enable/disable auto-trading |
| `SIGNAL_STRENGTH_MIN` | 30 | 30 | Execution floor (separate from beep) |
| `SIGNAL_STRENGTH_BEEP` | 25 | 50 | Audio beep threshold only |
| `TRADE_COOLDOWN_SEC` | 15 | 30 | Cooldown after any completed trade |
| `LOSS_COOLDOWN_SEC` | 120 | 120 | Extra cooldown after SL exit |
| `MAX_SESSION_LOSS` | 8 | 8 | Session P&L floor before trading suspends |
| `SL_DEFAULT` | 0.14 | 0.14 | Stop-loss offset from entry |
| `TP_BASE_SPREAD` | 0.10 | 0.10 | Base take-profit spread |
| `TP_MAX_PRICE` | 0.95 | 0.95 | Ceiling on TP (hard-coded in signal_engine.py) |
| `MARKET_ASSET` | btc | btc | Asset to trade (btc/eth/sol/xrp) |
| `MARKET_WINDOW` | 15 | 15 | Window in minutes (5 or 15) |
| `PHASE_EARLY_THRESHOLD` | 55 | 35 | Min strength in EARLY phase ⚠ operator-raised |
| `PHASE_MID_THRESHOLD` | 25 | 30 | Min strength in MID phase |
| `PHASE_LATE_THRESHOLD` | 70 | 50 | Min strength in LATE phase |
| `CHOP_BLOCK_TRADES` | 1 | 1 | Hard block all trades in CHOP regime |
| `RANGE_BLOCK_TRADES` | 1 | 1 | Hard block all trades in RANGE regime |
| `COUNTER_TREND_BLOCK` | 1 | 1 | Hard block counter-trend signals |
| `MIN_ENTRY_SL_RATIO` | 0.40 | 0.40 | Max SL/entry ratio (rejects low-price entries) |
| `MAX_ENTRY_PRICE` | 0.85 | 0.85 | Upper entry ceiling (prevents compressed TP room) |
| `MACD_FAST` | 12 | 5 | MACD fast period |
| `MACD_SLOW` | 26 | 10 | MACD slow period |
| `MACD_SIGNAL` | 9 | 4 | MACD signal period |

---

## Session Performance History

### 2026-03-03 (All Sessions)

| Session | Time | Trades | W/L | Win% | P&L | Notes |
|---|---|---|---|---|---|---|
| 1 | 22:38–07:10 | 6 | 1/5 | 17% | -$1.27 | pre-fix era |
| 2 | 07:28–09:16 | 2 | 0/2 | 0% | -$5.28 | orphan position loss |
| 3 | 09:28–09:53 | 3 | 0/3 | 0% | -$1.69 | revenge spiral |
| 4 | 12:33–13:01 | 2 | 1/1 | 50% | +$1.28 | v1.2.1 |
| 5 | 13:20–14:14 | 2 | 1/1 | 50% | -$0.35 | v1.2.2 |
| 6 | 14:19–15:03 | 5 | 1/4 | 20% | -$4.69 | sub-$0.25 entry worst loss |
| 7 | 16:39–19:33 | 3 | 2/1 | 67% | **+$1.92** | v1.2.5, MR wins |
| 8 | 19:33–19:45 | 2 | 1/1 | 50% | **+$2.66** | MR +$3.97 best trade |
| 9 | 19:45–20:19 | 2 | 1/1 | 50% | +$0.17 | v1.3.0 code, not restarted |
| 10 | 20:20–20:30 | 1 | 0/1 | 0% | -$1.28 | str=30 RANGE entry |
| 11 | 20:40–22:08 | 0 | — | — | $0.00 | 88 min zero trades, external cause |
| 12 | 22:14–23:17 | 2 | 2/0 | 100% | **+$2.15** | MR only, both wins |

### 2026-03-04

| Session | Time | Trades | W/L | Win% | P&L | Notes |
|---|---|---|---|---|---|---|
| 13 | 23:29–01:13 | 3 | 1/2 | 33% | -$6.39 | MR $0.10 entry × $5 stake = -$3.50 |
| 14 | 01:13–09:19 | 3 | 0/3 | 0% | -$1.84 | all MR SL exits at low-price tokens |

### Aggregate Stats (all signal vs MR trades)

| Path | Trades | Win% | Avg Win | Avg Loss | Total P&L |
|---|---|---|---|---|---|
| Signal | 25 | 32% | +$1.25 | -$1.33 | **-$12.58** |
| Mean Reversion | 11 | 45% | +$1.75 | -$1.67 | **-$1.27** |

Signal path is net negative. MR path is much better but two large-stake losses (-$3.97, -$3.50) dominate. Both of those are now preventable with the pending MR entry guard fix.

---

## Known Remaining Issues (Priority Order)

### ✅ Priority 1 — MR Path Entry Guards — DONE (v1.3.3)
Fixed in this session. Three guards added to MR `elif` chain before `handle_buy()`: min floor `$0.25`, max ceiling `MAX_ENTRY_PRICE ($0.85)`, SL/entry ratio `MIN_ENTRY_SL_RATIO (0.40)`. All five historical worst-case MR trades are now blocked. See fixed section above.

### 🔴 Priority 2 — Restart Bot to Activate All v1.3.x Fixes
Four fixes are committed but not yet running (bot has not been restarted since before v1.3.0):
- `MAX_CANDLES=60` → MACD finally live (v1.3.0)
- RANGE block, counter-trend block, SL ratio floor (v1.3.1)
- `MAX_ENTRY_PRICE=0.85` ceiling (v1.3.2)
- MR entry guards (v1.3.3) ← new

### 🟡 Priority 3 — MACD Thresholds Need Calibration
Now that MACD will be live for the first time (`W_MACD=0.10`, MACD 12/26/9 on 1-min BTC candles), the scoring thresholds in `signal_engine.py` were designed for normalised values but receive raw BTC dollar units. The MACD histogram for BTC can be ±$50. The thresholds `abs(macd_hist_delta) > 0.5` (strong) and `> 0.1` (moderate) are far too small — they will always fire at maximum score. Consider normalising: `macd_hist / atr` or `macd_hist / btc_price * 1000`. Check the first live session's signal logs to confirm MACD is non-zero and assess whether it's contributing sensibly.

### 🟡 Priority 4 — PHASE_EARLY_THRESHOLD=55 May Be Too Aggressive
The live `.env` has `PHASE_EARLY_THRESHOLD=55` but the code default is 35. With median signal strength ~23, almost no EARLY-phase signals ever reach 55. This effectively kills trading in the first two-thirds of every 15-min window. In the 88-min zero-trade session, 3 of the 5 valid opportunities were EARLY phase — they would have been blocked by this threshold even without RANGE/counter-trend filters. Consider reverting to 35 or at least 45.

### 🟡 Priority 5 — Signal Win Rate (32%) Still Below Breakeven
R:R is slightly inverted: avg win $1.25 vs avg loss $1.33. To break even at 32% win rate you need R:R > 2.1:1. Current R:R ≈ 0.94:1. Either:
- Raise `SIGNAL_STRENGTH_MIN` from 30 to 38–40 (higher conviction only)
- Widen TP: increase `TP_BASE_SPREAD` from 0.10 to 0.12–0.15
- Or both

### 🟢 Priority 6 — 88-Min Zero-Trade Session Root Cause Unknown
The bot ran 88 min (20:40–22:08) with 11 genuine qualifying signals and 0 trades. Signal CSV was healthy throughout. No stale data, no cooldown, no session loss. Most likely `SIGNAL_ENABLED` was toggled or a buy API call failed silently on every attempt. Not a code bug — but worth watching. If it happens again, check: (a) `SIGNAL_ENABLED` in `.env`, (b) API key validity, (c) balance vs `TRADE_AMOUNT`.

---

## How to Run

```bash
cd /home/camper/oneTwo/polymarket
source venv/bin/activate
python radar_poly.py          # uses TRADE_AMOUNT from .env (currently $4)
python radar_poly.py 10       # override to $10 per trade
```

Logs are written to `logs/` automatically. Check `logs/sessions.csv` for session summaries and `logs/trades_YYYY-MM-DD.csv` for individual trade detail.

---

## What the Next Agent Should Do

### Step 1 — ✅ MR entry guards — DONE (v1.3.3)
Already implemented. Three guards (`$0.25` floor, `MAX_ENTRY_PRICE` ceiling, `SL/entry` ratio) added to the MR `elif` chain in `radar_poly.py` ~L713–L721. No further action needed here.

### Step 2 — Restart the bot
Commit v1.3.3, push, then restart the bot. This activates `MAX_CANDLES=60` (MACD), all v1.3.1 regime guards, v1.3.2 entry ceiling, and v1.3.3 MR guards simultaneously for the first time.

### Step 3 — Run a session and check MACD
After restart, check the first few signal rows in `logs/signals_YYYY-MM-DD.csv`:
- `macd_hist` column should be non-zero (e.g. `±5` to `±50` for BTC)
- If still zero: verify `MAX_CANDLES` in `src/ws_binance.py` is 60, and that `MACD_SLOW + MACD_SIGNAL = 35` is less than 60

### Step 4 — Log analysis pattern
```bash
cat logs/sessions.csv
cat logs/trades_YYYY-MM-DD.csv
```

Key things to check per session:
- **MACD column** in signals CSV — should be non-zero after restart
- **Signal strength at each buy** — anything below 35 is weak; track win rate by strength bucket
- **Regime at each buy** — should only be TREND_UP or TREND_DOWN (CHOP/RANGE/CLOSING hard blocked)
- **Entry price at each buy** — should be $0.35–$0.85
- **SL timing** — exits within 15s = spread noise, exits at 60–120s = genuine move against
- **MR stake size** — should now be capped by entry guards (confirm no $5 stake at $0.10 entry)

### Step 5 — After any code change
```bash
# always in this order:
# 1. run diagnostics (no errors)
# 2. git add -A
# 3. git commit -m "vX.Y.Z — description"
# 4. git push origin myWay
# 5. update CHANGELOG.md
```

**Do not touch** `polymarket_api.py` or `py_clob_client` internals without understanding the Polymarket CLOB API — the order submission flow is fragile and has been debugged extensively over many sessions.