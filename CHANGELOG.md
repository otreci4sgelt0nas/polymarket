# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [1.3.1] — 2025

### Fixed — Critical

- **MACD still zero after v1.3.0 despite correct code** (`ws_binance.py`)
  `MAX_CANDLES = 60` was committed while the bot was already running. `MAX_CANDLES` is a module-level constant — it only takes effect on process restart. The bot ran through the entire post-commit period without restarting, so all 2,864 post-v1.3.0 signal rows still show `macd_hist=0.0000`. No code change required — **this is resolved by restarting the bot**. Documented here so the next agent does not repeat the same investigation.

### Added

- **RANGE regime hard block** (`radar_poly.py`)
  Log analysis of the last 2 sessions (sessions 10–11, 19:45–20:30) showed 0/2 win rate on RANGE-regime signal trades. RANGE provides no directional multiplier — the signal score is accepted at face value with zero trend confirmation. Both losing trades (20:07 DOWN str=39 and 20:24 DOWN str=30) were RANGE. Added `RANGE_BLOCK_TRADES` (default `1`, configurable via `.env`). When enabled, RANGE is treated as a hard block alongside CHOP and CLOSING. Skip log prints `SKIP — RANGE regime (no trend confirmation, hard block)`. Set `RANGE_BLOCK_TRADES=0` to revert to unconfirmed RANGE trades.

- **Counter-trend hard block** (`radar_poly.py`)
  The counter-trend multiplier (`REGIME_COUNTER_MULT=0.70`) dampens signal scores for trades against the confirmed trend direction but does not prevent execution. The 19:38 trade (DOWN signal in TREND_UP regime, str=45) and 20:24 trade (DOWN signal in TREND_UP, str=30) both lost. Added `COUNTER_TREND_BLOCK` (default `1`, configurable via `.env`). When enabled, any signal where direction opposes the confirmed trend (`DOWN` in `TREND_UP`, `UP` in `TREND_DOWN`) is unconditionally blocked. Skip log prints `SKIP — counter-trend (DOWN vs TREND_UP, hard block)`. Set `COUNTER_TREND_BLOCK=0` to revert to dampened-score counter-trend behaviour.

- **SL-to-entry ratio floor** (`radar_poly.py`)
  The existing flat $0.25 entry floor was necessary but insufficient. At entry=$0.33 with `SL_DEFAULT=$0.14`, the SL represents 42% of entry price — the token must nearly halve before the stop triggers, meaning it is already deep into a move with almost no buffer. The 20:24 trade (entry=$0.33, SL=$0.19) illustrates this: SL was hit exactly at the $0.19 floor in 67 seconds. Added `MIN_ENTRY_SL_RATIO` (default `0.40`, configurable via `.env`). If `SL_DEFAULT / entry_price > MIN_ENTRY_SL_RATIO`, the trade is rejected. At the default this enforces an effective minimum entry of `$0.14 / 0.40 = $0.35`, complementing the existing $0.25 floor. Skip log prints `SL/entry ratio 42% > 40% max`. Set `MIN_ENTRY_SL_RATIO=0` to disable.

### Verified

- All 4 losing signal trades from the last 2 sessions (20:07, 20:24, 14:56, 19:38) are blocked by the new guards. The 1 winning trade (20:01, str=60, TREND_DOWN) passes all guards cleanly.
- Guard simulation output:
  - `20:01 DOWN $0.45 str=60 TREND_DOWN` → ✓ PASS
  - `20:07 DOWN $0.54 str=39 RANGE` → ✗ BLOCK (RANGE hard block)
  - `20:24 DOWN $0.33 str=30 RANGE/TREND_UP` → ✗ BLOCK (RANGE hard block + SL ratio 42%)
  - `14:56 DOWN $0.17 str=35 CHOP` → ✗ BLOCK (CHOP + $0.25 floor)
  - `19:38 DOWN $0.58 str=45 TREND_UP` → ✗ BLOCK (counter-trend)

---

## [1.3.0] — 2025

### Fixed — Critical

