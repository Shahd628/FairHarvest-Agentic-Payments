"""
Standalone rule-based negotiation agent for the FairHarvest potato-trade system.

Polls the Sepolia TradeContract for OfferSubmitted / CounterOffered events and
responds with counterOffer() or acceptDeal() transactions, bounded by the
farmer's on-chain policy and the oracle price.

Required environment variables (see .env.example):
    INFURA_TOKEN, WALLET_PRIVATE_KEY, FARMER_ADDRESS,
    TRADE_CONTRACT_ADDRESS, ORACLE_CONTRACT_ADDRESS

Optional:
    POLL_INTERVAL   — seconds between block polls (default 30)
    TARGET_RATIO    — agent's opening counter as % of oracle price (default 95)
"""

import logging
import os
import time
from dataclasses import dataclass
from enum import IntEnum

from dotenv import load_dotenv
from web3 import Web3, HTTPProvider
from web3.exceptions import ContractLogicError

load_dotenv()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SEPOLIA_CHAIN_ID: int = 11_155_111
MAX_PRICE_DEVIATION: float = 0.20   # mirrors oracle contract's 20% single-step cap
NEGOTIATION_TIMEOUT: int = 7_200    # seconds — contract also enforces this via revert
LOG_LOOKBACK_BLOCKS: int = 10_000   # ~33 hours at 12s/block; used for oracle event scan
EVENT_CHUNK_SIZE: int = 2_000       # max blocks per eth_getLogs call (Infura limit)
GAS_BUFFER_RATIO: float = 1.20      # multiplied against estimated gas

DEFAULT_TARGET_RATIO: int = 95
DEFAULT_POLL_INTERVAL: int = 30

# ---------------------------------------------------------------------------
# ABIs  (minimal — update signatures once contracts are compiled and deployed)
# ---------------------------------------------------------------------------

TRADE_ABI = [
    # --- state readers ---
    {
        "inputs": [{"name": "farmerAddress", "type": "address"}],
        "name": "policies",
        "outputs": [
            {"name": "minPriceRatio", "type": "uint256"},
            {"name": "maxDealSize", "type": "uint256"},
            {"name": "maxRounds", "type": "uint256"},
            {"name": "isRegistered", "type": "bool"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "dealId", "type": "uint256"}],
        "name": "deals",
        "outputs": [
            {"name": "farmer", "type": "address"},
            {"name": "buyer", "type": "address"},
            {"name": "agreedPrice", "type": "uint256"},
            {"name": "quantity", "type": "uint256"},
            {"name": "dealTimestamp", "type": "uint256"},
            {"name": "escrowAmount", "type": "uint256"},
            {"name": "round", "type": "uint8"},
            {"name": "state", "type": "uint8"},
            {"name": "trackingUrl", "type": "string"},
            {"name": "farmerSigned", "type": "bool"},
            {"name": "buyerSigned", "type": "bool"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "farmer", "type": "address"},
            {"name": "buyer", "type": "address"},
        ],
        "name": "farmerBlacklist",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "buyer", "type": "address"}],
        "name": "platformBlacklist",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function",
    },
    # --- writes ---
    {
        "inputs": [
            {"name": "dealId", "type": "uint256"},
            {"name": "price", "type": "uint256"},
        ],
        "name": "counterOffer",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"name": "dealId", "type": "uint256"}],
        "name": "acceptDeal",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    # --- events ---
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "dealId", "type": "uint256"},
            {"indexed": True, "name": "buyer", "type": "address"},
            {"indexed": True, "name": "farmer", "type": "address"},
            {"indexed": False, "name": "price", "type": "uint256"},
            {"indexed": False, "name": "quantity", "type": "uint256"},
        ],
        "name": "OfferSubmitted",
        "type": "event",
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "dealId", "type": "uint256"},
            {"indexed": True, "name": "caller", "type": "address"},
            {"indexed": False, "name": "price", "type": "uint256"},
            {"indexed": False, "name": "round", "type": "uint8"},
        ],
        "name": "CounterOffered",
        "type": "event",
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "dealId", "type": "uint256"},
            {"indexed": True, "name": "agent", "type": "address"},
            {"indexed": False, "name": "agreedPrice", "type": "uint256"},
        ],
        "name": "DealAccepted",
        "type": "event",
    },
]

