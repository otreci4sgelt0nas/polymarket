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
import requests
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
DN_MAX_COMBINED_PRICE = float(os.getenv("DN_MAX_COMBINED_PRICE", "0.985"))
_dn_stake_env = os.getenv("DN_STAKE", "")
DN_STAKE         = float(_dn_stake_env) if _dn_stake_env else float(os.getenv("TRADE_AMOUNT", "4"))
DN_MAX_DAILY     = int(os.getenv("DN_MAX_DAILY", "5"))
DN_SCAN_INTERVAL = float(os.getenv("DN_SCAN_INTERVAL", "1.0"))
DN_ORDER_TIMEOUT = int(os.getenv("DN_ORDER_TIMEOUT", "30"))
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

            # ── Fetch both order books concurrently ───────────────────────
            try:
                fut_up = self._executor.submit(self._fetch_order_book, token_up)
                fut_dn = self._executor.submit(self._fetch_order_book, token_dn)
                asks_up = fut_up.result(timeout=5)
                asks_dn = fut_dn.result(timeout=5)
                consecutive_errors = 0
            except (FuturesTimeout, Exception) as e:
                consecutive_errors += 1
                logger.debug("[DN Hunter] order book fetch error (streak=%d): %s", consecutive_errors, e)
                if consecutive_errors >= 5:
                    self._print(
                        f"   {M}{B}[DN Hunter]{X} {Y}⚠ {consecutive_errors} consecutive book errors — "
                        f"sleeping 10s{X}"
                    )
                    time.sleep(10)
                else:
                    time.sleep(DN_SCAN_INTERVAL)
                continue

            if not asks_up or not asks_dn:
                time.sleep(DN_SCAN_INTERVAL)
                continue

            best_ask_up = float(asks_up[0]["price"])
            best_ask_dn = float(asks_dn[0]["price"])
            combined = best_ask_up + best_ask_dn
            self.last_combined = combined
            self.status = "scanning"

            # ── Opportunity? Quick top-of-book check ──────────────────────
            if combined >= DN_THRESHOLD:
                time.sleep(DN_SCAN_INTERVAL)
                continue

            # Double-check: re-fetch immediately to filter stale quotes
            try:
                fut_up2 = self._executor.submit(self._fetch_order_book, token_up)
                fut_dn2 = self._executor.submit(self._fetch_order_book, token_dn)
                asks_up2 = fut_up2.result(timeout=5)
                asks_dn2 = fut_dn2.result(timeout=5)
            except Exception as e:
                logger.debug("[DN Hunter] confirmation fetch failed: %s", e)
                time.sleep(DN_SCAN_INTERVAL)
                continue

            if not asks_up2 or not asks_dn2:
                time.sleep(DN_SCAN_INTERVAL)
                continue

            best_ask_up2 = float(asks_up2[0]["price"])
            best_ask_dn2 = float(asks_dn2[0]["price"])
            combined2 = best_ask_up2 + best_ask_dn2
            worst_combined_top = max(combined, combined2)

            if worst_combined_top >= DN_THRESHOLD:
                logger.debug(
                    "[DN Hunter] Opportunity evaporated on re-check: %.4f → %.4f",
                    combined, combined2,
                )
                time.sleep(DN_SCAN_INTERVAL)
                continue

            # ── Liquidity-Aware Sizing ────────────────────────────────────
            # Use the most recent order books
            asks_up_use = asks_up2
            asks_dn_use = asks_dn2

            current_balance = float('inf')
            if self._get_balance and self._client:
                try:
                    current_balance = self._get_balance(self._client)
                except Exception as e:
                    logger.debug("[DN Hunter] balance fetch error: %s", e)

            budget_usd = min(self.stake_amount * 2.0, current_balance - 0.05)
            if budget_usd < 0:
                budget_usd = 0
            # Rough target shares before VWAP limit
            budget_shares = budget_usd / worst_combined_top if worst_combined_top > 0 else 0

            safe_shares, vwap_up, vwap_dn, limit_up, limit_dn = self._calculate_safe_shares(
                asks_up_use, asks_dn_use, budget_shares, DN_MAX_COMBINED_PRICE
            )

            combined_vwap = vwap_up + vwap_dn
            spread = 1.0 - combined_vwap

            if safe_shares < DN_MIN_SHARES:
                now_str = datetime.now().strftime("%H:%M:%S")
                self._print(
                    f"
   {M}{B}{'═' * 55}{X}
"
                    f"   {M}{B}  DELTA-NEUTRAL ARB ◆ {now_str}{X}
"
                    f"   {Y}  FAILED: Insufficient Liquidity{X}
"
                    f"   {W}  Can only safely buy {safe_shares:.2f}sh (min {DN_MIN_SHARES}){X}
"
                    f"   {W}  Top combined: ${worst_combined_top:.4f} → VWAP ceiling: ${DN_MAX_COMBINED_PRICE:.4f}{X}
"
                    f"   {M}{B}{'═' * 55}{X}"
                )
                time.sleep(DN_SCAN_INTERVAL)
                continue

            now_str = datetime.now().strftime("%H:%M:%S")
            self._print(
                f"
   {M}{B}{'═' * 55}{X}
"
                f"   {M}{B}  DELTA-NEUTRAL ARB ◆ {now_str}{X}
"
                f"   {W}  Safe Shares: {C}{B}{safe_shares:.2f}sh{X}
"
                f"   {W}  VWAP UP: {G}${vwap_up:.4f}{X}  VWAP DN: {R}${vwap_dn:.4f}{X}
"
                f"   {W}  Limit UP: {G}${limit_up:.4f}{X} Limit DN: {R}${limit_dn:.4f}{X}
"
                f"   {W}  Combined VWAP: {C}{B}${combined_vwap:.4f}{X}  "
                f"Spread: {G}{B}+${spread:.4f} ({spread*100:.2f}%){X}
"
                f"   {M}{B}{'═' * 55}{X}"
            )

            # ── Execute both legs ─────────────────────────────────────────
            self.status = "firing"

            result = self._execute_arb(token_up, token_dn, limit_up, limit_dn, safe_shares,
                                       combined_vwap, spread, now_str)
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
        last_order = None
        while time.time() < deadline:
            # We explicitly DO NOT abort on self._stop_event.is_set() here!
            # If an order is already submitted, we must monitor it to completion
            # so we can properly unwind excess shares and log the result even during shutdown.
            try:
                order = self._client.get_order(order_id)
                if isinstance(order, dict):
                    last_order = order
                    status = order.get("status", "UNKNOWN")
                    if status in ("MATCHED", "FILLED"):
                        return "FILLED", order
                    if status in ("CANCELED", "CANCELLED"):
                        return "CANCELLED", order
            except Exception as e:
                logger.debug("[DN Hunter] poll_order %s: %s", order_id[:8], e)
            time.sleep(1)
        return "TIMEOUT", last_order

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
        limit_up: float,
        limit_dn: float,
        shares: float,
        combined_vwap: float,
        spread: float,
        now_str: str,
    ) -> ArbResult:
        """
        Submit both legs simultaneously, poll for fills, cancel if either fails.
        """
        MAX_PRICE = 0.99

        price_up = limit_up
        price_dn = limit_dn

        shares_up = shares
        shares_dn = shares

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
        filled_shares_up = shares_up if filled_up else 0.0
        filled_shares_dn = shares_dn if filled_dn else 0.0

        if details_up and isinstance(details_up, dict):
            fill_price_up = float(details_up.get("price", price_up))
            if "avg_price" in details_up:
                fill_price_up = float(details_up["avg_price"]) or fill_price_up
            sm = details_up.get("size_matched") or details_up.get("sizeMatched")
            if sm is not None:
                filled_shares_up = float(sm)

        if details_dn and isinstance(details_dn, dict):
            fill_price_dn = float(details_dn.get("price", price_dn))
            if "avg_price" in details_dn:
                fill_price_dn = float(details_dn["avg_price"]) or fill_price_dn
            sm = details_dn.get("size_matched") or details_dn.get("sizeMatched")
            if sm is not None:
                filled_shares_dn = float(sm)

        # Fall back to submitted price if response is zero
        if fill_price_up <= 0:
            fill_price_up = price_up
        if fill_price_dn <= 0:
            fill_price_dn = price_dn

        # ── Assess outcome ────────────────────────────────────────────────
        if filled_shares_up >= shares_up and filled_shares_dn >= shares_dn:
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
        if filled_shares_up > 0 and filled_shares_dn == 0:
            # DN failed: cancel UP if still open, then try a market sell on UP
            # to unwind. We use a low limit price to guarantee a fill.
            self._cancel_order(order_id_up)
            self._cancel_order(order_id_dn)
            self._print(
                f"   {Y}{B}[DN Hunter]{X} {Y}DN leg did not fill ({status_dn}). "
                f"Attempting UP leg unwind of {filled_shares_up:.2f}sh...{X}"
            )
            unwind_pnl = self._unwind_leg(token_up, filled_shares_up, fill_price_up)
            return ArbResult(
                timestamp=now_str, ask_up=ask_up, ask_dn=ask_dn,
                combined=combined, spread=spread,
                shares_up=filled_shares_up, shares_dn=0,
                fill_price_up=fill_price_up, fill_price_dn=0.0,
                status="PARTIAL",
                net_profit=unwind_pnl,
                note=f"UP filled {filled_shares_up:.2f}sh, DN {status_dn}. UP unwind attempted. Unwind P&L: ${unwind_pnl:.2f}",
            )

        if filled_shares_dn > 0 and filled_shares_up == 0:
            self._cancel_order(order_id_up)
            self._cancel_order(order_id_dn)
            self._print(
                f"   {Y}{B}[DN Hunter]{X} {Y}UP leg did not fill ({status_up}). "
                f"Attempting DN leg unwind of {filled_shares_dn:.2f}sh...{X}"
            )
            unwind_pnl = self._unwind_leg(token_dn, filled_shares_dn, fill_price_dn)
            return ArbResult(
                timestamp=now_str, ask_up=ask_up, ask_dn=ask_dn,
                combined=combined, spread=spread,
                shares_up=0, shares_dn=filled_shares_dn,
                fill_price_up=0.0, fill_price_dn=fill_price_dn,
                status="PARTIAL",
                net_profit=unwind_pnl,
                note=f"DN filled {filled_shares_dn:.2f}sh, UP {status_up}. DN unwind attempted. Unwind P&L: ${unwind_pnl:.2f}",
            )

        if filled_shares_up > 0 and filled_shares_dn > 0 and (filled_shares_up < shares_up or filled_shares_dn < shares_dn):
            # Both filled partially but unevenly. We keep the matched pairs and unwind the rest.
            self._cancel_order(order_id_up)
            self._cancel_order(order_id_dn)
            matched_pairs = min(filled_shares_up, filled_shares_dn)
            unwind_up = filled_shares_up - matched_pairs
            unwind_dn = filled_shares_dn - matched_pairs
            unwind_pnl_up = 0.0
            unwind_pnl_dn = 0.0

            if unwind_up > 0:
                self._print(f"   {Y}{B}[DN Hunter]{X} {Y}Imbalanced fill. Unwinding {unwind_up:.2f} excess UP sh...{X}")
                unwind_pnl_up = self._unwind_leg(token_up, unwind_up, fill_price_up)
            if unwind_dn > 0:
                self._print(f"   {Y}{B}[DN Hunter]{X} {Y}Imbalanced fill. Unwinding {unwind_dn:.2f} excess DN sh...{X}")
                unwind_pnl_dn = self._unwind_leg(token_dn, unwind_dn, fill_price_dn)

            actual_combined = fill_price_up + fill_price_dn
            gross_per_pair = 1.0 - actual_combined
            fee_up = fill_price_up * matched_pairs * DN_FEE_ESTIMATE
            fee_dn = fill_price_dn * matched_pairs * DN_FEE_ESTIMATE
            pair_profit = (gross_per_pair * matched_pairs) - fee_up - fee_dn
            total_net = pair_profit + unwind_pnl_up + unwind_pnl_dn

            return ArbResult(
                timestamp=now_str, ask_up=ask_up, ask_dn=ask_dn,
                combined=actual_combined, spread=1.0 - actual_combined,
                shares_up=matched_pairs, shares_dn=matched_pairs,
                fill_price_up=fill_price_up, fill_price_dn=fill_price_dn,
                status="FILLED" if matched_pairs > 0 else "PARTIAL",
                net_profit=total_net,
                note=f"Imbalanced fill (UP: {filled_shares_up:.2f}, DN: {filled_shares_dn:.2f}). Kept {matched_pairs:.2f} pairs, unwound excess. Net: ${total_net:.4f}",
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

    def _unwind_leg(self, token_id: str, shares: float, entry_price: float) -> float:
        """
        Best-effort market sell of a single filled leg to neutralise a partial arb.
        Uses a very aggressive low limit price to maximise fill probability.
        Logs the attempt but never raises — caller continues regardless.
        Returns the net profit (or loss) of the unwind.
        """
        if shares <= 0:
            return 0.0

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
                # Poll briefly
                status, details = self._poll_order_fill(order_id)
                if status == "FILLED":
                    exit_price = UNWIND_PRICE
                    if details and isinstance(details, dict) and "avg_price" in details:
                        exit_price = float(details["avg_price"]) or exit_price
                    pnl = (exit_price - entry_price) * shares
                    self._print(
                        f"   {G}[DN Hunter] Unwind filled for {shares:.2f}sh @ ${exit_price:.4f} "
                        f"(P&L: ${pnl:.4f}){X}"
                    )
                    return pnl
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

        # If we reach here, we failed to confirm a fill. Assume worst-case fill at 0.01 for P&L tracking
        # to ensure safety.
        return (UNWIND_PRICE - entry_price) * shares


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

    def _fetch_order_book(self, token_id: str) -> list[dict]:
        try:
            resp = requests.get(f"{CLOB}/book", params={"token_id": token_id}, timeout=5)
            data = resp.json()
            return data.get("asks", [])
        except Exception as e:
            logger.debug("[DN Hunter] _fetch_order_book error for %s: %s", token_id[:8], e)
            return []

    def _calculate_safe_shares(self, asks_up: list[dict], asks_dn: list[dict], budget_shares: float, max_combined_price: float) -> tuple[float, float, float, float, float]:
        """
        Traverses both order books to find the maximum shares we can buy
        such that the VWAP_up + VWAP_dn <= max_combined_price.
        Returns: (safe_shares, vwap_up, vwap_dn, limit_up, limit_dn)
        """
        if not asks_up or not asks_dn:
            return 0.0, 0.0, 0.0, 0.0, 0.0

        idx_up, idx_dn = 0, 0
        shares_accumulated = 0.0
        cost_up_accumulated = 0.0
        cost_dn_accumulated = 0.0

        limit_up = 0.0
        limit_dn = 0.0

        rem_up_size = float(asks_up[0]["size"])
        rem_up_price = float(asks_up[0]["price"])

        rem_dn_size = float(asks_dn[0]["size"])
        rem_dn_price = float(asks_dn[0]["price"])

        while True:
            take_shares = min(rem_up_size, rem_dn_size)

            new_shares = shares_accumulated + take_shares
            new_cost_up = cost_up_accumulated + (take_shares * rem_up_price)
            new_cost_dn = cost_dn_accumulated + (take_shares * rem_dn_price)

            vwap_up = new_cost_up / new_shares
            vwap_dn = new_cost_dn / new_shares
            combined_vwap = vwap_up + vwap_dn

            if combined_vwap > max_combined_price:
                P_total = rem_up_price + rem_dn_price
                C_total = cost_up_accumulated + cost_dn_accumulated
                S = shares_accumulated

                if P_total > max_combined_price:
                    x = (max_combined_price * S - C_total) / (P_total - max_combined_price)
                    if x > 0:
                        take_shares = x
                        shares_accumulated += take_shares
                        cost_up_accumulated += take_shares * rem_up_price
                        cost_dn_accumulated += take_shares * rem_dn_price
                        limit_up = rem_up_price
                        limit_dn = rem_dn_price
                break

            shares_accumulated = new_shares
            cost_up_accumulated = new_cost_up
            cost_dn_accumulated = new_cost_dn
            limit_up = rem_up_price
            limit_dn = rem_dn_price

            rem_up_size -= take_shares
            rem_dn_size -= take_shares

            if rem_up_size <= 1e-6:
                idx_up += 1
                if idx_up >= len(asks_up): break
                rem_up_size = float(asks_up[idx_up]["size"])
                rem_up_price = float(asks_up[idx_up]["price"])

            if rem_dn_size <= 1e-6:
                idx_dn += 1
                if idx_dn >= len(asks_dn): break
                rem_dn_size = float(asks_dn[idx_dn]["size"])
                rem_dn_price = float(asks_dn[idx_dn]["price"])

            if shares_accumulated >= budget_shares:
                break

        if shares_accumulated > budget_shares:
            # We stepped slightly over our budget, rewind exactly to budget
            excess = shares_accumulated - budget_shares
            shares_accumulated -= excess
            cost_up_accumulated -= excess * limit_up
            cost_dn_accumulated -= excess * limit_dn

        # Cap limit prices at 0.99
        limit_up = min(round(limit_up, 4), 0.99)
        limit_dn = min(round(limit_dn, 4), 0.99)

        vwap_up = cost_up_accumulated / shares_accumulated if shares_accumulated > 0 else 0
        vwap_dn = cost_dn_accumulated / shares_accumulated if shares_accumulated > 0 else 0

        return round(shares_accumulated, 2), vwap_up, vwap_dn, limit_up, limit_dn

    def _fetch_order_book(self, token_id: str) -> list[dict]:
        try:
            resp = requests.get(f"{CLOB}/book", params={"token_id": token_id}, timeout=5)
            data = resp.json()
            return data.get("asks", [])
        except Exception as e:
            logger.debug("[DN Hunter] _fetch_order_book error for %s: %s", token_id[:8], e)
            return []

    def _calculate_safe_shares(self, asks_up: list[dict], asks_dn: list[dict], budget_shares: float, max_combined_price: float) -> tuple[float, float, float, float, float]:
        """
        Traverses both order books to find the maximum shares we can buy
        such that the VWAP_up + VWAP_dn <= max_combined_price.
        Returns: (safe_shares, vwap_up, vwap_dn, limit_up, limit_dn)
        """
        if not asks_up or not asks_dn:
            return 0.0, 0.0, 0.0, 0.0, 0.0

        idx_up, idx_dn = 0, 0
        shares_accumulated = 0.0
        cost_up_accumulated = 0.0
        cost_dn_accumulated = 0.0

        limit_up = 0.0
        limit_dn = 0.0

        rem_up_size = float(asks_up[0]["size"])
        rem_up_price = float(asks_up[0]["price"])

        rem_dn_size = float(asks_dn[0]["size"])
        rem_dn_price = float(asks_dn[0]["price"])

        while True:
            take_shares = min(rem_up_size, rem_dn_size)

            new_shares = shares_accumulated + take_shares
            new_cost_up = cost_up_accumulated + (take_shares * rem_up_price)
            new_cost_dn = cost_dn_accumulated + (take_shares * rem_dn_price)

            vwap_up = new_cost_up / new_shares
            vwap_dn = new_cost_dn / new_shares
            combined_vwap = vwap_up + vwap_dn

            if combined_vwap > max_combined_price:
                P_total = rem_up_price + rem_dn_price
                C_total = cost_up_accumulated + cost_dn_accumulated
                S = shares_accumulated

                if P_total > max_combined_price:
                    x = (max_combined_price * S - C_total) / (P_total - max_combined_price)
                    if x > 0:
                        take_shares = x
                        shares_accumulated += take_shares
                        cost_up_accumulated += take_shares * rem_up_price
                        cost_dn_accumulated += take_shares * rem_dn_price
                        limit_up = rem_up_price
                        limit_dn = rem_dn_price
                break

            shares_accumulated = new_shares
            cost_up_accumulated = new_cost_up
            cost_dn_accumulated = new_cost_dn
            limit_up = rem_up_price
            limit_dn = rem_dn_price

            rem_up_size -= take_shares
            rem_dn_size -= take_shares

            if rem_up_size <= 1e-6:
                idx_up += 1
                if idx_up >= len(asks_up): break
                rem_up_size = float(asks_up[idx_up]["size"])
                rem_up_price = float(asks_up[idx_up]["price"])

            if rem_dn_size <= 1e-6:
                idx_dn += 1
                if idx_dn >= len(asks_dn): break
                rem_dn_size = float(asks_dn[idx_dn]["size"])
                rem_dn_price = float(asks_dn[idx_dn]["price"])

            if shares_accumulated >= budget_shares:
                break

        if shares_accumulated > budget_shares:
            # We stepped slightly over our budget, rewind exactly to budget
            excess = shares_accumulated - budget_shares
            shares_accumulated -= excess
            cost_up_accumulated -= excess * limit_up
            cost_dn_accumulated -= excess * limit_dn

        # Cap limit prices at 0.99
        limit_up = min(round(limit_up, 4), 0.99)
        limit_dn = min(round(limit_dn, 4), 0.99)

        vwap_up = cost_up_accumulated / shares_accumulated if shares_accumulated > 0 else 0
        vwap_dn = cost_dn_accumulated / shares_accumulated if shares_accumulated > 0 else 0

        return round(shares_accumulated, 2), vwap_up, vwap_dn, limit_up, limit_dn

