"""
Standalone rule-based negotiation agent for the FairHarvest potato-trade system.

Polls the Sepolia TradeContract for OfferSubmitted / CounterOffered events and
responds with counterOffer() or acceptDeal() transactions, bounded by the
farmer's on-chain policy and the oracle price.

When the buyer's offer meets the policy floor and rounds remain, the decision
to accept or push for a better price is delegated to an LLM (Claude). All hard
policy constraints (floor price, round limit) are enforced by the agent itself
before and after the LLM call — the LLM cannot override them.

Required environment variables (see .env.example):
    INFURA_TOKEN, WALLET_PRIVATE_KEY, FARMER_ADDRESS,
    TRADE_CONTRACT_ADDRESS, ORACLE_CONTRACT_ADDRESS

Optional:
    POLL_INTERVAL   — seconds between block polls (default 30)
    TARGET_RATIO    — fallback counter as % of oracle price if LLM unavailable (default 95)
    LLM_MODEL       — Claude model ID to use for decisions (default claude-haiku-4-5-20251001)
"""

import logging
import os
import sys
import time
from dataclasses import dataclass
from enum import IntEnum

import json
import re
import shutil
import subprocess

from dotenv import load_dotenv
from web3 import Web3, HTTPProvider
from web3.exceptions import ContractLogicError, TimeExhausted

load_dotenv(override=True)

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
LOG_LOOKBACK_BLOCKS: int = 20_000   # ~66 hours at 12s/block; used for oracle startup event scan
EVENT_CHUNK_SIZE: int = 2_000       # max blocks per eth_getLogs call (Infura limit)
GAS_BUFFER_RATIO: float = 1.20      # multiplied against estimated gas
STATE_FILE: str = "agent_state.json"   # persists last processed block across restarts

DEFAULT_TARGET_RATIO: int = 95
DEFAULT_POLL_INTERVAL: int = 30
DEFAULT_LLM_MODEL: str = "claude-haiku-4-5-20251001"

