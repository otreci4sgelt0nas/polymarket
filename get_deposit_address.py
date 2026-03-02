#!/usr/bin/env python3
"""
get_deposit_address.py — Polymarket deposit address, Polygon & BSC balances.

Reads POLYMARKET_API_KEY from .env, derives your EOA and Polymarket proxy
wallet addresses, then checks balances on both Polygon and BNB Smart Chain.

Usage:
    python get_deposit_address.py

┌─────────────────────────────────────────────────────────────┐
│  Polymarket runs on POLYGON only.                           │
│  If your funds are on BSC, you must bridge them first.      │
│  See the bridging guide printed at the end of this script.  │
└─────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── path setup so we can import from src/ ────────────────────────────────────
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from dotenv import load_dotenv
from eth_account import Account
from web3 import Web3

# ── ANSI colours ──────────────────────────────────────────────────────────────
RESET  = "\033[0m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
WHITE  = "\033[97m"
RED    = "\033[91m"
BLUE   = "\033[94m"
MAGENTA = "\033[95m"

# ── Polymarket CLOB ───────────────────────────────────────────────────────────
CLOB = "https://clob.polymarket.com"

# ── Polymarket Proxy Wallet Factory (Polygon mainnet) ─────────────────────────
PROXY_FACTORY        = "0xaB45c5A4B0c941a2F231C04C3f49182e1A254052"
PROXY_INIT_CODE_HASH = bytes.fromhex(
    "d21df8dc65880a8606f09fe0ce3df9b8869287ab0b058be05aa9e8af6330a00b"
)

# ── Polygon ───────────────────────────────────────────────────────────────────
POLYGON_CHAIN_ID = 137
POLYGON_RPCS = [
    "https://rpc.ankr.com/polygon",
    "https://polygon-mainnet.public.blastapi.io",
    "https://polygon-bor-rpc.publicnode.com",
]
# USDC on Polygon (6 decimals)
POLYGON_USDC_NATIVE  = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"  # native USDC
POLYGON_USDC_BRIDGED = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # USDC.e

# ── BNB Smart Chain ───────────────────────────────────────────────────────────
BSC_CHAIN_ID = 56
BSC_RPCS = [
    "https://rpc.ankr.com/bsc",
    "https://bsc-rpc.publicnode.com",
    "https://bsc-dataseed1.binance.org",
    "https://bsc-dataseed2.binance.org",
]
# Both tokens use 18 decimals on BSC
BSC_USDC = "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d"  # Binance-Peg USDC  (18 dec)
BSC_USDT = "0x55d398326f99059fF775485246999027B3197955"  # Binance-Peg USDT  (18 dec)

# ── Minimal ERC-20 ABI ────────────────────────────────────────────────────────
ERC20_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function",
    }
]

# ─────────────────────────────────────────────────────────────────────────────
# Address derivation
# ─────────────────────────────────────────────────────────────────────────────

def derive_proxy_address(eoa_address: str) -> str:
    """
    Compute the Polymarket proxy wallet address for a given EOA via CREATE2.

    The proxy is a minimal smart contract deployed by Polymarket's factory
    on behalf of the EOA.  It holds your USDC on Polygon and is the address
    the trading bot signs orders with as 'funder'.
    """
    eoa_bytes = bytes.fromhex(eoa_address.lower().replace("0x", ""))
    salt = Web3.keccak(eoa_bytes)
    raw  = Web3.keccak(
        b"\xff"
        + bytes.fromhex(PROXY_FACTORY.lower().replace("0x", ""))
        + salt
        + PROXY_INIT_CODE_HASH
    )[12:]
    return Web3.to_checksum_address(raw)


# ─────────────────────────────────────────────────────────────────────────────
# RPC helpers
# ─────────────────────────────────────────────────────────────────────────────

def _connect(rpc_list: list[str]) -> Web3 | None:
    """Return the first responsive Web3 instance from a list of RPC URLs."""
    for url in rpc_list:
        try:
            w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 8}))
            if w3.is_connected():
                return w3
        except Exception:
            continue
    return None


def _erc20_balance(w3: Web3, contract_address: str, wallet: str,
                   decimals: int) -> float:
    """Return token balance for wallet, scaled by decimals."""
    token = w3.eth.contract(
        address=Web3.to_checksum_address(contract_address),
        abi=ERC20_ABI,
    )
    raw = token.functions.balanceOf(Web3.to_checksum_address(wallet)).call()
    return raw / (10 ** decimals)


def _native_balance(w3: Web3, wallet: str) -> float:
    """Return native coin balance (MATIC or BNB) in ether units."""
    wei = w3.eth.get_balance(Web3.to_checksum_address(wallet))
    return float(w3.from_wei(wei, "ether"))


# ─────────────────────────────────────────────────────────────────────────────
# Polygon balance fetchers
# ─────────────────────────────────────────────────────────────────────────────

def fetch_polygon_balances(proxy_address: str) -> dict:
    """
    Fetch MATIC + USDC (native & bridged) from the Polygon proxy wallet.
    Returns a dict with keys: matic, usdc_native, usdc_bridged.
    """
    result = {"matic": 0.0, "usdc_native": 0.0, "usdc_bridged": 0.0, "error": None}
    w3 = _connect(POLYGON_RPCS)
    if w3 is None:
        result["error"] = "Could not connect to any Polygon RPC"
        return result
    try:
        result["matic"] = _native_balance(w3, proxy_address)
    except Exception as e:
        result["error"] = f"MATIC: {e}"

    for key, addr, dec in [
        ("usdc_native",  POLYGON_USDC_NATIVE,  6),
        ("usdc_bridged", POLYGON_USDC_BRIDGED, 6),
    ]:
        try:
            result[key] = _erc20_balance(w3, addr, proxy_address, dec)
        except Exception as e:
            result["error"] = f"{key}: {e}"

    return result


def fetch_clob_balance(proxy_address: str, private_key: str) -> float | None:
    """
    Fetch tradeable USDC balance from the Polymarket CLOB API.
    This is the balance the bot actually reads when placing orders.
    """
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import AssetType, BalanceAllowanceParams
        from py_clob_client.constants import POLYGON

        key    = private_key.lstrip("0x") if private_key.startswith("0x") else private_key
        client = ClobClient(
            CLOB,
            key=key,
            chain_id=POLYGON,
            signature_type=1,
            funder=proxy_address,
        )
        creds = client.create_or_derive_api_creds()
        if not creds:
            return None
        client.set_api_creds(creds)

        resp = client.get_balance_allowance(
            params=BalanceAllowanceParams(
                asset_type=AssetType.COLLATERAL,
                signature_type=1,
            )
        )
        return float(resp.get("balance", 0)) / 1e6
    except Exception as exc:
        print(f"  {YELLOW}⚠  CLOB API: {exc}{RESET}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# BSC balance fetchers
# ─────────────────────────────────────────────────────────────────────────────

def fetch_bsc_balances(eoa_address: str) -> dict:
    """
    Fetch BNB + USDC + USDT from the EOA on BNB Smart Chain.
    Note: on BSC the EOA address is identical to Polygon (same private key,
    both EVM chains).  USDC and USDT on BSC use 18 decimals.
    Returns a dict with keys: bnb, usdc, usdt, error.
    """
    result = {"bnb": 0.0, "usdc": 0.0, "usdt": 0.0, "error": None}
    w3 = _connect(BSC_RPCS)
    if w3 is None:
        result["error"] = "Could not connect to any BSC RPC"
        return result

    try:
        result["bnb"] = _native_balance(w3, eoa_address)
    except Exception as e:
        result["error"] = f"BNB: {e}"

    for key, addr in [("usdc", BSC_USDC), ("usdt", BSC_USDT)]:
        try:
            result[key] = _erc20_balance(w3, addr, eoa_address, decimals=18)
        except Exception as e:
            result["error"] = f"{key}: {e}"

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Formatting helpers
# ─────────────────────────────────────────────────────────────────────────────

def sep(char: str = "─", width: int = 64) -> str:
    return DIM + char * width + RESET


def usd_str(v: float, symbol: str = "USDC") -> str:
    colour = GREEN if v > 0 else DIM
    return f"{colour}${v:,.2f} {symbol}{RESET}"


def coin_str(v: float, symbol: str, warn_below: float = 0.01) -> str:
    colour = GREEN if v >= warn_below else RED
    return f"{colour}{v:.4f} {symbol}{RESET}"


def label(text: str, width: int = 24) -> str:
    return f"{WHITE}{text:<{width}}{RESET}"


def bscscan(address: str) -> str:
    return f"https://bscscan.com/address/{address}"


def polygonscan(address: str) -> str:
    return f"https://polygonscan.com/address/{address}"


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    load_dotenv()

    raw_key = os.getenv("POLYMARKET_API_KEY", "").strip()
    if not raw_key:
        print(f"\n{RED}✗  POLYMARKET_API_KEY not found in .env{RESET}")
        print(f"   Add your private key:  {CYAN}POLYMARKET_API_KEY=0x…{RESET}\n")
        sys.exit(1)

    full_key = raw_key if raw_key.startswith("0x") else f"0x{raw_key}"

    # ── derive addresses ──────────────────────────────────────────────────────
    try:
        eoa_address   = Account.from_key(full_key).address
        proxy_address = derive_proxy_address(eoa_address)
    except Exception as exc:
        print(f"\n{RED}✗  Could not derive addresses: {exc}{RESET}")
        print("   Make sure POLYMARKET_API_KEY is a valid 32-byte private key.\n")
        sys.exit(1)

    # ── fetch all balances concurrently ───────────────────────────────────────
    print(f"\n  {DIM}Fetching balances…{RESET}", flush=True)
    with ThreadPoolExecutor(max_workers=3) as pool:
        fut_clob    = pool.submit(fetch_clob_balance,    proxy_address, full_key)
        fut_polygon = pool.submit(fetch_polygon_balances, proxy_address)
        fut_bsc     = pool.submit(fetch_bsc_balances,     eoa_address)

        clob_balance = fut_clob.result()
        poly_bals    = fut_polygon.result()
        bsc_bals     = fut_bsc.result()

    # ═════════════════════════════════════════════════════════════════════════
    # HEADER
    # ═════════════════════════════════════════════════════════════════════════
    print()
    print(sep("═"))
    print(f"  {BOLD}{WHITE}Polymarket — Wallet Addresses & Balances{RESET}")
    print(sep("═"))

    # ─────────────────────────────────────────────────────────────────────────
    # ADDRESSES
    # ─────────────────────────────────────────────────────────────────────────
    print(f"\n  {BOLD}Addresses{RESET}")
    print(sep())
    print(f"  {DIM}{'TYPE':<26}{'ADDRESS'}{RESET}")
    print(sep())

    print(f"  {'EOA  (Polygon & BSC)':<26}{CYAN}{eoa_address}{RESET}")
    print(f"  {DIM}{'':26}Same address on every EVM chain — derived from your private key{RESET}")
    print()
    print(f"  {'Proxy wallet  (Polygon)':<26}{GREEN}{BOLD}{proxy_address}{RESET}")
    print(f"  {DIM}{'':26}Polymarket smart-contract wallet — holds your USDC for trading{RESET}")

    # ─────────────────────────────────────────────────────────────────────────
    # EXPLORER LINKS
    # ─────────────────────────────────────────────────────────────────────────
    print()
    print(f"\n  {BOLD}Explorer links{RESET}")
    print(sep())
    print(f"  {DIM}Polygon — EOA   {RESET}{BLUE}{polygonscan(eoa_address)}{RESET}")
    print(f"  {DIM}Polygon — Proxy {RESET}{BLUE}{polygonscan(proxy_address)}{RESET}")
    print(f"  {DIM}BSC     — EOA   {RESET}{YELLOW}{bscscan(eoa_address)}{RESET}")

    # ═════════════════════════════════════════════════════════════════════════
    # POLYGON BALANCES  (proxy wallet — what the bot uses)
    # ═════════════════════════════════════════════════════════════════════════
    print()
    print(sep("═"))
    print(f"  {BOLD}Polygon balances{RESET}  {DIM}(proxy wallet — {proxy_address[:10]}…){RESET}")
    print(sep("═"))

    if poly_bals.get("error") and poly_bals["matic"] == 0.0:
        print(f"  {YELLOW}⚠  {poly_bals['error']}{RESET}")
    else:
        if poly_bals.get("error"):
            print(f"  {YELLOW}⚠  Partial error: {poly_bals['error']}{RESET}")

        # CLOB (tradeable) — most important number for the bot
        if clob_balance is not None:
            clob_col = GREEN if clob_balance > 0 else YELLOW
            print(f"  {label('CLOB (tradeable)')}{clob_col}{BOLD}${clob_balance:,.2f} USDC{RESET}  "
                  f"{DIM}← what the bot sees{RESET}")
        else:
            print(f"  {label('CLOB (tradeable)')}{YELLOW}unavailable{RESET}")

        print(f"  {label('On-chain USDC')}{usd_str(poly_bals['usdc_native'])}"
              f"  {DIM}({POLYGON_USDC_NATIVE[:10]}…){RESET}")
        print(f"  {label('On-chain USDC.e')}{usd_str(poly_bals['usdc_bridged'])}"
              f"  {DIM}({POLYGON_USDC_BRIDGED[:10]}…){RESET}")

        # ── warn if on-chain USDC exists but hasn't been approved into Polymarket ──
        onchain_total = poly_bals["usdc_native"] + poly_bals["usdc_bridged"]
        clob_zero     = clob_balance is None or clob_balance == 0.0
        if onchain_total > 0 and clob_zero:
            print()
            print(f"  {YELLOW}{BOLD}⚠  Funds not yet active in Polymarket!{RESET}")
            print(f"  {YELLOW}   ${onchain_total:.2f} USDC is on-chain in the proxy wallet but the CLOB{RESET}")
            print(f"  {YELLOW}   balance is $0.00 — the bot cannot trade with it yet.{RESET}")
            print(f"  {YELLOW}   You need to approve/deposit via the Polymarket UI:{RESET}")
            print(f"  {BLUE}   https://polymarket.com/portfolio{RESET}")
            print(f"  {DIM}   (Profile → Deposit → confirm the on-chain allowance tx){RESET}")

        matic      = poly_bals["matic"]
        matic_warn = f"  {RED}⚠  Too low — deposit at least 0.1 MATIC for gas{RESET}" \
                     if matic < 0.01 else ""
        print(f"  {label('MATIC (gas)')}{coin_str(matic, 'MATIC', warn_below=0.01)}{matic_warn}")

    # ═════════════════════════════════════════════════════════════════════════
    # BSC BALANCES  (EOA — same address as Polygon)
    # ═════════════════════════════════════════════════════════════════════════
    print()
    print(sep("═"))
    print(f"  {BOLD}{YELLOW}BSC balances{RESET}  {DIM}(EOA — {eoa_address[:10]}…  |  chain ID {BSC_CHAIN_ID}){RESET}")
    print(sep("═"))

    if bsc_bals.get("error") and bsc_bals["bnb"] == 0.0:
        print(f"  {YELLOW}⚠  {bsc_bals['error']}{RESET}")
    else:
        if bsc_bals.get("error"):
            print(f"  {YELLOW}⚠  Partial error: {bsc_bals['error']}{RESET}")

        bnb      = bsc_bals["bnb"]
        bnb_warn = f"  {RED}⚠  Low BNB — keep some for BSC gas{RESET}" \
                   if 0 < bnb < 0.005 else ""
        print(f"  {label('BNB (gas)')}{coin_str(bnb, 'BNB', warn_below=0.005)}{bnb_warn}")
        print(f"  {label('USDC (BSC)')}{usd_str(bsc_bals['usdc'])}"
              f"  {DIM}18 dec · {BSC_USDC[:10]}…{RESET}")
        print(f"  {label('USDT (BSC)')}{usd_str(bsc_bals['usdt'], 'USDT')}"
              f"  {DIM}18 dec · {BSC_USDT[:10]}…{RESET}")

    # ═════════════════════════════════════════════════════════════════════════
    # HOW TO DEPOSIT TO POLYMARKET
    # ═════════════════════════════════════════════════════════════════════════
    print()
    print(sep("═"))
    print(f"  {BOLD}How to deposit to Polymarket{RESET}")
    print(sep("═"))
    print(f"""
  {BOLD}Option A — You already have USDC on Polygon{RESET}
  ─────────────────────────────────────────────
  Send USDC to your {GREEN}proxy wallet{RESET} on the Polygon network:

    Network  →  {WHITE}Polygon (MATIC mainnet, chain ID 137){RESET}
    Token    →  {WHITE}USDC{RESET}  {DIM}(native: {POLYGON_USDC_NATIVE}){RESET}
    Address  →  {GREEN}{BOLD}{proxy_address}{RESET}

  {YELLOW}⚠  Keep ≥ 0.1 MATIC in the proxy wallet for gas.{RESET}


  {BOLD}Option B — Your funds are on BSC (BNB chain){RESET}
  ────────────────────────────────────────────
  You need to bridge USDC/USDT from BSC → Polygon first.
  Your {CYAN}EOA address is the same on both chains{RESET}:

    {CYAN}{eoa_address}{RESET}

  Recommended bridges  (cross-chain USDC transfer):

    1. {WHITE}Stargate Finance{RESET}  {DIM}https://stargate.finance{RESET}
       Best option — direct USDC BSC → Polygon, low fees, powered by LayerZero.
       Select:  From {YELLOW}BNB Chain{RESET}  →  To {MAGENTA}Polygon{RESET}  →  Asset {WHITE}USDC{RESET}

    2. {WHITE}Celer cBridge{RESET}     {DIM}https://cbridge.celer.network{RESET}
       Supports USDC and USDT BSC → Polygon.

    3. {WHITE}Binance Exchange{RESET}  {DIM}(easiest if you have a Binance account){RESET}
       Deposit USDC/USDT on BSC → Withdraw to Polygon network
       directly to:  {GREEN}{proxy_address}{RESET}

  After bridging, your USDC lands on Polygon at your {CYAN}EOA{RESET}.
  From there, deposit into the proxy wallet via the Polymarket UI
  (app.polymarket.com → Profile → Deposit), or send directly
  to the {GREEN}proxy address{RESET} shown above.
""")
    print(sep("═"))


if __name__ == "__main__":
    main()