ORACLE_ABI = [
    {
        "inputs": [],
        "name": "getPrice",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "lastUpdated",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": False, "name": "oldPrice", "type": "uint256"},
            {"indexed": False, "name": "newPrice", "type": "uint256"},
            {"indexed": False, "name": "timestamp", "type": "uint256"},
        ],
        "name": "PriceUpdated",
        "type": "event",
    },
]

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


class DealState(IntEnum):
    NEGOTIATING = 0
    FUNDED = 1
    SHIPPED = 2
    DISPUTED = 3
    COMPLETE = 4
    REFUNDED = 5


@dataclass
class FarmerPolicy:
    min_price_ratio: int    # e.g. 90 → floor is 90% of oracle price
    max_deal_size: int      # max kg per deal
    max_rounds: int
    is_registered: bool


@dataclass
class Deal:
    farmer: str
    buyer: str
    agreed_price: int
    quantity: int
    deal_timestamp: int     # set on acceptDeal(); used as negotiation-start proxy
    escrow_amount: int
    round: int
    state: DealState
    tracking_url: str
    farmer_signed: bool
    buyer_signed: bool


# ---------------------------------------------------------------------------
# OracleClient
# ---------------------------------------------------------------------------


class OracleClient:
    """Thin wrapper around the on-chain oracle contract."""

    def __init__(self, w3: Web3, contract) -> None:
        self._w3 = w3
        self._contract = contract

    def get_price(self) -> int:
        """Call getPrice() on the oracle contract.

        Returns:
            Current commodity price (oracle precision units).

        Raises:
            ContractLogicError: If the oracle price is stale (oracle enforces
                a 2-hour freshness requirement internally).
        """
        return self._contract.functions.getPrice().call()

    def verify_deviation(self, current_price: int) -> bool:
        """Verify current_price is within MAX_PRICE_DEVIATION of the previous update.

        Reads the most recent PriceUpdated event rather than trusting the stored
        value alone, providing an independent cross-check against the oracle
        contract's own 20% cap enforcement.

        Args:
            current_price: The price just returned by get_price().

        Returns:
            True if deviation is within the allowed cap or history is
            insufficient to check. False if the cap appears violated.
        """
        latest_block = self._w3.eth.block_number
        from_block = max(0, latest_block - LOG_LOOKBACK_BLOCKS)

        logs = self._contract.events.PriceUpdated.get_logs(fromBlock=from_block)
        if not logs:
            logger.warning("No PriceUpdated events found in lookback window — skipping deviation check.")
            return True

        last = logs[-1]
        old_price = last["args"]["oldPrice"]
        if old_price == 0:
            return True

        deviation = abs(current_price - old_price) / old_price
        if deviation > MAX_PRICE_DEVIATION:
            logger.error(
                f"Oracle deviation {deviation:.1%} exceeds {MAX_PRICE_DEVIATION:.0%} cap "
                f"(old={old_price}, current={current_price}) — possible oracle manipulation."
            )
            return False

        return True


# ---------------------------------------------------------------------------
# NegotiationAgent
# ---------------------------------------------------------------------------


