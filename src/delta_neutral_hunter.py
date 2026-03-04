"""
Delta-Neutral Hunter — background arbitrage thread.

Scans Best Ask prices for UP and DOWN tokens every DN_SCAN_INTERVAL seconds.
When Price_UP_ask + Price_DOWN_ask < DN_THRESHOLD (default 0.985), both legs
are bought simultaneously via limit orders, locking in a guaranteed ~1.5% profit
regardless of market direction.

Payout mechanics:
  - Each share pair resolves to exactly $1.00 (one leg wins, one leg loses,
    net = $1.00 per pair purchased).
  - Cost per pair = ask_up + ask_dn  (before fees).
  - Polymarket taker fee ≈ 2% in 2026 fee structure, applied per leg.
  - Gross profit per pair = 1.00 - (ask_up + ask_dn)
  - Net profit per pair  = gross - fee_up_cost - fee_dn_cost
  - Threshold DN_THRESHOLD=0.985 gives ~1.5% gross spread after a nominal
    combined ~1% fee buffer, targeting ≥0.5% net per pair.

Thread safety:
  - Uses its OWN ClobClient instance (separate from main loop) — no shared
    mutable state on the client object.
  - Uses its OWN ThreadPoolExecutor (4 workers) — does not starve main loop.
  - Communicates with main loop via thread-safe primitives only:
      · threading.Event  _stop_event     — graceful shutdown
      · threading.Event  _pause_event    — paused while main loop holds a position
      · threading.Lock   _print_lock     — serialise terminal writes
      · queue.Queue      _result_queue   — push arb results to main loop for logging

Configuration (.env):
  DN_ENABLED        1       Enable/disable hunter entirely
  DN_THRESHOLD      0.985   Max combined ask price to trigger arb
  DN_STAKE          4       USD per LEG (defaults to TRADE_AMOUNT)
  DN_MAX_DAILY      5       Max arb trades per calendar day (0 = unlimited)
  DN_SCAN_INTERVAL  1.0     Seconds between price scans
  DN_ORDER_TIMEOUT  20      Seconds before an unfilled leg order is cancelled
  DN_MIN_SHARES     5       Minimum shares per leg (Polymarket minimum)
  DN_FEE_ESTIMATE   0.01    Per-leg fee estimate used in net-profit display only
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeout
from datetime import datetime

from py_clob_client.clob_types import (
    AssetType,
    BalanceAllowanceParams,
    OrderArgs,
    OrderType,
    PartialCreateOrderOptions,
)

from colors import B, C, D, G, M, R, W, X, Y

logger = logging.getLogger(__name__)

# ── Configuration (read once at import; restart to pick up .env changes) ──────

DN_ENABLED       = os.getenv("DN_ENABLED", "1").lower() in ("1", "true", "yes")
DN_THRESHOLD     = float(os.getenv("DN_THRESHOLD", "0.985"))
_dn_stake_env = os.getenv("DN_STAKE", "")
DN_STAKE         = float(_dn_stake_env) if _dn_stake_env else float(os.getenv("TRADE_AMOUNT", "4"))
DN_MAX_DAILY     = int(os.getenv("DN_MAX_DAILY", "5"))
DN_SCAN_INTERVAL = float(os.getenv("DN_SCAN_INTERVAL", "1.0"))
DN_ORDER_TIMEOUT = int(os.getenv("DN_ORDER_TIMEOUT", "20"))
DN_MIN_SHARES    = int(os.getenv("DN_MIN_SHARES", "5"))
DN_FEE_ESTIMATE  = float(os.getenv("DN_FEE_ESTIMATE", "0.01"))

# CLOB REST endpoint (same constant as polymarket_api.py)
CLOB = "https://clob.polymarket.com"


# ── Result record pushed to main loop ─────────────────────────────────────────

class ArbResult:
    """Immutable record pushed onto _result_queue after each arb attempt."""

    __slots__ = (
        "timestamp", "ask_up", "ask_dn", "combined", "spread",
        "shares_up", "shares_dn", "fill_price_up", "fill_price_dn",
        "status",      # 'FILLED' | 'PARTIAL' | 'FAILED' | 'CANCELLED'
        "net_profit",  # estimated net profit in USD (after fee estimate)
        "note",        # human-readable detail string
    )

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)

    def __repr__(self):
        return (
            f"ArbResult({self.status} ask={self.combined:.4f} "
            f"spread={self.spread:.4f} net=${self.net_profit:.4f})"
        )


# ── Hunter ────────────────────────────────────────────────────────────────────

class DeltaNeutralHunter:
    """
    Background thread that scans for delta-neutral arbitrage opportunities.

    Lifecycle:
        hunter = DeltaNeutralHunter(create_client_fn, get_price_fn, radar_logger)
        hunter.set_market(token_up, token_down)
        hunter.start()
        ...
        hunter.pause()   # while directional trade is live
        hunter.resume()
        hunter.stop()    # on shutdown

    The main loop should drain hunter.result_queue each cycle and log results.
    """

    def __init__(self, create_client_fn, get_price_fn, radar_logger,
                 print_lock: threading.Lock | None = None,
                 stake_amount: float = DN_STAKE,
                 get_balance_fn=None):
        """
        Args:
            create_client_fn: callable() -> (ClobClient, limit) — same as
                              polymarket_api.create_client.  Called once at
                              thread start to create the hunter's own client.
            get_price_fn:     callable(token_id, side) -> float — the same
                              PriceCache.get wrapper used by the main loop.
            radar_logger:     RadarLogger instance for CSV logging.
            print_lock:       optional threading.Lock for serialised terminal
                              output.  A new lock is created if None.
            stake_amount:     USD per leg (defaults to DN_STAKE)
            get_balance_fn:   callable(client) -> float — returns available balance.
        """
        self._create_client = create_client_fn
        self._get_price     = get_price_fn
        self._get_balance   = get_balance_fn
        self._logger        = radar_logger
        self._print_lock    = print_lock or threading.Lock()
        self.stake_amount   = stake_amount

        # Shared market state — written by main loop, read by hunter thread
        self._token_up:   str = ""
        self._token_dn:   str = ""
        self._market_lock = threading.Lock()

        # Control events
        self._stop_event  = threading.Event()
        self._pause_event = threading.Event()   # set = paused
        self._pause_event.clear()               # start unpaused

        # Results for main loop consumption
        self.result_queue: queue.Queue[ArbResult] = queue.Queue()

        # Daily trade counter (reset at midnight)
        self._daily_count    = 0
        self._daily_date_str = ""

        # Own executor — never shared with main loop
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="dn_hunter")

        self._thread: threading.Thread | None = None
        self._client = None   # set inside thread after create_client_fn()

        # Public status for UI panel (written by hunter, read by main thread)
        self.status: str = "idle"          # 'idle' | 'scanning' | 'firing' | 'paused' | 'stopped'
        self.last_combined: float = 0.0    # last observed ask_up + ask_dn
        self.session_arb_count: int = 0    # successful arbs this session
        self.session_arb_pnl:   float = 0.0

    # ── Public API ────────────────────────────────────────────────────────────

    def set_market(self, token_up: str, token_dn: str) -> None:
        """Update the token IDs to scan. Safe to call from main thread at any time."""
        with self._market_lock:
            self._token_up = token_up
            self._token_dn = token_dn

    def start(self) -> bool:
        """Start the hunter thread. Returns False if DN_ENABLED=0."""
        if not DN_ENABLED:
            self.status = "disabled"
            return False
        if self._thread and self._thread.is_alive():
            return True
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="dn_hunter", daemon=True
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        """Signal the hunter thread to stop and wait for it to exit."""
        self._stop_event.set()
        self._pause_event.clear()   # unblock if paused
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._executor.shutdown(wait=False)
        self.status = "stopped"

    def pause(self) -> None:
        """Pause scanning (e.g. while main loop holds a directional position)."""
        self._pause_event.set()
        self.status = "paused"

    def resume(self) -> None:
        """Resume scanning after a pause."""
        self._pause_event.clear()
        if not self._stop_event.is_set():
            self.status = "scanning"

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ── Internal ──────────────────────────────────────────────────────────────

    def _print(self, msg: str) -> None:
        """Thread-safe terminal print (prefixed so it's visually distinct)."""
        with self._print_lock:
            print(msg)

    def _reset_daily_counter_if_needed(self) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        if today != self._daily_date_str:
            self._daily_count    = 0
            self._daily_date_str = today

    def _daily_limit_reached(self) -> bool:
        self._reset_daily_counter_if_needed()
        return DN_MAX_DAILY > 0 and self._daily_count >= DN_MAX_DAILY

    def _run(self) -> None:
        """Main hunter loop — runs on the daemon thread."""
        self._print(
            f"   {M}{B}[DN Hunter]{X} {G}Started{X} — threshold={DN_THRESHOLD:.4f} "
            f"stake=${self.stake_amount:.0f}/leg interval={DN_SCAN_INTERVAL:.1f}s"
        )

        # Create own CLOB client
        try:
            self._client, _ = self._create_client()
            self._print(f"   {M}{B}[DN Hunter]{X} {G}✓ CLOB client ready{X}")
        except Exception as e:
            self._print(f"   {M}{B}[DN Hunter]{X} {R}✗ Client init failed: {e}{X}")
            self.status = "stopped"
            return

        self.status = "scanning"
        consecutive_errors = 0

        while not self._stop_event.is_set():
            # Honour pause (directional trade active)
            if self._pause_event.is_set():
                self.status = "paused"
                time.sleep(0.2)
                continue

            # Get current market tokens (snapshot under lock)
            with self._market_lock:
                token_up = self._token_up
                token_dn = self._token_dn

            if not token_up or not token_dn:
                time.sleep(DN_SCAN_INTERVAL)
                continue

            if self._daily_limit_reached():
                self.status = "scanning"   # still show as scanning, just won't fire
                time.sleep(DN_SCAN_INTERVAL)
                continue

            # ── Fetch both asks concurrently ──────────────────────────────
            try:
                fut_up = self._executor.submit(self._get_price, token_up, "BUY")
                fut_dn = self._executor.submit(self._get_price, token_dn, "BUY")
                ask_up = fut_up.result(timeout=5)
                ask_dn = fut_dn.result(timeout=5)
                consecutive_errors = 0
            except (FuturesTimeout, Exception) as e:
                consecutive_errors += 1
                logger.debug("[DN Hunter] price fetch error (streak=%d): %s", consecutive_errors, e)
                if consecutive_errors >= 5:
                    self._print(
                        f"   {M}{B}[DN Hunter]{X} {Y}⚠ {consecutive_errors} consecutive price errors — "
                        f"sleeping 10s{X}"
                    )
                    time.sleep(10)
                else:
                    time.sleep(DN_SCAN_INTERVAL)
                continue

            if ask_up <= 0 or ask_dn <= 0:
                time.sleep(DN_SCAN_INTERVAL)
                continue

            combined = ask_up + ask_dn
            self.last_combined = combined
            self.status = "scanning"

            # ── Opportunity? ──────────────────────────────────────────────
            if combined >= DN_THRESHOLD:
                time.sleep(DN_SCAN_INTERVAL)
                continue

            # Double-check: re-fetch immediately to filter stale quotes
            try:
                fut_up2 = self._executor.submit(self._get_price, token_up, "BUY")
                fut_dn2 = self._executor.submit(self._get_price, token_dn, "BUY")
                ask_up2 = fut_up2.result(timeout=5)
                ask_dn2 = fut_dn2.result(timeout=5)
            except Exception as e:
                logger.debug("[DN Hunter] confirmation fetch failed: %s", e)
                time.sleep(DN_SCAN_INTERVAL)
                continue

            combined2 = ask_up2 + ask_dn2
            # Use the worse (higher) of the two combined quotes for conservatism
            worst_combined = max(combined, combined2)
            ask_up_use = max(ask_up, ask_up2)
            ask_dn_use = max(ask_dn, ask_dn2)

            if worst_combined >= DN_THRESHOLD:
                logger.debug(
                    "[DN Hunter] Opportunity evaporated on re-check: %.4f → %.4f",
                    combined, combined2,
                )
                time.sleep(DN_SCAN_INTERVAL)
                continue

            spread = 1.0 - worst_combined
            now_str = datetime.now().strftime("%H:%M:%S")
            self._print(
                f"\n   {M}{B}{'═' * 55}{X}\n"
                f"   {M}{B}  DELTA-NEUTRAL ARB ◆ {now_str}{X}\n"
                f"   {W}  UP ask: {G}${ask_up_use:.4f}{X}  DN ask: {R}${ask_dn_use:.4f}{X}  "
                f"Combined: {C}{B}${worst_combined:.4f}{X}  "
                f"Spread: {G}{B}+${spread:.4f} ({spread*100:.2f}%){X}\n"
                f"   {M}{B}{'═' * 55}{X}"
            )

            # ── Execute both legs ─────────────────────────────────────────
            self.status = "firing"

            # Fetch balance if available, otherwise assume enough (to not break old behavior)
            current_balance = float('inf')
            if self._get_balance and self._client:
                try:
                    current_balance = self._get_balance(self._client)
                except Exception as e:
                    logger.debug("[DN Hunter] balance fetch error: %s", e)

            result = self._execute_arb(token_up, token_dn, ask_up_use, ask_dn_use,
                                       worst_combined, spread, now_str, current_balance)
            self.result_queue.put(result)

            if result.status == "FILLED":
                self._daily_count       += 1
                self.session_arb_count  += 1
                self.session_arb_pnl    += result.net_profit
                self._logger.log_trade(
                    "BUY", "delta_neutral_up",
                    result.shares_up, result.fill_price_up,
                    result.shares_up * result.fill_price_up,
                    "delta_neutral", 0.0, 0.0,
                )
                self._logger.log_trade(
                    "BUY", "delta_neutral_dn",
                    result.shares_dn, result.fill_price_dn,
                    result.shares_dn * result.fill_price_dn,
                    "delta_neutral", 0.0, 0.0,
                )
                pnl_est = result.net_profit
                pnl_color = G if pnl_est >= 0 else R
                self._print(
                    f"   {G}{B}  ✓ ARB FILLED{X} │ "
                    f"UP {result.shares_up:.0f}sh @ ${result.fill_price_up:.4f} + "
                    f"DN {result.shares_dn:.0f}sh @ ${result.fill_price_dn:.4f}\n"
                    f"   {pnl_color}{B}  Est. net profit: ${pnl_est:.4f} "
                    f"({self.session_arb_count} arbs this session){X}\n"
                )
            elif result.status == "PARTIAL":
                self._print(
                    f"   {Y}{B}  ⚠ PARTIAL FILL{X} — {result.note}\n"
                    f"   {Y}  Manual review recommended. One leg may be open.{X}\n"
                )
            else:
                self._print(
                    f"   {R}{B}  ✗ ARB FAILED{X} — {result.note}\n"
                )

            self.status = "scanning"
            # Brief pause after execution before scanning again
            time.sleep(max(DN_SCAN_INTERVAL, 2.0))

        self._print(f"   {M}{B}[DN Hunter]{X} {D}Stopped.{X}")
        self.status = "stopped"

    # ── Order execution ───────────────────────────────────────────────────────

    def _submit_limit_buy(self, token_id: str, price: float, shares: float) -> str | None:
        """
        Submit a GTC limit buy order at exactly `price` for `shares` shares.
        Returns order_id on success, None on failure.
        Does NOT monitor fill — caller handles that.
        """
        try:
            tick_size = self._client.get_tick_size(token_id)
            neg_risk  = self._client.get_neg_risk(token_id)
            order = self._client.create_order(
                OrderArgs(token_id=token_id, price=price, size=shares, side="BUY"),
                options=PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk),
            )
            resp = self._client.post_order(order, orderType=OrderType.GTC)
            if isinstance(resp, dict):
                return resp.get("orderID") or resp.get("id")
        except Exception as e:
            logger.debug("[DN Hunter] _submit_limit_buy(%s, %.4f, %.2f): %s",
                         token_id[:8], price, shares, e)
        return None

    def _poll_order_fill(self, order_id: str) -> tuple[str, dict | None]:
        """
        Poll order status until FILLED, CANCELLED, or DN_ORDER_TIMEOUT.
        Returns (status_str, order_dict | None).
        status_str: 'FILLED' | 'CANCELLED' | 'TIMEOUT' | 'ERROR'
        """
        deadline = time.time() + DN_ORDER_TIMEOUT
        while time.time() < deadline:
            if self._stop_event.is_set():
                return "CANCELLED", None
            try:
                order = self._client.get_order(order_id)
                if not isinstance(order, dict):
                    time.sleep(1)
                    continue
                status = order.get("status", "UNKNOWN")
                if status in ("MATCHED", "FILLED"):
                    return "FILLED", order
                if status in ("CANCELED", "CANCELLED"):
                    return "CANCELLED", order
            except Exception as e:
                logger.debug("[DN Hunter] poll_order %s: %s", order_id[:8], e)
            time.sleep(1)
        return "TIMEOUT", None

    def _cancel_order(self, order_id: str) -> None:
        """Best-effort cancel — never raises."""
        try:
            self._client.cancel(order_id)
        except Exception as e:
            logger.debug("[DN Hunter] cancel %s: %s", order_id[:8], e)

    def _execute_arb(
        self,
        token_up: str,
        token_dn: str,
        ask_up: float,
        ask_dn: float,
        combined: float,
        spread: float,
        now_str: str,
        current_balance: float = float('inf'),
    ) -> ArbResult:
        """
        Submit both legs simultaneously, poll for fills, cancel if either fails.

        Strategy:
          - Both orders go in as GTC limit buys at the observed ask price.
          - We poll both orders concurrently.
          - If BOTH fill → FILLED result.
          - If one fills and the other fails/cancels → cancel the filled leg too
            (market sell via a limit order well below mid, at SL_MIN_PRICE floor)
            and return PARTIAL.
          - If both fail → FAILED result.

        Order price = ask price exactly (no additional offset — the arb spread
        IS the profit; adding BUY_PRICE_OFFSET would reduce it further).
        We cap at 0.99 to avoid CLOB rejection.
        """
        MAX_PRICE = 0.99
        MIN_SHARES = max(DN_MIN_SHARES, 1)

        price_up = min(round(ask_up, 4), MAX_PRICE)
        price_dn = min(round(ask_dn, 4), MAX_PRICE)

        shares_up = round(self.stake_amount / price_up, 2)
        shares_dn = round(self.stake_amount / price_dn, 2)

        total_cost = (shares_up * price_up) + (shares_dn * price_dn)
        if current_balance < total_cost:
            return ArbResult(
                timestamp=now_str, ask_up=ask_up, ask_dn=ask_dn,
                combined=combined, spread=spread,
                shares_up=shares_up, shares_dn=shares_dn,
                fill_price_up=0.0, fill_price_dn=0.0,
                status="FAILED", net_profit=0.0,
                note=(
                    f"Insufficient balance: need ${total_cost:.2f} "
                    f"(UP=${shares_up*price_up:.2f}, DN=${shares_dn*price_dn:.2f}), "
                    f"have ${current_balance:.2f}."
                ),
            )

        if shares_up < MIN_SHARES or shares_dn < MIN_SHARES:
            return ArbResult(
                timestamp=now_str, ask_up=ask_up, ask_dn=ask_dn,
                combined=combined, spread=spread,
                shares_up=shares_up, shares_dn=shares_dn,
                fill_price_up=0.0, fill_price_dn=0.0,
                status="FAILED", net_profit=0.0,
                note=(
                    f"Shares below minimum: UP={shares_up:.2f} DN={shares_dn:.2f} "
                    f"(min={MIN_SHARES}). Increase DN_STAKE / TRADE_AMOUNT."
                ),
            )

        # ── Submit both orders concurrently ───────────────────────────────
        self._print(
            f"   {M}  Submitting UP limit buy: {shares_up:.2f}sh @ ${price_up:.4f}{X}"
        )
        self._print(
            f"   {M}  Submitting DN limit buy: {shares_dn:.2f}sh @ ${price_dn:.4f}{X}"
        )

        fut_up = self._executor.submit(self._submit_limit_buy, token_up, price_up, shares_up)
        fut_dn = self._executor.submit(self._submit_limit_buy, token_dn, price_dn, shares_dn)

        try:
            order_id_up = fut_up.result(timeout=15)
        except Exception as e:
            logger.debug("[DN Hunter] UP order submit failed: %s", e)
            order_id_up = None

        try:
            order_id_dn = fut_dn.result(timeout=15)
        except Exception as e:
            logger.debug("[DN Hunter] DN order submit failed: %s", e)
            order_id_dn = None

        if not order_id_up and not order_id_dn:
            return ArbResult(
                timestamp=now_str, ask_up=ask_up, ask_dn=ask_dn,
                combined=combined, spread=spread,
                shares_up=shares_up, shares_dn=shares_dn,
                fill_price_up=0.0, fill_price_dn=0.0,
                status="FAILED", net_profit=0.0,
                note="Both leg order submissions failed (API error).",
            )

        # If one leg failed to get an order ID, cancel the other immediately
        if not order_id_up and order_id_dn:
            self._cancel_order(order_id_dn)
            return ArbResult(
                timestamp=now_str, ask_up=ask_up, ask_dn=ask_dn,
                combined=combined, spread=spread,
                shares_up=0, shares_dn=0,
                fill_price_up=0.0, fill_price_dn=0.0,
                status="FAILED", net_profit=0.0,
                note="UP leg submission failed; DN leg cancelled.",
            )

        if order_id_up and not order_id_dn:
            self._cancel_order(order_id_up)
            return ArbResult(
                timestamp=now_str, ask_up=ask_up, ask_dn=ask_dn,
                combined=combined, spread=spread,
                shares_up=0, shares_dn=0,
                fill_price_up=0.0, fill_price_dn=0.0,
                status="FAILED", net_profit=0.0,
                note="DN leg submission failed; UP leg cancelled.",
            )

        # ── Poll both fills concurrently ──────────────────────────────────
        poll_up = self._executor.submit(self._poll_order_fill, order_id_up)
        poll_dn = self._executor.submit(self._poll_order_fill, order_id_dn)

        status_up, details_up = poll_up.result()
        status_dn, details_dn = poll_dn.result()

        filled_up = status_up == "FILLED"
        filled_dn = status_dn == "FILLED"

        fill_price_up = 0.0
        fill_price_dn = 0.0
        if details_up and isinstance(details_up, dict):
            fill_price_up = float(details_up.get("price", price_up))
            # Some responses carry avg_price on partial fills
            if "avg_price" in details_up:
                fill_price_up = float(details_up["avg_price"]) or fill_price_up
        if details_dn and isinstance(details_dn, dict):
            fill_price_dn = float(details_dn.get("price", price_dn))
            if "avg_price" in details_dn:
                fill_price_dn = float(details_dn["avg_price"]) or fill_price_dn

        # Fall back to submitted price if response is zero
        if fill_price_up <= 0:
            fill_price_up = price_up
        if fill_price_dn <= 0:
            fill_price_dn = price_dn

        # ── Assess outcome ────────────────────────────────────────────────
        if filled_up and filled_dn:
            actual_combined = fill_price_up + fill_price_dn
            # Gross profit: $1 payout - combined cost, per share (up side determines pairs)
            gross_per_pair = 1.0 - actual_combined
            # Fee estimate (per leg, applied to USD outlay)
            fee_up = fill_price_up * shares_up * DN_FEE_ESTIMATE
            fee_dn = fill_price_dn * shares_dn * DN_FEE_ESTIMATE
            gross_total = gross_per_pair * min(shares_up, shares_dn)
            net_profit = gross_total - fee_up - fee_dn

            return ArbResult(
                timestamp=now_str,
                ask_up=ask_up, ask_dn=ask_dn,
                combined=actual_combined, spread=1.0 - actual_combined,
                shares_up=shares_up, shares_dn=shares_dn,
                fill_price_up=fill_price_up, fill_price_dn=fill_price_dn,
                status="FILLED",
                net_profit=net_profit,
                note=(
                    f"Both legs filled. Gross/pair=${gross_per_pair:.4f} "
                    f"fees=${fee_up+fee_dn:.4f} net=${net_profit:.4f}"
                ),
            )

        # ── One or both legs failed — attempt mitigation ──────────────────
        if filled_up and not filled_dn:
            # DN failed: cancel UP if still open, then try a market sell on UP
            # to unwind. We use a low limit price to guarantee a fill.
            self._cancel_order(order_id_dn)
            self._print(
                f"   {Y}{B}[DN Hunter]{X} {Y}DN leg did not fill ({status_dn}). "
                f"Attempting UP leg unwind...{X}"
            )
            self._unwind_leg(token_up, shares_up)
            return ArbResult(
                timestamp=now_str, ask_up=ask_up, ask_dn=ask_dn,
                combined=combined, spread=spread,
                shares_up=shares_up, shares_dn=0,
                fill_price_up=fill_price_up, fill_price_dn=0.0,
                status="PARTIAL",
                net_profit=0.0,
                note=f"UP filled, DN {status_dn}. UP unwind attempted.",
            )

        if filled_dn and not filled_up:
            self._cancel_order(order_id_up)
            self._print(
                f"   {Y}{B}[DN Hunter]{X} {Y}UP leg did not fill ({status_up}). "
                f"Attempting DN leg unwind...{X}"
            )
            self._unwind_leg(token_dn, shares_dn)
            return ArbResult(
                timestamp=now_str, ask_up=ask_up, ask_dn=ask_dn,
                combined=combined, spread=spread,
                shares_up=0, shares_dn=shares_dn,
                fill_price_up=0.0, fill_price_dn=fill_price_dn,
                status="PARTIAL",
                net_profit=0.0,
                note=f"DN filled, UP {status_up}. DN unwind attempted.",
            )

        # Both failed / timed out / cancelled
        self._cancel_order(order_id_up)
        self._cancel_order(order_id_dn)
        return ArbResult(
            timestamp=now_str, ask_up=ask_up, ask_dn=ask_dn,
            combined=combined, spread=spread,
            shares_up=0, shares_dn=0,
            fill_price_up=0.0, fill_price_dn=0.0,
            status="FAILED",
            net_profit=0.0,
            note=f"Both legs failed: UP={status_up} DN={status_dn}.",
        )

    def _unwind_leg(self, token_id: str, shares: float) -> None:
        """
        Best-effort market sell of a single filled leg to neutralise a partial arb.
        Uses a very aggressive low limit price to maximise fill probability.
        Logs the attempt but never raises — caller continues regardless.
        """
        UNWIND_PRICE = 0.01   # floor — guarantees fill at any bid above dust
        try:
            tick_size = self._client.get_tick_size(token_id)
            neg_risk  = self._client.get_neg_risk(token_id)
            order = self._client.create_order(
                OrderArgs(token_id=token_id, price=UNWIND_PRICE, size=shares, side="SELL"),
                options=PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk),
            )
            resp = self._client.post_order(order, orderType=OrderType.GTC)
            order_id = None
            if isinstance(resp, dict):
                order_id = resp.get("orderID") or resp.get("id")
            if order_id:
                # Poll briefly — unwind doesn't need to be perfect, just attempted
                status, _ = self._poll_order_fill(order_id)
                if status == "FILLED":
                    self._print(
                        f"   {G}[DN Hunter] Unwind filled for {shares:.2f}sh @ ≤${UNWIND_PRICE:.2f}{X}"
                    )
                else:
                    self._print(
                        f"   {Y}[DN Hunter] Unwind {status} for {shares:.2f}sh — "
                        f"check open orders manually.{X}"
                    )
            else:
                self._print(
                    f"   {R}[DN Hunter] Unwind submit returned no order ID for {shares:.2f}sh.{X}"
                )
        except Exception as e:
            logger.debug("[DN Hunter] _unwind_leg error: %s", e)
            self._print(
                f"   {R}[DN Hunter] Unwind exception: {e} — check open orders manually.{X}"
            )