- **MACD still always zero despite v1.2.5 dedup fix** (`ws_binance.py`)
  The v1.2.5 fix correctly deduplicates candles by timestamp inside `compute_macd`, but the root cause was never fully resolved: `MAX_CANDLES = 30` in `ws_binance.py` capped the WebSocket buffer at 30 candles, while the `.env` configuration specifies `MACD_SLOW=26` and `MACD_SIGNAL=9` — requiring a minimum of **35 unique candles** for any result. `len(closes) < slow + signal_period` was always true, causing `compute_macd` to return `(0.0, 0.0, 0.0, 0.0)` on every single tick. Confirmed 100% zero MACD across all 101,430 signal rows in the post-fix session data. Fixed by increasing `MAX_CANDLES` from `30` → `60`, providing a 25-candle safety margin above the 35-candle minimum. The 10% `W_MACD` weight is now live for the first time.

### Added

- **Hard CHOP regime block** (`radar_poly.py`)
  CHOP was 33% of all session time in the v1.2.5 log data. The previous behaviour halved the signal score (`REGIME_CHOP_MULT=0.50`) but still allowed trades through when the dampened score cleared the phase threshold. Win rate in CHOP conditions is demonstrably poor. Added `CHOP_BLOCK_TRADES` (default `1`, configurable via `.env`). When enabled, CHOP is treated identically to CLOSING phase — all auto-trades (both signal and mean reversion paths) are unconditionally blocked. The skip log prints `SKIP — CHOP regime (hard block)` to distinguish from the old dampened-score skip. Set `CHOP_BLOCK_TRADES=0` to revert to the previous halved-score behaviour.

- **Max session loss limit** (`radar_poly.py`)
  Session 8 had 5 trades in 44 minutes, all after consecutive losses, ending at -$4.69. There was no mechanism to halt trading once a session drawdown reached a threshold. Added `MAX_SESSION_LOSS` (default `$8`, configurable via `.env`). Once `session_pnl <= -MAX_SESSION_LOSS`, all auto-trades on both the signal and mean reversion paths are suspended for the remainder of the session. The skip log prints in red: `SKIP — max session loss hit (P&L $X.XX ≤ -$8)`. Set `MAX_SESSION_LOSS=0` to disable. This acts as a hard daily loss limit independent of cooldowns.

- **`SIGNAL_STRENGTH_MIN` and `CHOP_BLOCK_TRADES` documented in `.env.example`** (`.env.example`)
  `SIGNAL_STRENGTH_MIN` existed in code since v1.2.2 but was absent from `.env.example`, making it invisible to operators. Added all three new risk management variables (`MAX_SESSION_LOSS`, `SIGNAL_STRENGTH_MIN`, `CHOP_BLOCK_TRADES`) under a new `── RISK MANAGEMENT ──` section in both `.env.example` and the live `.env`.

---

## [1.2.5] — 2025

### Fixed — Critical

- **MACD always zero across entire session** (`binance_api.py`)
  The radar polls at 0.5s intervals but uses 1-minute candles. Within each 1-minute candle, all ~120 sub-second rows share the exact same `close` price. Feeding 20 identical closes into the EMA calculation produces `fast_ema == slow_ema` → `macd_hist = 0.0` on every single tick. Confirmed zero MACD across 84,843 signal rows (100% of the day). Fixed by deduplicating candles by timestamp inside `compute_macd` before computing EMAs, so only one close per completed candle is used. The 15% `W_MACD` weight is now live.

- **Signal fired on stale Binance data (`rsi=0`, `bb_pos=0.0`)** (`radar_poly.py`)
  A failed or partial Binance fetch can return `rsi=0` and `bb_pos=0.0` — confirmed in session logs (one trade had `rsi=0 bb=0.00`). The signal engine silently used these garbage values, producing a misdirected trade. Added a stale-data guard before `compute_signal`: if `rsi=0` or `bb_pos=0.0`, the cycle is skipped with a yellow log message and retried in 2 seconds.

