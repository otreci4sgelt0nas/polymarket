"""Trade execution: buy, sell, close positions, TP/SL monitoring."""

from __future__ import annotations

import sys
import time
import logging
from datetime import datetime


from py_clob_client.clob_types import (
    OrderArgs, PartialCreateOrderOptions, OrderType,
    AssetType, BalanceAllowanceParams,
)

from colors import G, R, Y, M, B, D, X
from input_handler import read_key_nb
from polymarket_api import get_token_position, monitor_order

logger = logging.getLogger(__name__)

# Trading execution constants
BUY_PRICE_OFFSET = 0.02       # aggressive offset above best bid for market buy
SELL_PRICE_OFFSET = 0.05      # aggressive offset below best ask for market sell
MIN_SHARES = 5                # Polymarket minimum order size
MAX_TOKEN_PRICE = 0.99        # maximum price for buy orders
MIN_TOKEN_PRICE = 0.01        # minimum price for sell orders

# Order monitoring
ORDER_MONITOR_TIMEOUT = 30    # seconds to wait for order fill
ORDER_MONITOR_INTERVAL = 2    # seconds between order status checks
CLOSE_MONITOR_TIMEOUT = 15    # seconds to wait for close order fill
TP_SL_MONITOR_TIMEOUT = 600   # seconds before TP/SL monitoring times out


def sync_positions(client, token_up, token_down, positions, get_price, dn_shares_up=0.0, dn_shares_down=0.0):
    """Sync local positions with actual on-chain balances.

    Detects shares bought/sold directly on Polymarket's web interface
    and updates the local positions list accordingly.

    Args:
        client: ClobClient instance
        token_up/token_down: token IDs for current market
        positions: list of local position dicts (mutated in place)
        get_price: callable(token_id, side) -> float
        dn_shares_up: amount of UP shares held for delta-neutral strategy
        dn_shares_down: amount of DOWN shares held for delta-neutral strategy

    Returns:
        list of (direction, shares, price, action) tuples describing changes.
        action is 'added' or 'removed'.
    """
    changes = []

    for direction, token_id in [('up', token_up), ('down', token_down)]:
        try:
            actual_shares = get_token_position(client, token_id)
        except Exception as e:
            logger.debug("sync_positions: error querying %s: %s", direction, e)
            continue

        # Subtract DN shares so they aren't falsely adopted as directional orphans
        if direction == 'up':
            actual_shares = max(0.0, actual_shares - dn_shares_up)
        else:
            actual_shares = max(0.0, actual_shares - dn_shares_down)

        # Sum shares tracked locally for this direction
        local_shares = sum(p['shares'] for p in positions if p['direction'] == direction)

        diff = actual_shares - local_shares

        if diff >= 1.0:
            # New shares detected (bought on platform) — add as recovered position
            price = get_price(token_id, "SELL")
            if price <= 0:
                price = get_price(token_id, "BUY")
            positions.append({
                'direction': direction,
                'price': price,
                'shares': diff,
                'time': datetime.now().strftime("%H:%M:%S"),
                'source': 'platform',
            })
            changes.append((direction, diff, price, 'added'))

        elif diff <= -1.0:
            # Shares were sold on platform — remove from local tracking
            to_remove = abs(diff)
            # Remove from newest positions first (LIFO)
            for p in reversed(list(positions)):
                if p['direction'] != direction or to_remove <= 0:
                    continue
                if p['shares'] <= to_remove:
                    to_remove -= p['shares']
                    changes.append((direction, p['shares'], p['price'], 'removed'))
                    positions.remove(p)
                else:
                    changes.append((direction, to_remove, p['price'], 'removed'))
                    p['shares'] -= to_remove
                    to_remove = 0

    return changes