class NegotiationAgent:
    """Rule-based agent that negotiates potato-buying deals on behalf of a farmer.

    Polls the TradeContract for new OfferSubmitted and CounterOffered events,
    checks the buyer against blacklists, reads the farmer's on-chain policy and
    the oracle price, then submits counterOffer() or acceptDeal() transactions.

    Decision rules (evaluated in order per incoming offer/counter):
        1. Walk away if deal_round >= policy.max_rounds.
        2. Accept if offer_price >= floor_price (minPriceRatio % of oracle).
        3. Counter at max(target_price, floor_price), where target_price is
           TARGET_RATIO % of oracle.
    """

    def __init__(
        self,
        w3: Web3,
        trade_contract,
        oracle: OracleClient,
        wallet,
        farmer_addr: str,
        target_ratio: int,
    ) -> None:
        """Initialize the negotiation agent.

        Args:
            w3: Connected Web3 instance (Sepolia).
            trade_contract: Instantiated TradeContract web3 object.
            oracle: Initialized OracleClient.
            wallet: web3 Account object for signing transactions.
            farmer_addr: Checksummed address of the farmer whose deals to manage.
            target_ratio: Agent's opening counter as % of oracle price (e.g. 95).
        """
        self._w3 = w3
        self._trade = trade_contract
        self._oracle = oracle
        self._wallet = wallet
        self._farmer_addr = farmer_addr
        self._target_ratio = target_ratio
        # deal_id → block timestamp of the original OfferSubmitted event.
        # Lets the agent pre-check the 2h negotiation timeout before spending gas.
        self._offer_timestamps: dict[int, int] = {}

    # ── Event polling ────────────────────────────────────────────────────────

    def poll_events(self, from_block: int, to_block: int) -> list:
        """Fetch OfferSubmitted and CounterOffered events in the given block range.

        Queries in chunks of EVENT_CHUNK_SIZE to respect Infura's eth_getLogs
        limit. OfferSubmitted is filtered server-side by the farmer's address.
        Results are sorted by block number ascending.

        Args:
            from_block: First block to include (inclusive).
            to_block: Last block to include (inclusive).

        Returns:
            List of event log objects sorted by blockNumber ascending.
        """
        all_events: list = []
        chunk_start = from_block

        while chunk_start <= to_block:
            chunk_end = min(chunk_start + EVENT_CHUNK_SIZE - 1, to_block)

            offer_logs = self._trade.events.OfferSubmitted.get_logs(
                fromBlock=chunk_start,
                toBlock=chunk_end,
                argument_filters={"farmer": self._farmer_addr},
            )
            counter_logs = self._trade.events.CounterOffered.get_logs(
                fromBlock=chunk_start,
                toBlock=chunk_end,
            )
            all_events.extend(offer_logs)
            all_events.extend(counter_logs)
            chunk_start = chunk_end + 1

        all_events.sort(key=lambda e: e["blockNumber"])
        return all_events

    # ── On-chain reads ───────────────────────────────────────────────────────

    def check_blacklists(self, buyer_addr: str) -> bool:
        """Return True if buyer_addr is on the farmer-level or platform-level blacklist.

        The spec calls for a three-layer check: agent-level (this method),
        then on-chain farmer blacklist, then on-chain platform blacklist.
        The on-chain checks are the hard guarantee; this check saves gas by
        preventing the agent from sending a transaction that would revert.

        Args:
            buyer_addr: Checksummed buyer wallet address.

        Returns:
            True if the buyer is blacklisted (offer should be silently ignored).
            False if the buyer is clean.
        """
        if self._trade.functions.farmerBlacklist(self._farmer_addr, buyer_addr).call():
            logger.warning(f"Buyer {buyer_addr} is on farmer blacklist — ignoring.")
            return True

        if self._trade.functions.platformBlacklist(buyer_addr).call():
            logger.warning(f"Buyer {buyer_addr} is on platform blacklist — ignoring.")
            return True

        return False

    def get_policy(self) -> FarmerPolicy:
        """Fetch the farmer's on-chain policy struct.

        Returns:
            FarmerPolicy populated from contract state.

        Raises:
            ValueError: If the farmer has not yet called registerPolicy().
        """
        result = self._trade.functions.policies(self._farmer_addr).call()
        policy = FarmerPolicy(
            min_price_ratio=result[0],
            max_deal_size=result[1],
            max_rounds=result[2],
            is_registered=result[3],
        )
        if not policy.is_registered:
            raise ValueError(f"Farmer {self._farmer_addr} has not registered a policy.")
        return policy

    def get_deal(self, deal_id: int) -> Deal:
        """Fetch a deal struct by ID.

        Args:
            deal_id: On-chain deal identifier.

        Returns:
            Deal populated from contract state.
        """
        r = self._trade.functions.deals(deal_id).call()
        return Deal(
            farmer=r[0],
            buyer=r[1],
            agreed_price=r[2],
            quantity=r[3],
            deal_timestamp=r[4],
            escrow_amount=r[5],
            round=r[6],
            state=DealState(r[7]),
            tracking_url=r[8],
            farmer_signed=r[9],
            buyer_signed=r[10],
        )

    # ── Decision logic ───────────────────────────────────────────────────────

    def decide(
        self,
        deal_id: int,
        offer_price: int,
        deal_round: int,
        policy: FarmerPolicy,
        oracle_price: int,
    ) -> tuple[str, int]:
        """Apply rule-based decision for a given offer price and negotiation state.

        Args:
            deal_id: On-chain deal identifier (used only for logging).
            offer_price: Buyer's current proposed price.
            deal_round: Current negotiation round number.
            policy: Farmer's on-chain policy.
            oracle_price: Latest verified oracle price.

        Returns:
            Tuple of (action, price) where:
                action is "accept", "counter", or "walk_away".
                price is the counter value for "counter"; 0 otherwise.
        """
        floor_price = policy.min_price_ratio * oracle_price // 100
        target_price = max(self._target_ratio * oracle_price // 100, floor_price)

        logger.info(
            f"[deal {deal_id}] round={deal_round} offer={offer_price} "
            f"floor={floor_price} target={target_price} oracle={oracle_price}"
        )

        if deal_round >= policy.max_rounds:
            logger.info(f"[deal {deal_id}] Round limit reached — walking away.")
            return ("walk_away", 0)

        if offer_price >= floor_price:
            logger.info(f"[deal {deal_id}] Offer meets floor — accepting at {offer_price}.")
            return ("accept", 0)

        logger.info(f"[deal {deal_id}] Below floor — countering at {target_price}.")
        return ("counter", target_price)

    # ── Transaction helpers ──────────────────────────────────────────────────

    def _build_and_send(self, fn_call) -> str:
        """Build, sign, and broadcast a contract function call.

        Estimates gas and adds a GAS_BUFFER_RATIO cushion to reduce the risk
        of out-of-gas reverts. Waits for the receipt and raises on revert.

        Args:
            fn_call: A web3 contract function object ready for .build_transaction().

        Returns:
            Transaction hash as a hex string.

        Raises:
            ContractLogicError: If the transaction reverts on-chain.
        """
        nonce = self._w3.eth.get_transaction_count(self._wallet.address)
        gas_estimate = fn_call.estimate_gas({"from": self._wallet.address})

        tx = fn_call.build_transaction({
            "from": self._wallet.address,
            "nonce": nonce,
            "gas": int(gas_estimate * GAS_BUFFER_RATIO),
            "gasPrice": self._w3.eth.gas_price,
            "chainId": SEPOLIA_CHAIN_ID,
        })
        signed = self._w3.eth.account.sign_transaction(tx, private_key=self._wallet.key)
        tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction)
        logger.info(f"Tx sent: {tx_hash.hex()} — awaiting confirmation...")

        receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash)
        if receipt["status"] != 1:
            raise ContractLogicError(f"Transaction reverted: {tx_hash.hex()}")

        logger.info(
            f"Confirmed in block {receipt['blockNumber']} "
            f"(gas used: {receipt['gasUsed']})"
        )
        return tx_hash.hex()

    def send_counter(self, deal_id: int, price: int) -> str:
        """Sign and send counterOffer(dealId, price).

        Args:
            deal_id: On-chain deal identifier.
            price: Counter-offer price in oracle precision units.

        Returns:
            Transaction hash as a hex string.
        """
        logger.info(f"[deal {deal_id}] Sending counterOffer at {price}.")
        return self._build_and_send(self._trade.functions.counterOffer(deal_id, price))

    def send_accept(self, deal_id: int) -> str:
        """Sign and send acceptDeal(dealId).

        Args:
            deal_id: On-chain deal identifier.

        Returns:
            Transaction hash as a hex string.
        """
        logger.info(f"[deal {deal_id}] Sending acceptDeal.")
        return self._build_and_send(self._trade.functions.acceptDeal(deal_id))

    # ── Event handlers ───────────────────────────────────────────────────────

    def _handle_offer_submitted(
        self, event, policy: FarmerPolicy, oracle_price: int
    ) -> None:
        """Process an OfferSubmitted event.

        Records the block timestamp for later timeout checks, validates the
        buyer against both blacklists, then calls decide() and executes.

        Args:
            event: OfferSubmitted event log object.
            policy: Pre-fetched farmer policy.
            oracle_price: Pre-fetched verified oracle price.
        """
        args = event["args"]
        deal_id: int = args["dealId"]
        buyer: str = args["buyer"]
        offer_price: int = args["price"]

        block = self._w3.eth.get_block(event["blockNumber"])
        self._offer_timestamps[deal_id] = block["timestamp"]

        logger.info(
            f"[deal {deal_id}] OfferSubmitted by {buyer} "
            f"at price={offer_price} qty={args['quantity']}"
        )

        if self.check_blacklists(buyer):
            return

        action, price = self.decide(deal_id, offer_price, 1, policy, oracle_price)
        self._execute_action(deal_id, action, price)

    def _handle_counter_offered(
        self, event, policy: FarmerPolicy, oracle_price: int
    ) -> None:
        """Process a CounterOffered event, skipping the agent's own submissions.

        Skips if caller == agent wallet (our own counter reflected back).
        Skips if the deal's farmer is not the watched farmer.
        Pre-checks the 2h negotiation timeout before spending gas.

        Args:
            event: CounterOffered event log object.
            policy: Pre-fetched farmer policy.
            oracle_price: Pre-fetched verified oracle price.
        """
        args = event["args"]
        deal_id: int = args["dealId"]
        caller: str = args["caller"]
        offer_price: int = args["price"]
        round_num: int = args["round"]

        if Web3.to_checksum_address(caller) == self._wallet.address:
            return

        deal = self.get_deal(deal_id)
        if Web3.to_checksum_address(deal.farmer) != self._farmer_addr:
            return

        logger.info(
            f"[deal {deal_id}] CounterOffered by {caller} "
            f"at price={offer_price} round={round_num}"
        )

        offer_ts = self._offer_timestamps.get(deal_id)
        if offer_ts is not None:
            current_ts: int = self._w3.eth.get_block("latest")["timestamp"]
            if current_ts - offer_ts >= NEGOTIATION_TIMEOUT:
                logger.warning(
                    f"[deal {deal_id}] Negotiation timeout exceeded "
                    f"({current_ts - offer_ts}s elapsed) — walking away."
                )
                return

        action, price = self.decide(deal_id, offer_price, round_num, policy, oracle_price)
        self._execute_action(deal_id, action, price)

    def _execute_action(self, deal_id: int, action: str, price: int) -> None:
        """Execute the agent's decision by dispatching the appropriate transaction.

        Logs walk_away decisions without sending a transaction; the deal will
        expire via the contract's 2h negotiation timeout.

        Args:
            deal_id: On-chain deal identifier.
            action: One of "accept", "counter", or "walk_away".
            price: Counter price (used only when action == "counter").
        """
        if action == "walk_away":
            logger.info(f"[deal {deal_id}] Walking away — no transaction sent.")
            return
        try:
            if action == "accept":
                self.send_accept(deal_id)
            elif action == "counter":
                self.send_counter(deal_id, price)
        except ContractLogicError as exc:
            logger.error(f"[deal {deal_id}] Transaction reverted: {exc}")

    # ── Main loop ────────────────────────────────────────────────────────────

    def run(self, poll_interval: int) -> None:
        """Start the main polling loop. Runs indefinitely until interrupted.

        On each tick: reads oracle price and farmer policy (to catch on-chain
        updates), then fetches and processes all new events since the last
        processed block.

        Args:
            poll_interval: Seconds to sleep between poll cycles.
        """
        last_block = self._w3.eth.block_number
        logger.info(
            f"Agent started. Farmer={self._farmer_addr} "
            f"target_ratio={self._target_ratio}% "
            f"starting_block={last_block}"
        )

        while True:
            try:
                current_block = self._w3.eth.block_number
                if current_block <= last_block:
                    time.sleep(poll_interval)
                    continue

                policy = self.get_policy()

                try:
                    oracle_price = self._oracle.get_price()
                except ContractLogicError:
                    logger.error("Oracle price is stale — skipping cycle.")
                    last_block = current_block
                    time.sleep(poll_interval)
                    continue

                if not self._oracle.verify_deviation(oracle_price):
                    logger.error("Oracle deviation check failed — skipping cycle.")
                    last_block = current_block
                    time.sleep(poll_interval)
                    continue

                events = self.poll_events(last_block + 1, current_block)
                logger.info(
                    f"Blocks {last_block + 1}–{current_block}: "
                    f"{len(events)} event(s). Oracle={oracle_price}"
                )

                for event in events:
                    event_name = event["event"]
                    if event_name == "OfferSubmitted":
                        self._handle_offer_submitted(event, policy, oracle_price)
                    elif event_name == "CounterOffered":
                        self._handle_counter_offered(event, policy, oracle_price)

                last_block = current_block

            except KeyboardInterrupt:
                logger.info("Shutdown requested — agent stopping.")
                break
            except Exception as exc:
                logger.exception(f"Unexpected error in poll loop: {exc}")
                time.sleep(poll_interval)


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


