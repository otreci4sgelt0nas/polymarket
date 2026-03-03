# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

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
