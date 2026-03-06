import logging
import time
import os
import threading
from datetime import datetime, timezone
import requests

logger = logging.getLogger(__name__)
GAMMA = "https://gamma-api.polymarket.com"

class AutoClaimer:
    def __init__(self, radar_logger, session, get_price_fn):
        self.radar_logger = radar_logger
        self.session = session
        self.get_price_fn = get_price_fn
        self.pending_claims = []
        self._lock = threading.Lock()
        self._thread = None
        self._stop_event = threading.Event()
        self._print_lock = threading.Lock()

    def add_pending_positions(self, market_slug, positions):
        """Add positions to be monitored for resolution and claim."""
        with self._lock:
            # We group by market_slug
            self.pending_claims.append({
                "market_slug": market_slug,
                "positions": positions,
                "added_at": time.time(),
                "last_checked": 0
            })

    def _print(self, msg):
        with self._print_lock:
            print(msg)

    def start(self):
        if self._thread is None or not self._thread.is_alive():
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run, daemon=True, name="AutoClaimer")
            self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def _run(self):
        while not self._stop_event.is_set():
            self._check_resolutions()
            # Sleep for a bit before checking again (UMA resolution takes time)
            # Sleep in chunks to allow fast stop
            for _ in range(30):
                if self._stop_event.is_set():
                    break
                time.sleep(1)

    def _check_resolutions(self):
        with self._lock:
            # Copy to avoid mutation during iteration
            pending = list(self.pending_claims)

        for claim_group in pending:
            market_slug = claim_group["market_slug"]

            # Don't spam the API, check once every 30 seconds max
            if time.time() - claim_group["last_checked"] < 30:
                continue

            claim_group["last_checked"] = time.time()

            try:
                r = requests.get(f"{GAMMA}/events", params={"slug": market_slug}, timeout=10)
                if r.status_code == 200 and r.json():
                    ev = r.json()[0]
                    markets = ev.get("markets", [])
                    if markets:
                        m = markets[0]
                        # Check if the market is closed and resolved
                        is_resolved = m.get("closed", False) or m.get("resolved", False)

                        if is_resolved:
                            self._process_resolved_market(m, claim_group)

            except Exception as e:
                logger.debug(f"Error checking resolution for {market_slug}: {e}")

    def _process_resolved_market(self, market_data, claim_group):
        tokens = market_data.get("tokens", [])
        winning_token_id = None

        for token in tokens:
            # The winning token usually has winner: true
            if token.get("winner") == True:
                winning_token_id = token.get("token_id")
                break

        if not winning_token_id:
            # If not explicitly marked winner, check groupItemVal or price
            # Polymarket gamma sometimes sets price to 1 for the winner after resolution
            for token in tokens:
                if token.get("price") == 1.0:
                    winning_token_id = token.get("token_id")
                    break

        if not winning_token_id:
            logger.debug(f"Market {market_data.get('question')} is resolved but winner unknown yet.")
            return

        # We found the winner!
        market_slug = claim_group["market_slug"]
        positions = claim_group["positions"]

        # Format the UI message
        from colors import G, R, Y, B, X, M

        total_pnl_added = 0.0
        shares_claimed = 0.0

        for p in positions:
            token_up = market_data.get("tokens", [{}])[0].get("token_id")
            token_down = market_data.get("tokens", [{}, {}])[1].get("token_id") if len(market_data.get("tokens", [])) > 1 else None

            is_winner = False
            if p['direction'] == 'up' and winning_token_id == token_up:
                is_winner = True
            elif p['direction'] == 'down' and winning_token_id == token_down:
                is_winner = True

            if is_winner:
                # Winning shares redeem for exactly $1.00 USDC
                pnl = (1.00 - p['price']) * p['shares']
                shares_claimed += p['shares']
                total_pnl_added += pnl

                # Update global session stats safely
                self.session.session_pnl += pnl
                # We don't increment trade_count because it was already incremented at BUY
                self.session.trade_history.append(pnl)

                # Log the claim to CSV!
                self.radar_logger.log_trade(
                    "CLAIM", p['direction'], p['shares'], 1.00,
                    p['shares'] * 1.00, "market_resolved", pnl, self.session.session_pnl
                )
            else:
                # Losing shares redeem for exactly $0.00 USDC (100% loss)
                pnl = (0.00 - p['price']) * p['shares']
                self.session.session_pnl += pnl
                self.session.trade_history.append(pnl)

                self.radar_logger.log_trade(
                    "LOSS", p['direction'], p['shares'], 0.00,
                    0.00, "market_resolved", pnl, self.session.session_pnl
                )

        if shares_claimed > 0:
            self._print(f"\n   {M}{B}💰 [AUTO-CLAIM]{X} {G}Market {market_slug} resolved!{X}")
            self._print(f"   {M}{B}💰 [AUTO-CLAIM]{X} {G}Claimed {shares_claimed:.2f} winning shares for $1.00 each.{X}")
            self._print(f"   {M}{B}💰 [AUTO-CLAIM]{X} {G}Session P&L updated by +${total_pnl_added:.2f}{X}")

            # Execute actual on-chain transaction if possible
            self._execute_on_chain_claim(market_data.get("conditionId"))
        else:
            self._print(f"\n   {M}{B}💰 [AUTO-CLAIM]{X} {Y}Market {market_slug} resolved, but held shares were losers ($0.00).{X}")

        # Remove from pending queue
        with self._lock:
            if claim_group in self.pending_claims:
                self.pending_claims.remove(claim_group)

    def _execute_on_chain_claim(self, condition_id):
        from colors import Y, D, X
        if not condition_id:
            return

        builder_key = os.getenv("BUILDER_API_KEY")
        if not builder_key:
            self._print(f"   {D}Note: Cannot submit gasless on-chain claim without BUILDER_API_KEY.{X}")
            self._print(f"   {D}You will need to manually click 'Claim' on Polymarket.com, but{X}")
            self._print(f"   {D}your local Session P&L has been accurately updated!{X}")
            return

        try:
            from py_builder_relayer_client.client import RelayClient
            from py_builder_signing_sdk.config import BuilderConfig
            from py_builder_signing_sdk.sdk_types import BuilderApiKeyCreds

            creds = BuilderApiKeyCreds(
                key=os.getenv("BUILDER_API_KEY", ""),
                secret=os.getenv("BUILDER_SECRET", ""),
                passphrase=os.getenv("BUILDER_PASSPHRASE", "")
            )
            builder_config = BuilderConfig(local_builder_creds=creds)
            pk = os.getenv("POLYMARKET_API_KEY", "")
            if pk.startswith("0x"): pk = pk[2:]

            # Use SAFE tx type which is required for Polymarket proxy wallets
            relayer_client = RelayClient(
                "https://relayer-v2.polymarket.com/",
                137,
                pk,
                builder_config
            )

            # We must encode the redeemPositions call manually
            from web3 import Web3
            w3 = Web3()
            CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
            USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
            PARENT_ID = "0x" + "0" * 64

            # ABI for redeemPositions(address collateralToken, bytes32 parentCollectionId, bytes32 conditionId, uint256[] indexSets)
            # The selector is 0xdbcf3720
            # A bit complex to encode manually without eth-abi, so we'll use web3's contract
            abi = '[{"inputs":[{"internalType":"contract IERC20","name":"collateralToken","type":"address"},{"internalType":"bytes32","name":"parentCollectionId","type":"bytes32"},{"internalType":"bytes32","name":"conditionId","type":"bytes32"},{"internalType":"uint256[]","name":"indexSets","type":"uint256[]"}],"name":"redeemPositions","outputs":[],"stateMutability":"nonpayable","type":"function"}]'
            contract = w3.eth.contract(address=CTF_ADDRESS, abi=abi)

            # Ensure condition_id is strictly a bytes32 hex string
            if not condition_id.startswith("0x"):
                condition_id = "0x" + condition_id

            tx_data = contract.encodeABI(fn_name="redeemPositions", args=[
                USDC_E,
                Web3.to_bytes(hexstr=PARENT_ID),
                Web3.to_bytes(hexstr=condition_id),
                [1, 2] # indexSets for standard binary markets (YES/NO)
            ])

            redeem_tx = {
                "to": CTF_ADDRESS,
                "data": tx_data,
                "value": "0"
            }

            response = relayer_client.execute([redeem_tx], "Redeem Winnings")
            self._print(f"   {Y}Sent on-chain claim via Relayer!{X}")

        except ImportError:
            self._print(f"   {D}Note: py-builder-relayer-client not installed.{X}")
            self._print(f"   {D}Run `pip install py-builder-relayer-client` for on-chain auto-claiming.{X}")
        except Exception as e:
            self._print(f"   {D}Note: On-chain auto-claim failed: {e}{X}")
            self._print(f"   {D}You may need to claim manually on Polymarket.com.{X}")