def close_all_positions(positions, token_up, token_down, trade_logger, reason,
                        session_pnl, trade_history, get_price,
                        client=None, executor=None):
    """Close all positions on-chain and calculate P&L for each.

    Args:
        positions: list of position dicts
        token_up/token_down: token IDs
        trade_logger: RadarLogger instance
        reason: str — 'market_expired', 'emergency', 'exit', 'tp', 'sl', 'cancel'
        session_pnl: current cumulative P&L
        trade_history: list of individual trade P&L values
        get_price: callable(token_id, side) -> float
        client: ClobClient instance (required for on-chain close; skipped if None)
        executor: ThreadPoolExecutor (required for on-chain close; skipped if None)

    Returns:
        (total_pnl, count, updated_session_pnl, pnl_list)
        pnl_list: list of (direction, shares, entry_price, exit_price, pnl) per position
    """
    # Submit on-chain sell orders first (best-effort) before clearing local state
    if reason != 'market_expired' and client is not None and executor is not None and positions:
        try:
            close_msg = execute_close_market(client, token_up, token_down, get_price, executor)
            logger.debug("close_all_positions on-chain result: %s", close_msg)
        except Exception as e:
            logger.debug("close_all_positions on-chain error: %s", e)

    total_pnl = 0.0
    count = 0
    pnl_list = []

    # If it's market expired, try to pair up delta-neutral positions first
    if reason == 'market_expired':
        up_shares = sum(p['shares'] for p in positions if p['direction'] == 'up')
        down_shares = sum(p['shares'] for p in positions if p['direction'] == 'down')
        matched_pairs = min(up_shares, down_shares)
        
        for p in positions:
            if p.get('shares', 0) < 0.01:
                continue
                
            # Treat as Pending Settlement - we assume matched pairs net to $1.00 payout per pair
            # This is an approximation for logging so the UI doesn't show a 100% loss.
            # We don't realize fake losses for pending settlement.
            exit_price = p['price'] # default to breakeven if we don't know the result
            if matched_pairs > 0.01:
                # Part of a DN pair that guarantees $1 payout minus cost
                pass # Already accounted for or we can just log it at cost

            pnl = 0.0 # Pending settlement, so no realized P&L here
            
            pnl_list.append((p['direction'], p['shares'], p['price'], exit_price, pnl))
            trade_logger.log_trade("PENDING_SETTLEMENT", p['direction'], p['shares'], exit_price,
                                   p['shares'] * exit_price, reason, pnl, session_pnl)
            count += 1
            
        positions.clear()
        return total_pnl, count, session_pnl, pnl_list

    for p in positions:
        # Skip ghost positions — floating-point dust left after a TP/SL close can
        # survive as a near-zero share count, producing a tiny negative pnl that
        # inflates profit_factor (e.g. 703.28) and adds a spurious loss to the log.
        if p.get('shares', 0) < 0.01:
            logger.debug("close_all_positions: skipping ghost position %s (shares=%.4f)", p.get('direction'), p.get('shares', 0))
            continue
        token_id = token_up if p['direction'] == 'up' else token_down
        try:
            exit_price = get_price(token_id, "SELL")
        except Exception as e:
            logger.debug("Error getting exit price: %s", e)
            exit_price = 0
        pnl = (exit_price - p['price']) * p['shares'] if exit_price > 0 else 0
        total_pnl += pnl
        count += 1
        session_pnl += pnl
        trade_history.append(pnl)
        pnl_list.append((p['direction'], p['shares'], p['price'], exit_price, pnl))
        trade_logger.log_trade("CLOSE", p['direction'], p['shares'], exit_price,
                               p['shares'] * exit_price, reason, pnl, session_pnl)

    positions.clear()
    return total_pnl, count, session_pnl, pnl_list


