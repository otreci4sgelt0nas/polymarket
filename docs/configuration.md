# Configuration Reference

All 32 parameters are configured via the `.env` file. Copy `.env.example` to `.env` and edit as needed.

---

## Credentials

| Variable | Description | Default |
|---|---|---|
| `POLYMARKET_API_KEY` | Private key exported from Polymarket wallet (0x...) | **Required** |

## Market Selection

| Variable | Description | Default |
|---|---|---|
| `MARKET_ASSET` | Crypto asset to trade (`btc`, `eth`, `sol`, `xrp`) | btc |
| `MARKET_WINDOW` | Market window duration in minutes (`5` or `15`) | 15 |

**Available markets:**

| Asset | 5-minute | 15-minute |
|-------|----------|-----------|
| BTC | `btc-updown-5m` | `btc-updown-15m` |
| ETH | `eth-updown-5m` | `eth-updown-15m` |
| SOL | `sol-updown-5m` | `sol-updown-15m` |
| XRP | `xrp-updown-5m` | `xrp-updown-15m` |

## Trading

| Variable | Description | Default |
|---|---|---|
| `POSITION_LIMIT` | Max exposure in USD (positions + pending orders) | 76 |
| `TRADE_AMOUNT` | Trade amount in USD per operation | 4 |
| `PRICE_ALERT_ENABLED` | Enable (1) or disable (0) audio beep when token price crosses threshold | 1 |
| `PRICE_ALERT` | Price threshold that triggers audio alert | 0.80 |
| `SIGNAL_ENABLED` | Enable (1) or disable (0) signal opportunity beep + trade prompt | 1 |
| `SIGNAL_STRENGTH_BEEP` | Min signal strength (0-100) to trigger opportunity beep | 50 |
| `PRICE_BEAT_ALERT` | Beep when BTC price moves $X or more from Price to Beat (0 = disabled) | 80 |

## Indicator Periods

Optimized for 15-minute mean reversion strategy.

| Variable | Description | Default |
|---|---|---|
| `RSI_PERIOD` | RSI period (2-6 optimal for 15min; shorter = more reactive) | 5 |
| `MACD_FAST` | MACD fast EMA period | 12 |
| `MACD_SLOW` | MACD slow EMA period | 26 |
| `MACD_SIGNAL` | MACD signal line period | 9 |
| `BB_PERIOD` | Bollinger Bands lookback period (tighter for 15min squeeze detection) | 10 |
| `BB_STD` | Bollinger Bands standard deviations (1.5 = tighter bands) | 1.5 |
| `ADX_PERIOD` | ADX period (14 = standard, recommended for regime detection) | 14 |

## Signal Weights

Component weights for the signal score. Must sum to ~1.0. Optimized for mean reversion (divergence + bollinger have higher weight).

| Variable | Component | Default |
|---|---|---|
| `W_MOMENTUM` | BTC Momentum (RSI + candle score) | 0.25 |
| `W_DIVERGENCE` | Divergence (BTC price vs Polymarket price — key for mean reversion) | 0.25 |
| `W_SUPPORT_RESISTANCE` | Support/Resistance levels | 0.10 |
| `W_MACD` | MACD histogram delta (confirmation, not primary) | 0.10 |
| `W_VWAP` | VWAP position + slope | 0.15 |
| `W_BOLLINGER` | Bollinger Bands position (squeeze + band touch = entry signal) | 0.15 |

## Volatility & Regime

| Variable | Description | Default |
|---|---|---|
| `VOL_THRESHOLD` | ATR/price ratio to flag high volatility (0.03 = 3%) | 0.03 |
| `VOL_AMPLIFIER` | Score multiplier when high volatility detected | 1.3 |
| `REGIME_CHOP_MULT` | CHOP regime: dampen signal (0.5 = -50%) | 0.50 |
| `REGIME_TREND_BOOST` | Trend-aligned: boost signal (1.15 = +15%) | 1.15 |
| `REGIME_COUNTER_MULT` | Counter-trend: reduce signal (0.7 = -30%) | 0.70 |

## Phase Thresholds

Min signal strength per market phase (proportional to window size).

