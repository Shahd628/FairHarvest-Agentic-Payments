"""
oracle_updater.py  —  FairHarvest Oracle Updater Service
=========================================================
Polls the price feed API every 1-2 hours (randomised to avoid timing patterns)
and submits updatePrice() transactions to the MockOracle contract on Sepolia.

Usage
-----
  pip install web3 requests python-dotenv
  python oracle_updater.py

Required .env
-------------
  ORACLE_ADMIN_PRIVATE_KEY=0x...   # separate key from negotiation agent
  ORACLE_CONTRACT_ADDRESS=0x...
  RPC_URL=https://sepolia.infura.io/v3/<YOUR_KEY>
  PRICE_API_URL=http://localhost:8000   # or deployed URL
  COMMODITY=potato                     # which commodity this oracle tracks
"""

from __future__ import annotations

import logging
import os
import random
import sys
import time
from typing import Optional

import requests
from dotenv import load_dotenv
from web3 import Web3
from web3.middleware import geth_poa_middleware

load_dotenv()

# ─── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("oracle_updater")

# ─── Config ───────────────────────────────────────────────────────────────────

PRIVATE_KEY        = os.environ["ORACLE_ADMIN_PRIVATE_KEY"]
CONTRACT_ADDRESS   = Web3.to_checksum_address(os.environ["ORACLE_CONTRACT_ADDRESS"])
RPC_URL            = os.environ["RPC_URL"]
PRICE_API_URL      = os.environ.get("PRICE_API_URL", "http://localhost:8000")
COMMODITY          = os.environ.get("COMMODITY", "potato").lower()

MIN_INTERVAL_SEC   = 60   # minimal to show it works normally 1 hour
MAX_INTERVAL_SEC   = 120   

# ─── ABI (only the functions we need) ────────────────────────────────────────

ORACLE_ABI = [
    {
        "inputs": [{"internalType": "uint256", "name": "newPrice", "type": "uint256"}],
        "name": "updatePrice",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "getPriceUnsafe",
        "outputs": [
            {"internalType": "uint256", "name": "price",     "type": "uint256"},
            {"internalType": "uint256", "name": "timestamp", "type": "uint256"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "secondsUntilNextUpdate",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True,  "name": "oldPrice",  "type": "uint256"},
            {"indexed": True,  "name": "newPrice",  "type": "uint256"},
            {"indexed": False, "name": "timestamp", "type": "uint256"},
        ],
        "name": "PriceUpdated",
        "type": "event",
    },
]

# ─── Web3 Setup ───────────────────────────────────────────────────────────────

w3 = Web3(Web3.HTTPProvider(RPC_URL))
w3.middleware_onion.inject(geth_poa_middleware, layer=0)  # needed for Sepolia

if not w3.is_connected():
    log.error("Cannot connect to RPC: %s", RPC_URL)
    sys.exit(1)

account  = w3.eth.account.from_key(PRIVATE_KEY)
contract = w3.eth.contract(address=CONTRACT_ADDRESS, abi=ORACLE_ABI)

log.info("Oracle updater started | account=%s | contract=%s | commodity=%s",
         account.address, CONTRACT_ADDRESS, COMMODITY)

# ─── Helpers ──────────────────────────────────────────────────────────────────

def fetch_price() -> Optional[int]:
    """Fetch the scaled price (×1e6) from the price feed API."""
    try:
        url = f"{PRICE_API_URL}/price/{COMMODITY}"
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        scaled = int(data["price_scaled"])
        log.info("Fetched price: %s TL/kg  (scaled=%d)", data["price_tl"], scaled)
        return scaled
    except Exception as exc:
        log.error("Failed to fetch price: %s", exc)
        return None


def send_update(new_price: int) -> bool:
    """Submit updatePrice() to the oracle contract. Returns True on success."""
    try:
        # Check on-chain cooldown first to avoid wasting gas
        wait = contract.functions.secondsUntilNextUpdate().call()
        if wait > 0:
            log.warning("On-chain cooldown active — %d seconds remaining. Skipping.", wait)
            return False

        nonce = w3.eth.get_transaction_count(account.address)
        gas_price = w3.eth.gas_price

        txn = contract.functions.updatePrice(new_price).build_transaction({
            "from":     account.address,
            "nonce":    nonce,
            "gasPrice": gas_price,
            "gas":      80_000,
        })

        signed = w3.eth.account.sign_transaction(txn, PRIVATE_KEY)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        log.info("Tx submitted: %s", tx_hash.hex())

        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        if receipt.status == 1:
            log.info("✅ Price updated on-chain | block=%d | gas=%d",
                     receipt.blockNumber, receipt.gasUsed)
            return True
        else:
            log.error("❌ Transaction reverted | tx=%s", tx_hash.hex())
            return False

    except Exception as exc:
        log.error("Failed to send update: %s", exc)
        return False


def log_current_on_chain_state() -> None:
    """Print the current oracle state for visibility."""
    try:
        price, ts = contract.functions.getPriceUnsafe().call()
        age_min = (time.time() - ts) / 60
        log.info("On-chain state | price=%d (%.4f TL/kg) | age=%.1f min",
                 price, price / 1_000_000, age_min)
    except Exception as exc:
        log.warning("Could not read on-chain state: %s", exc)

# ─── Main Loop ────────────────────────────────────────────────────────────────

def main() -> None:
    log_current_on_chain_state()

    while True:
        # Randomise sleep interval: normally 1–2 hours
        sleep_sec = random.uniform(MIN_INTERVAL_SEC, MAX_INTERVAL_SEC)
        log.info("Next update in %.1f minutes (%.2f hours)",
                 sleep_sec / 60, sleep_sec / 3600)
        time.sleep(sleep_sec)

        log.info("──── Oracle update cycle ────")
        new_price = fetch_price()
        if new_price is None:
            log.warning("Price fetch failed — skipping this cycle.")
            continue

        success = send_update(new_price)
        if success:
            log_current_on_chain_state()
        else:
            log.warning("Update not applied — will retry next cycle.")


if __name__ == "__main__":
    main()