def execute_buy_market(client, direction, amount_usd, token_up, token_down,
                       get_price, executor, quiet=False):
    """Execute aggressive market buy order."""
    token_id = token_up if direction == "up" else token_down

    base_price = get_price(token_id, "BUY")
    if base_price <= 0:
        return None, f"{R}✗ Error getting price{X}"

    price = min(base_price + BUY_PRICE_OFFSET, MAX_TOKEN_PRICE)
    shares = round(amount_usd / price, 2)
    if shares < MIN_SHARES:
        return None, f"{R}✗ Minimum {MIN_SHARES} shares (increase amount){X}"

    try:
        # Run order creation with timeout to prevent hanging on API calls
        def _submit_order():
            tick_size = client.get_tick_size(token_id)
            neg_risk = client.get_neg_risk(token_id)
            order = client.create_order(
                OrderArgs(token_id=token_id, price=price, size=shares, side="BUY"),
                options=PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk),
            )
            return client.post_order(order, orderType=OrderType.GTC)

        fut = executor.submit(_submit_order)
        resp = fut.result(timeout=15)
    except Exception as e:
        return None, f"{R}✗ Error submitting: {e}{X}"

    order_id = resp.get("orderID") or resp.get("id") if isinstance(resp, dict) else None
    if not order_id:
        return None, f"{R}✗ No order ID{X}"

    status, details = monitor_order(client, order_id, interval=ORDER_MONITOR_INTERVAL,
                                    timeout_sec=ORDER_MONITOR_TIMEOUT, quiet=quiet)

    if status == "FILLED":
        sm = float(details.get("size_matched", 0)) if details else 0
        p = float(details.get("price", 0)) if details else price
        return {'shares': sm, 'price': p}, f"{G}✓ BUY MKT {direction.upper()} | {sm:.2f} @ ${p:.4f} = ${sm * p:.2f}{X}"
    else:
        return None, f"{Y}✗ Order not filled ({status}){X}"


def execute_close_market(client, token_up, token_down, get_price, executor):
    """Close all positions."""
    results = []
    total_value = 0.0

    for _ in range(3):
        shares_up = get_token_position(client, token_up)
        shares_down = get_token_position(client, token_down)

        if shares_up < 0.01 and shares_down < 0.01:
            if results:
                return f"{G}✓ CLOSED! {', '.join(results)} | Total: ${total_value:.2f}{X}"
            # Only declare "no positions" after at least one retry, to guard against
            # API lag returning 0 on the very first query when shares actually exist.
            if _ > 0:
                return f"{G}✓ No positions{X}"
            # First iteration returned zero — sleep briefly and retry once more
            time.sleep(1)
            continue

        for token_id, shares, name in [(token_up, shares_up, "UP"), (token_down, shares_down, "DOWN")]:
            if shares < 0.01:
                continue

            base_price = get_price(token_id, "SELL")
            market_price = max(base_price - SELL_PRICE_OFFSET, MIN_TOKEN_PRICE)

            try:
                fut_bal = executor.submit(
                    client.update_balance_allowance,
                    params=BalanceAllowanceParams(
                        asset_type=AssetType.CONDITIONAL, token_id=token_id, signature_type=1,
                    )
                )
                fut_bal.result(timeout=10)
            except Exception as e:
                logger.debug("update_balance_allowance error for %s: %s", name, e)

            try:
                def _submit_sell(tid=token_id, mp=market_price, sh=shares):
                    tick_size = client.get_tick_size(tid)
                    neg_risk = client.get_neg_risk(tid)
                    order = client.create_order(
                        OrderArgs(token_id=tid, price=mp, size=sh, side="SELL"),
                        options=PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk),
                    )
                    return client.post_order(order, orderType=OrderType.GTC)

                fut = executor.submit(_submit_sell)
                resp = fut.result(timeout=15)
                order_id = resp.get("orderID") or resp.get("id") if isinstance(resp, dict) else None
                if order_id:
                    status, details = monitor_order(client, order_id, interval=1,
                                                    timeout_sec=CLOSE_MONITOR_TIMEOUT, quiet=True)
                    if status == "FILLED":
                        sm = float(details.get("size_matched", 0)) if details else 0
                        p = float(details.get("price", 0)) if details else market_price
                        value = sm * p
                        total_value += value
                        results.append(f"{name}: {sm:.2f} @ ${p:.2f} = ${value:.2f}")
            except Exception as e:
                logger.debug("Error closing %s position: %s", name, e)

        time.sleep(1)

    return f"{G}✓ {', '.join(results)} | ${total_value:.2f}{X}" if results else f"{Y}⚠ Could not close{X}"