| Variable | Phase | Default |
|---|---|---|
| `PHASE_EARLY_THRESHOLD` | EARLY (>66% time left): conservative — wait for data | 55 |
| `PHASE_MID_THRESHOLD` | MID (33-66% time left): best entry window — more permissive | 25 |
| `PHASE_LATE_THRESHOLD` | LATE (6-33% time left): very selective — only high conviction | 70 |

---

## Auto-Trading

> **Disabled by default.** Set `AUTO_TRADE_ENABLED=1` to opt in.  
> The radar remains fully functional as a manual tool when auto-trading is off.

When enabled, the radar automatically executes trades whenever the signal engine fires an opportunity that passes **all** safety guards below — no keypress required. A background thread monitors TP/SL so the main loop never blocks.

### Master Switch

| Variable | Description | Default |
|---|---|---|
| `AUTO_TRADE_ENABLED` | `1` to enable fully automatic execution, `0` for manual-only | `0` |

### Signal Quality Guards

| Variable | Description | Default |
|---|---|---|
| `AUTO_MIN_STRENGTH` | Minimum signal strength (0–100) required to auto-execute | `65` |
| `AUTO_ALLOWED_REGIMES` | Comma-separated list of allowed market regimes (`TREND_UP`, `TREND_DOWN`, `RANGE`, `CHOP`) | `TREND_UP,TREND_DOWN,RANGE` |
| `AUTO_ALLOWED_PHASES` | Comma-separated list of allowed market phases (`EARLY`, `MID`, `LATE`, `CLOSING`) | `EARLY,MID,LATE` |

### Entry Price Guards

These prevent trades with catastrophic risk/reward ratios (e.g. buying UP at $0.93 for a max gain of $0.07 while risking $0.93).

| Variable | Description | Default |
|---|---|---|
| `MAX_ENTRY_PRICE` | Block auto-buy when the token price exceeds this level | `0.85` |
| `MIN_ENTRY_PRICE` | Block auto-buy when the token price is below this level (very low probability bets) | `0.08` |

### Risk / Capital Protection

| Variable | Description | Default |
|---|---|---|
| `AUTO_MAX_POSITIONS` | Maximum number of concurrent open positions; new entries blocked above this | `1` |
| `AUTO_MAX_LOSS` | Session P&L floor in USD — trading halts if `session_pnl` drops to or below this value | `-20` |
| `AUTO_LOSS_COOLDOWN` | Seconds to pause after **any losing trade** before allowing the next auto-entry | `120` |
| `AUTO_WIN_COOLDOWN` | Seconds to pause after **a winning trade** (avoids over-trading in choppy markets) | `30` |

### Execution Timing

| Variable | Description | Default |
|---|---|---|
| `AUTO_CLOSE_SECONDS` | Auto-close all open positions when `time_remaining` drops to or below this many **seconds** | `45` |
| `TPSL_TIMEOUT` | Hard timeout in seconds for the background TP/SL monitor thread | `600` |

### Circuit Breaker

When `session_pnl` falls to or below `AUTO_MAX_LOSS`, the auto-trader **halts immediately** and stops executing new trades. The radar continues to display signals — it just won't fire orders. The status line will read `HALTED — max loss reached`.

To manually reset after reviewing the situation, restart the radar (a future hotkey `R` is planned in the backlog).

### Behaviour When a Guard Blocks Auto-Trade

If `AUTO_TRADE_ENABLED=1` but a guard condition prevents execution, the radar falls back to the standard **manual prompt** (the `OPPORTUNITY!` banner with the 10-second `S`/`U`/`D` window). The reason the auto-trade was skipped is shown beneath the banner in grey, e.g.:

```
  auto skipped: loss cooldown (87s left)
  auto skipped: phase 'EARLY' not in allowed set {'MID', 'LATE'}
  auto skipped: entry $0.88 > max $0.85 (risk/reward 1:8)
```

### Non-Blocking TP/SL Monitor

Once a position is opened (via auto-trade **or** by pressing `S` manually), TP/SL monitoring runs in a **background daemon thread** (`BackgroundTPSL`). The main radar loop continues to update prices, compute signals, and accept hotkeys (`C` to close, `Q` to exit) without any blocking.

Each cycle the scrolling log shows a live progress bar:

```
   ⏳ monitoring [████████░░] TP$0.62 SL$0.46 (+38%)
```

Pressing **C** at any time cancels the background monitor and immediately closes all positions.
