# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

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