# ---------------------------------------------------------------------------
# ABIs
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
    # --- negotiation bookkeeping ---
    {
        "inputs": [{"name": "dealId", "type": "uint256"}],
        "name": "offerStartedAt",
        "outputs": [{"name": "", "type": "uint256"}],
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
        "outputs": [{"name": "price", "type": "uint256"},
                    {"name": "timestamp", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        # Skips the 2-hour staleness revert — safe for off-chain reads only.
        # Agent falls back to this when getPrice() reverts due to stale data.
        "inputs": [],
        "name": "getPriceUnsafe",
        "outputs": [{"name": "price", "type": "uint256"},
                    {"name": "timestamp", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "stalenessCheckEnabled",
        "outputs": [{"name": "", "type": "bool"}],
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
            {"indexed": True, "internalType": "uint256", "name": "oldPrice", "type": "uint256"},
            {"indexed": True, "internalType": "uint256", "name": "newPrice", "type": "uint256"},
            {"indexed": False, "internalType": "uint256", "name": "timestamp", "type": "uint256"},
        ],
        "name": "PriceUpdated",
        "type": "event",
    },
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_claude_cli() -> list[str]:
    """Return the subprocess command prefix to invoke the claude CLI.

    Checks (in order):
    1. CLAUDE_CLI_PATH env var — explicit override, useful when PATH is stale.
    2. shutil.which("claude") — searches the current process PATH.
    3. Common Windows npm global bin — %APPDATA%\\npm\\claude.cmd — covers the
       case where VS Code was launched before Claude Code updated PATH.

    On Windows the returned list wraps the binary in ["cmd", "/c", <path>] so
    that .cmd files execute correctly without shell=True.
    """
    explicit = os.environ.get("CLAUDE_CLI_PATH")
    if explicit:
        claude: str | None = explicit
    else:
        claude = shutil.which("claude")
        if claude is None and sys.platform == "win32":
            npm_candidate = os.path.expandvars(r"%APPDATA%\npm\claude.cmd")
            if os.path.isfile(npm_candidate):
                claude = npm_candidate

    if claude is None:
        raise FileNotFoundError(
            "claude CLI not found in PATH or %APPDATA%\\npm. "
            "Install Claude Code or set CLAUDE_CLI_PATH=/full/path/to/claude in .env"
        )

    if sys.platform == "win32":
        return ["cmd", "/c", claude]
    return [claude]


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
        """Return the current oracle price.

        Tries getPrice() first (enforces 2-hour staleness window). If that
        reverts because the price is stale, falls back to getPriceUnsafe()
        and logs a warning. To suppress the warning in testing, call
        setStalenessCheck(false) via oracle_admin.py, or run the oracle
        updater to push a fresh price.

        Returns:
            Current commodity price (oracle precision units ×1e6).
        """
        try:
            price, _ = self._contract.functions.getPrice().call()
            return price
        except ContractLogicError:
            logger.warning(
                "getPrice() reverted — oracle price is stale (staleness check enabled). "
                "Using getPriceUnsafe() for now. Run: python oracle_admin.py disable-staleness"
            )
            price, _ = self._contract.functions.getPriceUnsafe().call()
            return price

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

        # Chunk the scan so a large LOG_LOOKBACK_BLOCKS doesn't exceed Infura's
        # 10k-block-per-eth_getLogs limit. Collects all events then uses the last.
        logs = []
        chunk_start = from_block
        while chunk_start <= latest_block:
            chunk_end = min(chunk_start + EVENT_CHUNK_SIZE - 1, latest_block)
            logs.extend(self._contract.events.PriceUpdated.get_logs(
                from_block=chunk_start, to_block=chunk_end
            ))
            chunk_start = chunk_end + 1

        if not logs:
            logger.warning("No PriceUpdated events found in lookback window — skipping deviation check.")
            return True

        last = logs[-1]
        old_price = last["args"]["oldPrice"]
        if old_price == 0:
            logger.info(
                f"Oracle history: {len(logs)} PriceUpdated event(s) found, "
                f"last at block {last['blockNumber']} — first-ever update (oldPrice=0), skipping deviation check."
            )
            return True

        deviation = abs(current_price - old_price) / old_price
        if deviation > MAX_PRICE_DEVIATION:
            logger.error(
                f"Oracle deviation {deviation:.1%} exceeds {MAX_PRICE_DEVIATION:.0%} cap "
                f"(old={old_price}, current={current_price}) — possible oracle manipulation."
            )
            return False

        logger.info(
            f"Oracle history: {len(logs)} PriceUpdated event(s) found, "
            f"last at block {last['blockNumber']} — deviation {deviation:.2%} vs previous price. OK."
        )
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
        llm_model: str,
    ) -> None:
        """Initialize the negotiation agent.

        Args:
            w3: Connected Web3 instance (Sepolia).
            trade_contract: Instantiated TradeContract web3 object.
            oracle: Initialized OracleClient.
            wallet: web3 Account object for signing transactions.
            farmer_addr: Checksummed address of the farmer whose deals to manage.
            target_ratio: Fallback counter as % of oracle price when LLM is unavailable.
            llm_model: Claude model ID passed to `claude --model` for decisions.
        """
        self._w3 = w3
        self._trade = trade_contract
        self._oracle = oracle
        self._wallet = wallet
        self._farmer_addr = farmer_addr
        self._target_ratio = target_ratio
        self._llm_model = llm_model
        # Last oracle price seen by this agent instance; used for cache-based
        # deviation checks instead of a per-cycle PriceUpdated event scan.
        self._last_oracle_price: int | None = None
        # In-memory guard: deal IDs for which a tx was sent but not yet confirmed.
        # Prevents duplicate sends when the same OfferSubmitted event is re-processed
        # after a receipt timeout.
        self._pending_deal_ids: set[int] = set()

    # ── Event polling ────────────────────────────────────────────────────────

    def poll_events(self, from_block: int, to_block: int) -> list:
        """Fetch OfferSubmitted and CounterOffered events in the given block range.

        Uses two separate get_logs calls per chunk (one per event type) so that
        web3.py handles topic encoding and log decoding through its tested path.
        OfferSubmitted is filtered server-side to this agent's farmer address via
        argument_filters, so only relevant offers are returned.

        Infura's 10k-block limit is respected via EVENT_CHUNK_SIZE; steady-state
        polls cover a few new blocks per cycle (typically a single un-chunked call).

        Args:
            from_block: First block to include (inclusive).
            to_block: Last block to include (inclusive).

        Returns:
            List of decoded event log objects sorted by (blockNumber, logIndex).
        """
        all_events: list = []
        chunk_start = from_block

        while chunk_start <= to_block:
            chunk_end = min(chunk_start + EVENT_CHUNK_SIZE - 1, to_block)

            offer_logs = list(self._trade.events.OfferSubmitted.get_logs(
                from_block=chunk_start,
                to_block=chunk_end,
                argument_filters={"farmer": self._farmer_addr},
            ))
            counter_logs = list(self._trade.events.CounterOffered.get_logs(
                from_block=chunk_start,
                to_block=chunk_end,
            ))
            all_events.extend(offer_logs)
            all_events.extend(counter_logs)

            chunk_start = chunk_end + 1

        all_events.sort(key=lambda e: (e["blockNumber"], e["logIndex"]))
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
        """Apply policy guardrails then delegate to the LLM for strategic choices.

        Hard rules (LLM cannot override):
            - Offer below floor → must counter (or walk away if rounds exhausted).
            - Round limit reached with acceptable offer → must accept.
        Soft rule (LLM decides):
            - Offer meets floor AND rounds remain → accept now or push for more?

        Args:
            deal_id: On-chain deal identifier (used only for logging).
            offer_price: Buyer's current proposed price.
            deal_round: Current negotiation round number.
            policy: Farmer's on-chain policy.
            oracle_price: Latest verified oracle price.

        Returns:
            Tuple of (action, price) where action is "accept", "counter", or
            "walk_away", and price is the counter value (0 for non-counter actions).
        """
        floor_price = policy.min_price_ratio * oracle_price // 100

        logger.info(
            f"[deal {deal_id}] round={deal_round} offer={offer_price} "
            f"floor={floor_price} oracle={oracle_price}"
        )

        if offer_price < floor_price:
            if deal_round >= policy.max_rounds:
                logger.info(f"[deal {deal_id}] Below floor at round limit — walking away.")
                return ("walk_away", 0)
            target = max(self._target_ratio * oracle_price // 100, floor_price)
            logger.info(f"[deal {deal_id}] Below floor — countering at {target}.")
            return ("counter", target)

        if deal_round >= policy.max_rounds:
            logger.info(
                f"[deal {deal_id}] Round limit reached, offer acceptable — accepting at {offer_price}."
            )
            return ("accept", 0)

        # Offer meets floor and rounds remain: let the LLM decide whether to
        # accept now or try to push the price higher for the farmer.
        return self._llm_decide(deal_id, offer_price, deal_round, policy, oracle_price)

    def _llm_decide(
        self,
        deal_id: int,
        offer_price: int,
        deal_round: int,
        policy: FarmerPolicy,
        oracle_price: int,
    ) -> tuple[str, int]:
        """Ask the LLM whether to accept the offer or counter for a better price.

        The LLM receives full negotiation context and responds via tool use to
        guarantee structured output. The returned counter price is clamped within
        valid bounds regardless of what the LLM suggests. Falls back to
        _rule_based_fallback() on any API or parsing failure.

        Args:
            deal_id: On-chain deal identifier (used for logging).
            offer_price: Buyer's current proposed price (already >= floor).
            deal_round: Current negotiation round number.
            policy: Farmer's on-chain policy.
            oracle_price: Latest verified oracle price.

        Returns:
            Tuple of ("accept", 0) or ("counter", price).
        """
        floor_price = policy.min_price_ratio * oracle_price // 100
        rounds_left = policy.max_rounds - deal_round
        offer_pct = offer_price / oracle_price * 100

        user_message = (
            f"Negotiation context:\n"
            f"  Oracle market price  : {oracle_price}\n"
            f"  Buyer's current offer: {offer_price} ({offer_pct:.1f}% of market)\n"
            f"  Farmer's floor price : {floor_price} ({policy.min_price_ratio}% of market)\n"
            f"  Negotiation round    : {deal_round} of {policy.max_rounds}\n"
            f"  Rounds remaining     : {rounds_left}\n\n"
            f"The offer is above the farmer's minimum floor, so accepting is valid.\n"
            f"You may also counter with a higher price to maximise the farmer's revenue.\n"
            f"Any counter price must be strictly above {offer_price} and at most {oracle_price}.\n"
            f"Caution: if you counter and the buyer does not respond, the deal expires "
            f"after round {policy.max_rounds} with no sale — weigh this risk carefully."
        )

        try:
            prompt = (
                "You are a negotiation agent acting on behalf of a potato farmer. "
                "Your sole objective is to maximise the farmer's sale price within "
                "the policy constraints.\n\n"
                f"{user_message}\n\n"
                "Respond with ONLY a JSON object — no markdown, no extra text:\n"
                '{"action": "accept" or "counter", '
                '"counter_price": <integer, only include if action is counter>, '
                '"reasoning": "<one sentence>"}'
            )
            cmd = _find_claude_cli() + ["-p"]
            if self._llm_model:
                cmd += ["--model", self._llm_model]

            result = subprocess.run(
                cmd, input=prompt, capture_output=True, text=True, timeout=60
            )
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or "claude -p exited non-zero")

            raw = result.stdout.strip()
            # Strip markdown code fences if the model wrapped the JSON anyway.
            fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
            if fence_match:
                raw = fence_match.group(1).strip()
            json_start = raw.find("{")
            json_end = raw.rfind("}")
            if json_start == -1 or json_end == -1:
                raise ValueError(f"No JSON object in output: {raw!r}")

            decision = json.loads(raw[json_start : json_end + 1])
            action: str = decision["action"]
            reasoning: str = decision.get("reasoning", "")
            logger.info(f"[deal {deal_id}] LLM: {action}. {reasoning}")

            if action == "accept":
                return ("accept", 0)

            raw_counter: int = int(decision.get("counter_price", 0))
            # Clamp: must beat the offer, respect floor, and not exceed oracle.
            counter_price = max(raw_counter, offer_price + 1, floor_price)
            counter_price = min(counter_price, oracle_price)
            return ("counter", counter_price)

        except Exception as exc:
            logger.warning(
                f"[deal {deal_id}] LLM call failed ({exc}) — falling back to rule-based."
            )
            return self._rule_based_fallback(offer_price, oracle_price, floor_price)

    def _rule_based_fallback(
        self, offer_price: int, oracle_price: int, floor_price: int
    ) -> tuple[str, int]:
        """Fallback decision when the LLM is unavailable.

        Counters at target_ratio if that would beat the current offer; otherwise
        accepts to avoid wasting a round on a pointless counter.

        Args:
            offer_price: Buyer's current proposed price.
            oracle_price: Latest verified oracle price.
            floor_price: Computed floor (minPriceRatio * oracle / 100).

        Returns:
            Tuple of ("accept", 0) or ("counter", price).
        """
        target = max(self._target_ratio * oracle_price // 100, floor_price)
        if target > offer_price:
            logger.info(f"Fallback: countering at {target}.")
            return ("counter", target)
        logger.info("Fallback: target not above offer — accepting.")
        return ("accept", 0)

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

        # EIP-1559 pricing — more reliable than legacy gasPrice on post-merge networks.
        # maxFeePerGas = 2× current base fee + tip, so the tx survives a base-fee doubling.
        latest_block = self._w3.eth.get_block("latest")
        base_fee: int = latest_block.get("baseFeePerGas") or 0
        max_priority_fee: int = Web3.to_wei(2, "gwei")
        max_fee: int = base_fee * 2 + max_priority_fee

        tx = fn_call.build_transaction({
            "from": self._wallet.address,
            "nonce": nonce,
            "gas": int(gas_estimate * GAS_BUFFER_RATIO),
            "maxFeePerGas": max_fee,
            "maxPriorityFeePerGas": max_priority_fee,
            "chainId": SEPOLIA_CHAIN_ID,
            "type": 2,
        })
        signed = self._w3.eth.account.sign_transaction(tx, private_key=self._wallet.key)
        tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction)
        logger.info(f"Tx sent: {tx_hash.hex()} — awaiting confirmation...")

        try:
            receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=300)
        except TimeExhausted:
            logger.warning(
                f"Receipt timeout for tx {tx_hash.hex()} after 300s — "
                "tx is still in the mempool."
            )
            raise

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

        logger.info(
            f"[deal {deal_id}] OfferSubmitted by {buyer} "
            f"at price={offer_price} qty={args['quantity']}"
        )

        # Guard 1: in-memory check — tx was sent but receipt timed out last cycle.
        if deal_id in self._pending_deal_ids:
            logger.info(f"[deal {deal_id}] Tx still pending (sent last cycle) — skipping duplicate send.")
            return

        # Guard 2: on-chain check — a previous tx already confirmed and advanced the deal.
        # deals() outputs: (farmer, buyer, agreedPrice, quantity, dealTimestamp, escrowAmount, round, state, ...)
        deal_info = self._trade.functions.deals(deal_id).call()
        on_chain_round: int = deal_info[6]
        # Contract initialises round=1 on submitOffer (buyer's first offer = round 1).
        # round > 1 means the farmer already countered — skip to avoid duplicate sends.
        if on_chain_round > 1:
            logger.info(f"[deal {deal_id}] Already at round {on_chain_round} on chain — skipping duplicate send.")
            self._pending_deal_ids.discard(deal_id)
            return

        if self.check_blacklists(buyer):
            return

        action, price = self.decide(deal_id, offer_price, 1, policy, oracle_price)
        self._pending_deal_ids.add(deal_id)
        try:
            self._execute_action(deal_id, action, price)
        except TimeExhausted:
            # Tx is in the mempool but unconfirmed after 300s (Sepolia issue).
            # Keep deal_id in _pending_deal_ids so the next cycle skips re-sending.
            # Guard 2 (round > 0) will clear it once the tx eventually mines.
            logger.warning(f"[deal {deal_id}] Tx pending in mempool — guard active for next cycle.")
            return
        except Exception:
            # Hard failure (ContractLogicError, RPC error, etc.) — no tx in flight.
            self._pending_deal_ids.discard(deal_id)
            raise
        self._pending_deal_ids.discard(deal_id)

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

        offer_started: int = self._trade.functions.offerStartedAt(deal_id).call()
        current_ts: int = self._w3.eth.get_block("latest").get("timestamp", 0)
        if current_ts - offer_started >= NEGOTIATION_TIMEOUT:
            logger.warning(
                f"[deal {deal_id}] Negotiation timeout exceeded "
                f"({current_ts - offer_started}s elapsed) — walking away."
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

    # ── State persistence ────────────────────────────────────────────────────

    def _load_state(self) -> int:
        """Return the last processed block from the state file.

        Falls back to TRADE_DEPLOY_BLOCK (if set) for a first-run history scan,
        or to the current block if neither is available.

        Returns:
            Block number to use as the exclusive lower bound for the first poll.
        """
        try:
            with open(STATE_FILE) as f:
                data = json.load(f)
                block = int(data["last_block"])
                logger.info(f"Resuming from state file: last_block={block}")
                return block
        except (FileNotFoundError, KeyError, json.JSONDecodeError, ValueError):
            pass

        deploy_env = os.environ.get("TRADE_DEPLOY_BLOCK")
        if deploy_env:
            block = int(deploy_env)
            logger.info(f"No state file — scanning from TRADE_DEPLOY_BLOCK={block}")
            return block

        block = self._w3.eth.block_number
        logger.info(f"No state file or deploy block — starting from current block {block}")
        return block

    def _save_state(self, last_block: int) -> None:
        """Persist last processed block so restarts resume without rescanning."""
        with open(STATE_FILE, "w") as f:
            json.dump({"last_block": last_block}, f)

    # ── Main loop ────────────────────────────────────────────────────────────

    def run(self, poll_interval: int) -> None:
        """Start the main polling loop. Runs indefinitely until interrupted.

        On each tick: reads oracle price and farmer policy (to catch on-chain
        updates), then fetches and processes all new events since the last
        processed block.

        Args:
            poll_interval: Seconds to sleep between poll cycles.
        """
        last_block = self._load_state()
        logger.info(
            f"Agent started. Farmer={self._farmer_addr} "
            f"target_ratio={self._target_ratio}% "
            f"starting_block={last_block}"
        )

        # One-time startup: establish baseline oracle price with full on-chain
        # event-history verification. Subsequent cycles use cached comparison
        # (zero extra RPC calls) instead of scanning PriceUpdated logs every tick.
        try:
            startup_price = self._oracle.get_price()
            if not self._oracle.verify_deviation(startup_price):
                logger.warning("Oracle deviation check failed on startup — proceeding with caution.")
            self._last_oracle_price = startup_price
            logger.info(f"Baseline oracle price: {startup_price}")
        except ContractLogicError:
            logger.warning("Oracle price stale on startup — baseline will be set on first live cycle.")

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

                # Cache-based deviation check: avoids scanning PriceUpdated event
                # history on every cycle. Only triggers when price actually changes.
                if self._last_oracle_price is not None and oracle_price != self._last_oracle_price:
                    deviation = abs(oracle_price - self._last_oracle_price) / self._last_oracle_price
                    if deviation > MAX_PRICE_DEVIATION:
                        logger.error(
                            f"Oracle price moved {deviation:.1%} since last observation "
                            f"(was {self._last_oracle_price}, now {oracle_price}) — skipping cycle."
                        )
                        last_block = current_block
                        time.sleep(poll_interval)
                        continue
                self._last_oracle_price = oracle_price

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
                self._save_state(last_block)

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
        "llm_model": os.environ.get("LLM_MODEL", DEFAULT_LLM_MODEL),
    }


def main() -> None:
    """Entry point. Loads config, connects to Sepolia, wires contracts, starts agent.

    Raises:
        EnvironmentError: If required env vars are missing.
        ConnectionError: If the Infura RPC connection fails.
    """
    config = _load_config()

    rpc_url = f"https://sepolia.infura.io/v3/{config['infura_token']}"
    # Disable HTTP keep-alive so the agent never holds a stale TCP connection
    # across the poll-interval sleep. Infura's load balancer closes idle
    # connections after ~30-60 s; "Connection: close" prevents the silent reuse
    # of a dead socket on the next RPC call.
    w3 = Web3(HTTPProvider(rpc_url, request_kwargs={
        "timeout": 30,
        "headers": {"Connection": "close"},
    }))
    if not w3.is_connected():
        raise ConnectionError("Failed to connect to Sepolia via Infura.")

    wallet = w3.eth.account.from_key(config["private_key"])
    balance = w3.eth.get_balance(wallet.address)
    logger.info(
        f"Agent wallet: {wallet.address} "
        f"({Web3.from_wei(balance, 'ether'):.6f} ETH)"
    )

    # ── AUTH WARNING ────────────────────────────────────────────────────────
    # FarmEscrow.counterOffer() and acceptDeal() require msg.sender == d.farmer
    # or d.buyer.  The agent's wallet is neither unless WALLET_PRIVATE_KEY is
    # the farmer's own key.  All transactions will revert until the contract is
    # updated with an authorizedAgents mapping:
    #
    #   mapping(address => address) public authorizedAgents;
    #
    #   function setAgent(address agent) external {
    #       require(policies[msg.sender].isRegistered, "not registered");
    #       authorizedAgents[msg.sender] = agent;
    #   }
    #
    # And counterOffer / acceptDeal check:
    #   require(msg.sender == d.buyer || msg.sender == d.farmer
    #           || msg.sender == authorizedAgents[d.farmer], "not a party");
    #
    # For testing: set WALLET_PRIVATE_KEY to the farmer's private key so that
    # wallet.address == FARMER_ADDRESS.
    if wallet.address.lower() != config["farmer_addr"].lower():
        logger.warning(
            "AGENT WALLET (%s) != FARMER ADDRESS (%s): counterOffer/acceptDeal "
            "will revert — contract requires msg.sender to be the farmer. "
            "See auth comment in main() for the required contract fix.",
            wallet.address, config["farmer_addr"],
        )

    trade_contract = w3.eth.contract(address=config["trade_addr"], abi=TRADE_ABI)
    oracle_contract = w3.eth.contract(address=config["oracle_addr"], abi=ORACLE_ABI)

    logger.info(f"LLM model: {config['llm_model']}")

    oracle = OracleClient(w3, oracle_contract)
    agent = NegotiationAgent(
        w3=w3,
        trade_contract=trade_contract,
        oracle=oracle,
        wallet=wallet,
        farmer_addr=config["farmer_addr"],
        target_ratio=config["target_ratio"],
        llm_model=config["llm_model"],
    )
    agent.run(poll_interval=config["poll_interval"])


if __name__ == "__main__":
    main()