def monitor_tp_sl(token_id, tp, sl, tp_above, sl_above, get_price, executor,
                  timeout_sec=TP_SL_MONITOR_TIMEOUT, time_remaining_fn=None):
    """Monitor price until TP, SL, manual cancel (C key), timeout, or market expiry.

    Args:
        time_remaining_fn: optional callable() -> float (minutes left in window).
                           When it returns <= 0 the monitor exits with 'EXPIRED'.
    Uses concurrent price fetch + key checking for lower latency."""
    price = 0.0
    start = time.time()
    while True:
        # Exit if the market window has closed
        if time_remaining_fn is not None:
            try:
                tr = time_remaining_fn()
                if tr <= 0:
                    return 'EXPIRED', price if price > 0 else get_price(token_id, "SELL")
            except Exception as e:
                logger.debug("monitor_tp_sl time_remaining_fn error: %s", e)

        if time.time() - start > timeout_sec:
            return 'TIMEOUT', price if price > 0 else get_price(token_id, "SELL")
        # Fetch SELL price concurrently while checking keys.
        # We use the SELL (bid) side because that is the price we actually receive
        # when closing — using BUY (ask) was always higher than the real exit value,
        # so SL comparisons fired too late or never.
        fut_price = executor.submit(get_price, token_id, "SELL")

        # Check keys while waiting for price (5 × 0.1s = 0.5s)
        for _ in range(5):
            key = read_key_nb()
            if key == 'c':
                fut_price.result()  # don't leak the future
                return 'CANCEL', price if price > 0 else get_price(token_id, "SELL")
            time.sleep(0.1)

        price = fut_price.result()
        if price <= 0:
            continue

        now = datetime.now().strftime("%H:%M:%S")

        if tp_above and price >= tp:
            return 'TP', price
        if not tp_above and price <= tp:
            return 'TP', price

        if sl_above and price >= sl:
            return 'SL', price
        if not sl_above and price <= sl:
            return 'SL', price

        dist_tp = abs(tp - price)
        dist_sl = abs(sl - price)
        bar_pos = 10 - int(dist_tp / (dist_tp + dist_sl) * 10) if (dist_tp + dist_sl) > 0 else 5
        bar = f"{G}{'█' * bar_pos}{X}{R}{'█' * (10 - bar_pos)}{X}"
        sys.stdout.write(f"\r   {D}{now}{X} | ${price:.2f} | SL ${sl:.2f} [{bar}] TP ${tp:.2f} │ {D}C=close{X}   ")
        sys.stdout.flush()


def execute_hotkey(client, direction, trade_amount, token_up, token_down,
                   get_price, executor):
    """Execute manual buy via hotkey (u/d). Returns (buy_info, error_msg)."""
    result, msg = execute_buy_market(client, direction, trade_amount, token_up, token_down,
                                     get_price, executor, quiet=True)
    exec_time = datetime.now().strftime("%H:%M:%S")

    if result:
        sys.stdout.write('\a')
        sys.stdout.flush()
        return {
            'direction': direction,
            'price': result['price'],
            'shares': result['shares'],
            'time': exec_time,
        }, None
    else:
        return None, msg


def handle_buy(client, direction, trade_amount, token_up, token_down,
               positions, balance, trade_logger, session_pnl, get_price, executor,
               reason="manual"):
    """Execute buy and update state. Returns (info, balance, last_action).

    Args:
        direction: 'up' or 'down'
        reason: 'signal' or 'manual'

    Returns:
        (info_dict_or_None, updated_balance, last_action_str)
    """
    info, error_msg = execute_hotkey(client, direction, trade_amount, token_up, token_down,
                                     get_price, executor)
    if info:
        d_color = G if direction == 'up' else R
        last_action = f"{d_color}{B}BUY {direction.upper()}{X} {info['shares']:.0f}sh @ ${info['price']:.2f} │ {D}{reason}{X}"
        positions.append(info)
        balance -= info['price'] * info['shares']
        trade_logger.log_trade("BUY", direction, info['shares'], info['price'],
                               info['shares'] * info['price'], reason, 0, session_pnl)
    else:
        last_action = f"{R}✗ BUY {direction.upper()} FAILED{X} │ {error_msg or 'unknown error'}"
    return info, balance, last_action