# ── Module-level helper for UI panel ──────────────────────────────────────────

def hunter_status_str(hunter: "DeltaNeutralHunter | None") -> str:
    """
    Returns a compact one-line status string suitable for embedding in draw_panel.
    Safe to call from the main thread at any time.

    Examples:
        "◆ DN scanning  $0.9923  0 arbs"
        "◆ DN firing!   $0.9841  1 arbs  +$0.12"
        "◆ DN paused"
        "◆ DN disabled"
    """
    if hunter is None or not DN_ENABLED:
        return f"{D}◆ DN disabled{X}"

    status = hunter.status
    combined = hunter.last_combined
    count = hunter.session_arb_count
    pnl = hunter.session_arb_pnl

    if status == "stopped":
        return f"{D}◆ DN stopped{X}"
    if status == "paused":
        return f"{Y}◆ DN paused{X}"
    if status == "disabled":
        return f"{D}◆ DN disabled{X}"
    if status == "firing":
        combined_str = f" {C}{B}${combined:.4f}{X}" if combined > 0 else ""
        return f"{M}{B}◆ DN FIRING!{X}{combined_str} {G}{count} arbs{X}"

    # scanning / idle
    if combined > 0:
        spread = 1.0 - combined
        if combined < DN_THRESHOLD:
            comb_color = G
        elif combined < DN_THRESHOLD + 0.01:
            comb_color = Y
        else:
            comb_color = D
        combined_str = f" {comb_color}${combined:.4f}{X}"
    else:
        combined_str = ""

    pnl_str = ""
    if count > 0:
        pnl_color = G if pnl >= 0 else R
        pnl_str = f" {pnl_color}{'+' if pnl >= 0 else ''}${pnl:.2f}{X}"

    return (
        f"{M}◆ DN scanning{X}{combined_str} "
        f"{D}{count} arbs{X}{pnl_str}"
    )
