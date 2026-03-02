"""Auto-trading engine: safety guards, circuit breaker, and non-blocking TP/SL monitor.

This module adds fully automated trade execution on top of the existing radar.
All automation is disabled by default — set AUTO_TRADE_ENABLED=1 in .env to opt in.

New .env variables
──────────────────
AUTO_TRADE_ENABLED      0|1     Enable fully automatic trade execution          (default: 0)
AUTO_MIN_STRENGTH       int     Minimum signal strength % to auto-execute        (default: 65)
AUTO_MAX_POSITIONS      int     Max concurrent positions; blocks new entries      (default: 1)
AUTO_MAX_LOSS           float   Session P&L floor; halts trading below this      (default: -20)
AUTO_LOSS_COOLDOWN      int     Seconds to wait after ANY losing trade            (default: 120)
AUTO_WIN_COOLDOWN       int     Seconds to wait after a winning trade             (default: 30)
AUTO_CLOSE_SECONDS      float   Auto-close positions when time_remaining <= this  (default: 45)
AUTO_ALLOWED_REGIMES    str     Comma-separated allowed regimes                  (default: TREND_UP,TREND_DOWN,RANGE)
AUTO_ALLOWED_PHASES     str     Comma-separated allowed phases                   (default: EARLY,MID,LATE)
MAX_ENTRY_PRICE         float   Block buys above this token price                 (default: 0.85)
MIN_ENTRY_PRICE         float   Block buys below this token price (avoid 1c bets) (default: 0.08)
"""

from __future__ import annotations

import os
import time
import logging
import threading
from typing import Callable, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration (loaded once at import time)
# ---------------------------------------------------------------------------

AUTO_TRADE_ENABLED: bool = os.getenv("AUTO_TRADE_ENABLED", "0").lower() in ("1", "true", "yes")
AUTO_MIN_STRENGTH: int = int(os.getenv("AUTO_MIN_STRENGTH", "65"))
AUTO_MAX_POSITIONS: int = int(os.getenv("AUTO_MAX_POSITIONS", "1"))
AUTO_MAX_LOSS: float = float(os.getenv("AUTO_MAX_LOSS", "-20"))
AUTO_LOSS_COOLDOWN: int = int(os.getenv("AUTO_LOSS_COOLDOWN", "120"))
AUTO_WIN_COOLDOWN: int = int(os.getenv("AUTO_WIN_COOLDOWN", "30"))
AUTO_CLOSE_SECONDS: float = float(os.getenv("AUTO_CLOSE_SECONDS", "45"))
AUTO_ALLOWED_REGIMES: frozenset[str] = frozenset(
    os.getenv("AUTO_ALLOWED_REGIMES", "TREND_UP,TREND_DOWN,RANGE").split(",")
)
AUTO_ALLOWED_PHASES: frozenset[str] = frozenset(
    os.getenv("AUTO_ALLOWED_PHASES", "EARLY,MID,LATE").split(",")
)
MAX_ENTRY_PRICE: float = float(os.getenv("MAX_ENTRY_PRICE", "0.85"))
MIN_ENTRY_PRICE: float = float(os.getenv("MIN_ENTRY_PRICE", "0.08"))

# TP/SL monitor poll interval (seconds) in background thread
_TPSL_POLL_INTERVAL: float = 0.5
# Maximum time a TP/SL monitor is allowed to run (seconds)
_TPSL_TIMEOUT: float = float(os.getenv("TPSL_TIMEOUT", "600"))


# ---------------------------------------------------------------------------
# AutoTrader — stateful guard / controller
# ---------------------------------------------------------------------------