def _load_config() -> dict:
    """Load and validate required environment variables from .env.

    Returns:
        Dict of validated, type-coerced configuration values.

    Raises:
        EnvironmentError: If any required variable is missing.
    """
    required = [
        "INFURA_TOKEN",
        "WALLET_PRIVATE_KEY",
        "FARMER_ADDRESS",
        "TRADE_CONTRACT_ADDRESS",
        "ORACLE_CONTRACT_ADDRESS",
    ]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise EnvironmentError(f"Missing required env vars: {', '.join(missing)}")

    return {
        "infura_token": os.environ["INFURA_TOKEN"],
        "private_key": os.environ["WALLET_PRIVATE_KEY"],
        "farmer_addr": Web3.to_checksum_address(os.environ["FARMER_ADDRESS"]),
        "trade_addr": Web3.to_checksum_address(os.environ["TRADE_CONTRACT_ADDRESS"]),
        "oracle_addr": Web3.to_checksum_address(os.environ["ORACLE_CONTRACT_ADDRESS"]),
        "poll_interval": int(os.environ.get("POLL_INTERVAL", DEFAULT_POLL_INTERVAL)),
        "target_ratio": int(os.environ.get("TARGET_RATIO", DEFAULT_TARGET_RATIO)),
    }


def main() -> None:
    """Entry point. Loads config, connects to Sepolia, wires contracts, starts agent.

    Raises:
        EnvironmentError: If required env vars are missing.
        ConnectionError: If the Infura RPC connection fails.
    """
    config = _load_config()

    rpc_url = f"https://sepolia.infura.io/v3/{config['infura_token']}"
    w3 = Web3(HTTPProvider(rpc_url))
    if not w3.is_connected():
        raise ConnectionError("Failed to connect to Sepolia via Infura.")

    wallet = w3.eth.account.from_key(config["private_key"])
    balance = w3.eth.get_balance(wallet.address)
    logger.info(
        f"Agent wallet: {wallet.address} "
        f"({Web3.from_wei(balance, 'ether'):.6f} ETH)"
    )

    trade_contract = w3.eth.contract(address=config["trade_addr"], abi=TRADE_ABI)
    oracle_contract = w3.eth.contract(address=config["oracle_addr"], abi=ORACLE_ABI)

    oracle = OracleClient(w3, oracle_contract)
    agent = NegotiationAgent(
        w3=w3,
        trade_contract=trade_contract,
        oracle=oracle,
        wallet=wallet,
        farmer_addr=config["farmer_addr"],
        target_ratio=config["target_ratio"],
    )
    agent.run(poll_interval=config["poll_interval"])


if __name__ == "__main__":
    main()