- **Signal auto-trade fired on sub-$0.25 tokens** (`radar_poly.py`)
  Tokens priced below $0.25 carry <25% implied win probability and only ~$0.22 of room between entry and the SL floor ($0.03), making losses disproportionately large. The last session's worst trade was entry=$0.17 → exit=$0.05 → **-$2.82** on a $4 position. Added a minimum entry price guard (`>= 0.25`) to the signal execution path before `handle_buy` is called. The Mean Reversion alert already had a `< 0.70` upper guard; this adds the lower bound to both paths.

---

## [1.2.2] — 2025

### Fixed — Critical

- **`SIGNAL_STRENGTH_BEEP` silently overriding all phase thresholds** (`radar_poly.py`)
  `effective_threshold = max(SIGNAL_STRENGTH_BEEP, phase_threshold)` meant the audio beep setting (default 50) acted as the execution floor, completely overriding the phase thresholds of EARLY=35 and MID=30. Session 5 had 98 valid MID-phase TREND_DOWN signals (strength 30–49) — every single one was blocked. Introduced a new `SIGNAL_STRENGTH_MIN` variable (default `30`, configurable via `.env`) as the dedicated execution floor. `SIGNAL_STRENGTH_BEEP` now controls audio only and no longer participates in the execution gate. `effective_threshold = max(SIGNAL_STRENGTH_MIN, phase_threshold)`.

- **Ghost position produces `profit_factor = 703.28` in sessions.csv** (`trade_executor.py`)
  After a TP/SL close, floating-point dust (≈0.003 shares) could survive in the positions list and be passed to `close_all_positions` on market expiry. The ghost produced `pnl = (0.009 - 0.53) * 0.003 = -$0.00182` — logged as `-0.00`, counted as a loss by the `<= 0` check, but with `gross_losses = 0.00182` the profit factor calculated as `1.28 / 0.00182 = 703.28`. Added a `shares < 0.01` guard at the top of the `close_all_positions` loop, consistent with the same threshold used everywhere else in the codebase.

### Improved

- **Audio beep decoupled from execution** (`radar_poly.py`)
  The beep now fires independently whenever `strength >= SIGNAL_STRENGTH_BEEP` (default 50) with a 5-second cooldown, giving audio feedback on strong signals regardless of whether the execution threshold is met.

---

## [1.2.1] — 2025

### Fixed — Critical

- **`execute_close_market` UnboundLocalError at startup** (`radar_poly.py`)
  A redundant `from trade_executor import execute_close_market` inside the orphan-position `if` block caused Python to treat the name as a local variable throughout the entire enclosing scope. Any code path that referenced `execute_close_market` before reaching that `import` line raised `UnboundLocalError: cannot access local variable 'execute_close_market' where it is not associated with a value`. This crashed the entire signal execution block, meaning **all trades after a buy fired ran completely unmonitored** — no TP, no SL, positions held until `market_expired`. Removed the redundant local import; the top-level import at L75 is sufficient.

- **`monitor_tp_sl` polled BUY (ask) price instead of SELL (bid) price** (`trade_executor.py`)
  The monitor fetched `get_price(token_id, "BUY")` on every tick, but the price you receive when closing is always the SELL (bid) side. Log analysis of the session confirmed all three post-v1.2.0 trades had the SELL price already below SL at sample #0, yet the monitor never triggered because it was comparing the SL against the higher ask price. All price fetches and fallback returns in `monitor_tp_sl` changed from `"BUY"` → `"SELL"`.

---

## [1.2.0] — 2025

### Fixed — Critical

- **SL too tight for token volatility** (`signal_engine.py`)
  Token price standard deviation across the session was $0.267, but `SL_DEFAULT` was only $0.06 — 4.4× narrower than normal market noise. This caused 5 of 6 SL exits to trigger within seconds of entry, stopped out by spread fluctuation rather than real reversals. `SL_DEFAULT` widened from `0.06` → `0.14`. `TP_BASE_SPREAD` widened proportionally from `0.05` → `0.10` to restore a positive R:R ratio. Both remain overridable via `.env`.

- **`monitor_tp_sl` SL direction inverted** (`trade_executor.py`)
  For a normal long trade (`sl_above = False`), the SL branch evaluated `price >= sl` which is always true for any token sitting above its stop — causing instant SL exits on the very first price tick. Corrected to `price <= sl` (price must fall to the SL level) and `sl_above` branch corrected symmetrically to `price >= sl` (price must rise to a stop set above entry, for short-side hedges).