class AutoTrader:
    """Evaluates whether the current market state justifies an automatic trade.

    Call ``should_trade()`` each cycle to get a (bool, reason) decision.
    Call ``record_trade(pnl)`` once a position is closed so cooldown timers
    and the circuit-breaker can be updated.
    Call ``reset_breaker()`` to manually re-arm after a halt.

    Thread-safety: this object is only ever read/written from the main loop
    thread, so no locking is required.
    """

    def __init__(self) -> None:
        self._last_trade_time: float = 0.0
        self._last_loss_time: float = 0.0
        self._last_win_time: float = 0.0
        self._halted: bool = False
        self._halt_reason: str = ""
        # Running count of consecutive losses (reset to 0 on win)
        self.consecutive_losses: int = 0

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        """True when AUTO_TRADE_ENABLED=1 in .env."""
        return AUTO_TRADE_ENABLED

    @property
    def halted(self) -> bool:
        return self._halted

    @property
    def halt_reason(self) -> str:
        return self._halt_reason

    # ------------------------------------------------------------------
    # Core decision method
    # ------------------------------------------------------------------

    def should_trade(
        self,
        signal: Optional[dict],
        positions: list,
        session_pnl: float,
        phase: str,
        regime: str,
        time_remaining: float,
        entry_price: float = 0.0,
    ) -> Tuple[bool, str]:
        """Evaluate all guards and return (allowed, human_readable_reason).

        Parameters
        ----------
        signal:         Output of ``compute_signal()``, may be None.
        positions:      Current open positions list.
        session_pnl:    Running session P&L in USD.
        phase:          Current market phase string (EARLY/MID/LATE/CLOSING).
        regime:         Current regime string (TREND_UP/TREND_DOWN/RANGE/CHOP).
        time_remaining: Minutes until market resolution.
        entry_price:    Token buy price for the suggested direction.
        """
        if not AUTO_TRADE_ENABLED:
            return False, "auto-trade disabled"

        # ── Circuit breaker ──────────────────────────────────────────
        if session_pnl <= AUTO_MAX_LOSS and not self._halted:
            self._halted = True
            self._halt_reason = f"max loss reached (${session_pnl:.2f} ≤ ${AUTO_MAX_LOSS:.2f})"
            logger.warning("AutoTrader circuit breaker tripped: %s", self._halt_reason)

        if self._halted:
            return False, f"HALTED — {self._halt_reason}"

        # ── Position limit ───────────────────────────────────────────
        open_count = len(positions)
        if open_count >= AUTO_MAX_POSITIONS:
            return False, f"position limit ({open_count}/{AUTO_MAX_POSITIONS} open)"

        # ── Loss cooldown ────────────────────────────────────────────
        if self._last_loss_time > 0:
            elapsed = time.time() - self._last_loss_time
            remaining_cd = AUTO_LOSS_COOLDOWN - elapsed
            if remaining_cd > 0:
                return False, f"loss cooldown ({remaining_cd:.0f}s left)"

        # ── Win cooldown ─────────────────────────────────────────────
        if self._last_win_time > 0:
            elapsed = time.time() - self._last_win_time
            remaining_cd = AUTO_WIN_COOLDOWN - elapsed
            if remaining_cd > 0:
                return False, f"win cooldown ({remaining_cd:.0f}s left)"

        # ── Phase filter ─────────────────────────────────────────────
        if phase not in AUTO_ALLOWED_PHASES:
            return False, f"phase '{phase}' not in allowed set {set(AUTO_ALLOWED_PHASES)}"

        # ── Regime filter ─────────────────────────────────────────────
        if regime not in AUTO_ALLOWED_REGIMES:
            return False, f"regime '{regime}' not allowed"

        # ── Time-to-expiry filter ─────────────────────────────────────
        if time_remaining <= AUTO_CLOSE_SECONDS / 60:
            return False, f"too close to expiry ({time_remaining:.1f}min ≤ {AUTO_CLOSE_SECONDS}s)"

        # ── Entry price guard ─────────────────────────────────────────
        if entry_price > 0:
            if entry_price > MAX_ENTRY_PRICE:
                return (
                    False,
                    f"entry ${entry_price:.2f} > max ${MAX_ENTRY_PRICE:.2f} "
                    f"(risk/reward 1:{entry_price / max(1 - entry_price, 0.001):.0f})",
                )
            if entry_price < MIN_ENTRY_PRICE:
                return (
                    False,
                    f"entry ${entry_price:.2f} < min ${MIN_ENTRY_PRICE:.2f} (very low probability)",
                )

        # ── Signal checks ─────────────────────────────────────────────
        if signal is None:
            return False, "no signal available"

        direction = signal.get("direction", "NEUTRAL")
        if direction == "NEUTRAL":
            return False, "signal is NEUTRAL"

        strength = signal.get("strength", 0)
        if strength < AUTO_MIN_STRENGTH:
            return False, f"strength {strength}% < threshold {AUTO_MIN_STRENGTH}%"

        if signal.get("suggestion") is None:
            return False, "signal has no entry suggestion"

        return True, "OK"

    # ------------------------------------------------------------------
    # State update after a trade closes
    # ------------------------------------------------------------------

    def record_trade(self, pnl: float) -> None:
        """Update internal state after a position is closed.

        Parameters
        ----------
        pnl: Realised P&L for the closed trade (positive = win, negative = loss).
        """
        self._last_trade_time = time.time()
        if pnl < 0:
            self._last_loss_time = time.time()
            self.consecutive_losses += 1
            logger.debug(
                "AutoTrader: loss recorded ($%.2f), consecutive=%d, cooldown %ds",
                pnl,
                self.consecutive_losses,
                AUTO_LOSS_COOLDOWN,
            )
        else:
            self._last_win_time = time.time()
            self.consecutive_losses = 0
            logger.debug(
                "AutoTrader: win recorded ($%.2f), cooldown %ds",
                pnl,
                AUTO_WIN_COOLDOWN,
            )

    # ------------------------------------------------------------------
    # Circuit breaker manual reset
    # ------------------------------------------------------------------

    def reset_breaker(self) -> None:
        """Manually re-arm the circuit breaker (e.g. after operator review)."""
        was_halted = self._halted
        self._halted = False
        self._halt_reason = ""
        if was_halted:
            logger.info("AutoTrader circuit breaker manually reset")

    # ------------------------------------------------------------------
    # Human-readable status for the UI panel
    # ------------------------------------------------------------------

    def status_line(self, session_pnl: float, time_remaining: float) -> str:
        """Return a short status string suitable for display in the radar panel."""
        if not AUTO_TRADE_ENABLED:
            return "AUTO OFF"
        if self._halted:
            return f"HALTED: {self._halt_reason}"
        # Loss cooldown
        if self._last_loss_time > 0:
            cd_left = AUTO_LOSS_COOLDOWN - (time.time() - self._last_loss_time)
            if cd_left > 0:
                return f"COOLDOWN {cd_left:.0f}s"
        # Win cooldown
        if self._last_win_time > 0:
            cd_left = AUTO_WIN_COOLDOWN - (time.time() - self._last_win_time)
            if cd_left > 0:
                return f"COOLDOWN {cd_left:.0f}s"
        # Near expiry
        if time_remaining <= AUTO_CLOSE_SECONDS / 60:
            return "CLOSING — no trades"
        # How far from loss limit
        headroom = session_pnl - AUTO_MAX_LOSS
        return f"ARMED  | loss room ${headroom:.2f}"