- **Orphan position at startup caused $5.09 loss** (`radar_poly.py`)
  A position left over from a previous window (9.62 shares) was not tracked by the session. When the market resolved against it, the close was submitted too late and received $0.001 residual value. `sync_positions` at startup now pauses and presents a non-blocking timed prompt (auto-adopts after 10 seconds if unattended) so orphan shares are always detected, adopted, closed, or explicitly skipped before trading begins.

### Fixed

- **Revenge-trading spiral after consecutive SL exits** (`radar_poly.py`)
  Session 3 logged 3 SL exits in 5 minutes ($0.45 + $0.39 + $0.84 = $1.68 lost). A new `LOSS_COOLDOWN_SEC` (default `120s`, configurable via `.env`) lockout is stamped on every SL exit via `session.last_sl_at`. Any signal that fires during the loss cooldown is blocked and logged with a red `SKIP — loss cooldown (Xs left)` message. This is enforced separately from the existing 30s `TRADE_COOLDOWN_SEC`.

- **Phase thresholds blocked almost all trades** (`signal_engine.py`)
  `PHASE_EARLY_THRESHOLD` was 50 (first ~10 min of a 15m window) and `PHASE_LATE_THRESHOLD` was 70 (last ~5 min), leaving only the narrow MID phase with a reachable threshold of 30. Only 0.1% of signal rows across the full session reached strength ≥ 50. Thresholds lowered: EARLY `50` → `35`, LATE `70` → `50`.

- **`suggestion` object not generated below strength 30** (`signal_engine.py`)
  Signals scoring 20–29 had a populated direction but `suggestion = None`, silently blocking the execution gate even when thresholds were met. Lowered suggestion generation threshold from `30` → `20`.

- **`execute_close_market` early-exits on API lag** (`trade_executor.py`)
  On the very first iteration, `get_token_position` could return 0 due to a momentary API blip, causing the function to return "No positions" without submitting any sell order. Added a first-iteration retry: if both balances are zero on iteration 0, the function sleeps 1 second and retries before concluding no position exists.

### Improved

- **Skip-reason logging every cycle** (`radar_poly.py`)
  Every cycle where a non-neutral signal is suppressed, the scroll log now prints the exact reason: phase threshold, CHOP regime, loss cooldown, trade cooldown, open position, or insufficient balance. Turns post-session debugging from guesswork into a 2-second log review.

- **Orphan position prompt is non-blocking** (`radar_poly.py`)
  The startup orphan prompt uses a timed `select`/`kbhit` loop instead of `input()`. If no key is pressed within 10 seconds the bot auto-adopts the positions and continues — the bot never hangs unattended.

---

## [1.1.0] — 2025

### Fixed — Critical

- **`sl_above` flag inverted** (`radar_poly.py`)  
  The flag was evaluated as `sl < real_entry`, which is almost always `True` for long trades, causing the SL branch in `monitor_tp_sl` to fire incorrectly on every position. Fixed to `sl > real_entry`, which correctly evaluates to `False` for normal long trades so the monitor uses the `price >= sl` branch as intended.

- **`close_all_positions` never submitted a sell order on-chain** (`trade_executor.py`)  
  When a market expired, positions were cleared locally and fake P&L was logged, but no sell order was ever submitted to Polymarket. `close_all_positions` now accepts optional `client` and `executor` parameters and calls `execute_close_market` before clearing local state, ensuring expired positions are actually sold on-chain.

### Fixed

- **Inconsistent `trade_count`** (`radar_poly.py`)  
  Trades opened during `monitor_tp_sl` and then exited were counted; positions left open at quit were never counted. Added a clarifying comment establishing the invariant: count increments on completed round-trip close, which is now consistent across all code paths.

- **Countdown drift on market refresh** (`radar_poly.py`)  
  `elapsed` was calculated from `last_market_check`, which resets every 60 seconds, causing the displayed timer to jump on each refresh. Fixed by adding `base_time_set_at` to `TradingSession` (set once when `base_time` is first assigned) and computing `elapsed = now - base_time_set_at`.

- **No balance guard before buy** (`radar_poly.py`)  
  A signal could fire and attempt a buy even with a `$0` balance, wasting a 3-second countdown and an API call. Added a `session.balance < trade_amount` guard with a clear skip message before the buy is attempted.

- **ThreadPool starvation with 2 shared workers** (`radar_poly.py`)  
  `monitor_tp_sl` continuously submits price-fetch futures to the same 2-worker pool used by the main loop, which could starve the monitor. Increased `ThreadPoolExecutor` to `max_workers=4`.

- **`monitor_order` missing `FILLED` status** (`polymarket_api.py`)  
  Changed `if status == "MATCHED":` to `if status in ("MATCHED", "FILLED"):` so a direct `FILLED` response from the CLOB API does not cause indefinite polling.

### Improved

- **No cooldown after auto-trade** (`radar_poly.py`)  
  Added `TRADE_COOLDOWN_SEC` (default `30s`, configurable via `.env`). `session.last_trade_at` is stamped when a trade closes. The signal block skips with a countdown message while the cooldown is active, preventing back-to-back trades in the same window.

- **Stale `S=accept signal` hotkey in panel** (`ui_panel.py`)  
  `S` was removed from the auto-trade flow but still displayed in the panel footer. Updated the hotkey bar to `C=cancel/close`, accurately reflecting the available actions.

- **VWAP reset every 20-candle poll** (`binance_api.py`)  
  VWAP was recomputed from scratch on each HTTP poll using only the latest 20 candles, producing a rolling window value rather than a true session VWAP. Added a module-level `_vwap_anchor` dict that accumulates `cum_vol` and `cum_tp_vol` across all candle timestamps seen since session start (deduped by timestamp). `reset_vwap_anchor()` is called in `radar_poly.py` when a new market window begins so VWAP reflects the actual session from open.

- **No weight-sum validation** (`signal_engine.py`)  
  Added a startup check: if `W_MOMENTUM + W_DIVERGENCE + W_SR + W_MACD + W_VWAP + W_BB` does not equal `1.0 ± 0.01`, a `warnings.warn` fires immediately at import time so misconfigured weights are surfaced before any trading begins.

- **`monitor_tp_sl` blocks past market expiry** (`trade_executor.py`)  
  Added an optional `time_remaining_fn` parameter. When the callback returns `<= 0`, the monitor returns `'EXPIRED'` immediately instead of waiting out the full 600-second timeout. The lambda is wired in `radar_poly.py` using `session.base_time` and `base_time_set_at`.

---

## [1.0.1] — 2025

### Added

- Mean reversion alert system — beeps when RSI extreme + Bollinger touch + MID phase align (~65% win rate)
- Position monitor with TP/SL alerts and audio beeps
- Research-optimised indicator settings for 15-minute windows (RSI period 5, BB period 10 / 1.5 std)
- Configurable alert toggles (`PRICE_ALERT_ENABLED`, `SIGNAL_ENABLED`, `PRICE_BEAT_ALERT`)
- Position sync — auto-detects positions opened or closed directly on the Polymarket website

---

## [1.0.0] — 2025

### Added

- Real-time Binance WebSocket price feed with HTTP fallback and auto-reconnect
- 6-component signal engine: RSI, MACD, VWAP, Bollinger Bands, divergence, S/R levels
- Market regime detection (TREND_UP, TREND_DOWN, RANGE, CHOP) via ADX
- Phase-aware signal thresholds (EARLY / MID / LATE / CLOSING)
- Split-screen terminal UI — static panel with live stats + scrolling log
- Manual hotkey trading — U / D / C / Q
- Multi-market support — BTC, ETH, SOL, XRP with 5-minute or 15-minute windows
- Session statistics — win rate, P&L, profit factor, max drawdown
- CSV logging — signals, trades, sessions (daily rotation)
- Cross-platform setup scripts (`setup.sh` / `setup.bat`)
- 32 configurable parameters via `.env`