# ---------------------------------------------------------------------------
# BackgroundTPSL — non-blocking TP/SL price monitor
# ---------------------------------------------------------------------------

class BackgroundTPSL:
    """Monitors a token price for TP/SL triggers in a background daemon thread.

    The main loop never blocks.  Each iteration the loop checks ``done`` and,
    when True, reads ``result`` to decide whether to close the position.

    Usage
    -----
    ::

        bg = BackgroundTPSL()
        bg.start(token_id, tp=0.57, sl=0.47,
                 tp_above=True, sl_above=True,
                 get_price=get_price)

        # --- each main-loop cycle ---
        if bg.done:
            outcome, exit_price = bg.result   # 'TP' | 'SL' | 'CANCEL' | 'TIMEOUT'
            bg = None

        # Cancel early (e.g. user presses C):
        bg.cancel()
        outcome, exit_price = bg.result

    Thread-safety
    -------------
    ``done`` and ``result`` are written exactly once by the daemon thread and
    read by the main thread.  Under CPython's GIL a single attribute write is
    atomic enough for this use-case.  ``cancel()`` uses a ``threading.Event``
    which is explicitly thread-safe.
    """

    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._cancel_event: threading.Event = threading.Event()

        # Public output — set by daemon thread, read by main thread
        self.done: bool = False
        self.result: Optional[Tuple[str, float]] = None  # ('TP'/'SL'/'CANCEL'/'TIMEOUT', price)

        # Context kept for display purposes
        self.token_id: str = ""
        self.tp: float = 0.0
        self.sl: float = 0.0
        self.tp_above: bool = True
        self.sl_above: bool = True
        self.entry_price: float = 0.0
        self.start_time: float = 0.0

    # ------------------------------------------------------------------
    # Start / cancel
    # ------------------------------------------------------------------

    def start(
        self,
        token_id: str,
        tp: float,
        sl: float,
        tp_above: bool,
        sl_above: bool,
        get_price: Callable[[str, str], float],
        entry_price: float = 0.0,
        timeout_sec: float = _TPSL_TIMEOUT,
    ) -> None:
        """Launch the background monitor thread.

        Parameters
        ----------
        token_id:    Polymarket conditional token ID to monitor.
        tp:          Take-profit price level.
        sl:          Stop-loss price level.
        tp_above:    True  → TP triggers when price >= tp  (bought expecting UP).
                     False → TP triggers when price <= tp  (bought expecting DOWN).
        sl_above:    True  → SL triggers when price <= sl  (normal: SL below entry).
                     False → SL triggers when price >= sl  (inverse short position).
        get_price:   Callable(token_id, side) → float.  Should be the radar's
                     cached ``get_price`` function.
        entry_price: Entry price recorded for display only.
        timeout_sec: Hard timeout after which 'TIMEOUT' is returned.
        """
        if self._thread is not None and self._thread.is_alive():
            logger.warning("BackgroundTPSL.start() called while monitor already running — ignoring")
            return

        self._cancel_event.clear()
        self.done = False
        self.result = None
        self.token_id = token_id
        self.tp = tp
        self.sl = sl
        self.tp_above = tp_above
        self.sl_above = sl_above
        self.entry_price = entry_price
        self.start_time = time.time()

        self._thread = threading.Thread(
            target=self._run,
            args=(token_id, tp, sl, tp_above, sl_above, get_price, timeout_sec),
            daemon=True,
            name="tpsl-monitor",
        )
        self._thread.start()
        logger.debug(
            "BackgroundTPSL started: token=%s tp=%.3f sl=%.3f tp_above=%s sl_above=%s",
            token_id[:12],
            tp,
            sl,
            tp_above,
            sl_above,
        )

    def cancel(self, join_timeout: float = 2.0) -> None:
        """Signal the monitor to stop and block briefly for a clean shutdown.

        After this call ``done`` is True and ``result`` is ('CANCEL', last_price).
        """
        if self._thread is None or not self._thread.is_alive():
            # Already finished — nothing to do
            if not self.done:
                self.result = ("CANCEL", 0.0)
                self.done = True
            return

        self._cancel_event.set()
        self._thread.join(timeout=join_timeout)
        # Ensure done flag is set even if the thread didn't finish in time
        if not self.done:
            self.result = ("CANCEL", 0.0)
            self.done = True

    # ------------------------------------------------------------------
    # Convenience helpers for the main loop
    # ------------------------------------------------------------------

    def is_running(self) -> bool:
        """Return True while the daemon thread is alive and hasn't finished."""
        return self._thread is not None and self._thread.is_alive() and not self.done

    def progress_bar(self, width: int = 10) -> str:
        """Return an ASCII progress bar showing proximity to TP vs SL.

        Returns an empty string if no valid data is available yet.
        """
        if self.result is not None:
            return ""
        # We don't have the current price here, but we can show a placeholder
        # The main loop should use the last known price from draw_panel instead
        return "─" * width

    def progress_info(self, current_price: float, width: int = 10) -> str:
        """Return a bar + label string using the latest known token price.

        Suitable for printing in the radar's scrolling log each cycle.
        """
        if current_price <= 0 or self.done:
            return ""
        dist_tp = abs(self.tp - current_price)
        dist_sl = abs(self.sl - current_price)
        total = dist_tp + dist_sl
        if total <= 0:
            return ""
        tp_ratio = dist_sl / total          # high ratio = near TP
        filled = round(tp_ratio * width)
        bar = "█" * filled + "░" * (width - filled)
        pct_to_tp = (current_price - self.entry_price) / max(abs(self.tp - self.entry_price), 0.001) * 100
        return f"[{bar}] TP${self.tp:.2f} SL${self.sl:.2f} ({pct_to_tp:+.0f}%)"

    # ------------------------------------------------------------------
    # Internal monitor loop (runs in daemon thread)
    # ------------------------------------------------------------------

    def _run(
        self,
        token_id: str,
        tp: float,
        sl: float,
        tp_above: bool,
        sl_above: bool,
        get_price: Callable[[str, str], float],
        timeout_sec: float,
    ) -> None:
        """Background thread body — never calls print() or modifies shared state
        other than ``self.done`` and ``self.result``."""
        price: float = 0.0
        start = time.time()

        while not self._cancel_event.is_set():
            # Hard timeout guard
            if time.time() - start > timeout_sec:
                logger.info("BackgroundTPSL timeout after %.0fs", timeout_sec)
                self.result = ("TIMEOUT", price if price > 0 else 0.0)
                self.done = True
                return

            # Fetch price — swallow errors; retry next poll
            try:
                fetched = get_price(token_id, "BUY")
                if fetched > 0:
                    price = fetched
            except Exception as exc:
                logger.debug("BackgroundTPSL price fetch error: %s", exc)

            if price > 0:
                # TP check
                if tp_above and price >= tp:
                    logger.info("BackgroundTPSL TP hit: price=%.4f tp=%.4f", price, tp)
                    self.result = ("TP", price)
                    self.done = True
                    return
                if not tp_above and price <= tp:
                    logger.info("BackgroundTPSL TP hit: price=%.4f tp=%.4f", price, tp)
                    self.result = ("TP", price)
                    self.done = True
                    return

                # SL check
                if sl_above and price <= sl:
                    logger.info("BackgroundTPSL SL hit: price=%.4f sl=%.4f", price, sl)
                    self.result = ("SL", price)
                    self.done = True
                    return
                if not sl_above and price >= sl:
                    logger.info("BackgroundTPSL SL hit: price=%.4f sl=%.4f", price, sl)
                    self.result = ("SL", price)
                    self.done = True
                    return

            # Wait for next poll or cancellation signal
            self._cancel_event.wait(timeout=_TPSL_POLL_INTERVAL)

        # Cancelled by external call to cancel()
        logger.debug("BackgroundTPSL cancelled (price at cancel: %.4f)", price)
        self.result = ("CANCEL", price if price > 0 else 0.0)
        self.done = True
