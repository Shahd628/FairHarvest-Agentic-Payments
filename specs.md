# Smart Contract Outline

Farmer\
    ├── Negotiates via Farmi negotiation agent\
    ├── Ships via approved courier(s) only\
    └── Submits tracking URL on-chain\

AI Arbitration Agent\
    ├── Fetches tracking URL from chain farmer\
    ├── Validates domain matches approved courier\
    ├── Parses structured data using courier-specific parser\
    ├── Validates shipment date against deal timestamp\
    └── Makes high-confidence decision almost always\

Smart Contract\
    ├── Stores approved courier list\
    ├── Enforces courier must be approved\
    ├── Multisig 2-of-3 releases funds\
    └── Timelock refunds if never delivered\

One contract for all of the farmers:\
Deploy (once)\
    ↓\
registerPolicy (farmer's wallet)\
    ↓\
submitOffer (buyer's wallet)\
    ↓\
acceptDeal (agent wallet — pays gas only)\
    ↓\
fundEscrow (buyer's wallet — sends real ETH to contract)\
    ↓\
multiSigPaymentReveal(farmer's wallet — triggers payment release)\
```
(
[normal] farmer.sign() + buyer.sign() → paid 
[dispute] raiseDispute() → arbitrator investigates → arbitrator.sign() + winning party.sign() → resolved
)
```

Store each farmer's policy as a struct and map it by their address.

Ex:
``` js
struct FarmerPolicy {
    uint256 minPriceRatio;    // e.g. 90 means 90% of oracle price
    uint256 maxDealSize;      // max kg per deal
    uint256 maxRounds;        // max negotiation rounds
    bool isRegistered;        // has this farmer set up her policy
}

mapping(address => FarmerPolicy) public policies;

function registerPolicy( uint256 _minPriceRatio, uint256 _maxDealSize, uint256 _maxRounds ) public 
{ policies[msg.sender] = FarmerPolicy(
    {
        minPriceRatio: _minPriceRatio, 
        maxDealSize: _maxDealSize, 
        maxRounds: _maxRounds, 
        isRegistered: true
    });
}
```

Use events and states for negotiation. Negotiation Agent:
1. Buyer submits on-chain
2. Agent wakes up and checks the offer against policy
3. Counteroffer if applicable
4. Buyer accepts the offer
5. Buyer send the money to contract

Throughout, use events to emit the offers and counteroffers for agent and buyer to keep track of the transactions.

Example Run:

Oracle price=2500\
Buyer calls submitOffer(2500)\
    → contract emits OfferSubmitted(dealId, 2500)\
    → agent is listening, wakes up\

Agent calls counterOffer(2550)\
    → contract emits CounterOffered(dealId, 2550, round=2)\
    → buyer is listening, sees counter\

Buyer calls counterOffer(2530)\
    → contract emits CounterOffered(dealId, 2530, round=3)\
    → agent is listening, wakes up\

Agent calls acceptDeal()\
    → contract emits DealAccepted(dealId, 2530)\
    → buyer is listening, knows to fund escrow\

Buyer calls fundEscrow()\
    → contract emits EscrowFunded(dealId, 2530)\
    → farmer is listening, knows to deliver\

# Oracle Components

## Off-chain
> a mock FastAPI/real API of current potato prices (ex: in USD/100 kg)

- push prices to oracle contract via transactions
- separate Python script (ex: 20 lines of Web3.py) runs every 2 to 4 hours, reads endpoint, and calls updatePrice(newPrice) on oracle contract, signed by admin private key (update time can be configurable parameter)
- the oracle is centralized and controlled by the admin key; in production this would be replaced by Chainlink or Pyth
## On-chain

- Oracle contract :latest price + timestamp; getPrice() function → gets called by Escrow contract
- Oracle Interactions:
    - Agent uses getPrice() from oracle contract for negotiation\
    i.e. counter-offer at 95% of market, decide to walk away at 80% , etc…
    - Escrow contract uses getPrice() from oracle contract to enforce floor price/percentage policy
    - Tx fails/reverts if agreed price below some floor relative to oracle price
- Oracle Contract security:
    - price and lastUpdated storage variables
    - updatePrice() function only callable by admin address;
    - price cannot change more than 20% in one update\
    * Does not protect against 19% change followed by another 19% change
    - Oracle contract should emit event on every price update(creates public on-chain history of prices)
    - Agent can verify the price by reading event log rather than just trusting stored value
    - getPrice() reverts if timestamp too old (ex: 2 hours ago)
    - negotiation -to-settlement price gap:
        - Negotiation deal started when price was 4.8 but at payment time (hours later) price is 5.
        - Add a negotiation time limit, ex: 2 hours (enforceable by agent)
        - Add a staleness check in escrow: check block.timestamp - oracle.lastUpdated < maxAge
        - Escrow price check should be within a flexible enough percentage band, but not too flexible to avoid exploitation (_minPriceRatio)
        - Escrow can have a paused flag varibale to freeze new deal creation if something is wrong
        - FastAPI mock should be secured with an API key, and TLS certificate

# Smart Contract Additional Feature: Blacklists

An example of keeping track of a deal between a specific buyer and farmer
```js
struct Deal {
    address farmer;
    address buyer;
    uint256 agreedPrice;      // in your price precision units
    uint256 quantity;         // kg
    uint256 dealTimestamp;    // when deal was accepted
    uint256 escrowAmount;     // ETH locked
    uint8   round;            // current negotiation round
    DealState state;          // enum: NEGOTIATING, FUNDED, DISPUTED, COMPLETE, REFUNDED
    string  trackingUrl;      // farmer submits after shipping
    bool    farmerSigned;     // for 2-of-3 multisig
    bool    buyerSigned;
}

enum DealState { NEGOTIATING, FUNDED, SHIPPED, DISPUTED, COMPLETE, REFUNDED }

mapping(uint256 => Deal) public deals;
uint256 public nextDealId;
```
**Farmer-level blacklist** — per-farmer, stored on-chain, controlled by the farmer. A farmer who has been cheated by a specific buyer can block them permanently.

**Platform-level blacklist** — global, controlled by the contract owner/admin. For buyers who have committed fraud across multiple farmers.
solidity
```js
// Farmer-level: farmer controls their own blacklist
mapping(address => mapping(address => bool)) public farmerBlacklist;
// farmerBlacklist[farmerAddress][buyerAddress] = true means blocked

function blacklistBuyer(address buyer) external {
    farmerBlacklist[msg.sender][buyer] = true;
    emit BuyerBlacklisted(msg.sender, buyer);
}

// Platform-level: only contract owner
mapping(address => bool) public platformBlacklist;

function platformBlacklistBuyer(address buyer) external onlyOwner {
    platformBlacklist[buyer] = true;
    emit BuyerPlatformBlacklisted(buyer);
}
```

Then in submitOffer(), check both blacklists! **The farmer's agent should also check the blacklist before responding to any offer** — but the contract check is the hard guarantee.

**Add the farmer policy file's blacklist as a third layer in the negotiation agent, so the agent won't even acknowledge an offer from a blacklisted address before it reaches on-chain validation.**

# System Actors

**Negotiation Agent**
- Role: Polls chain for new offers; calls LLM; validates decisions against on-chain farmer policy; submits accept/reject/counter transactions
- Security: policy-constrained; wallet holds gas only

**Arbitration Agent**
- Role: Fetches tracking URL from chain; parses approved courier page; LLM determines submitted URL liability; signs as arbitrator in; reasons about dispute evidence for arbitration
- Security: separate wallet and process from negotiation agent

**Oracle Updater Service** (off-chain python script)
- Role: Fetches commodity prices from a mock API; writes to mock oracle every 2-4 hours
- Security: 20% deviation cap limits damage if key is compromised

**Main Contract**
- Role: Enforces escrow lock, multisig 2-of-3, delivery terms, insurance logic, timelock refund, and price floor

**Oracle Contract**
- Role: Stores current commodity price; enforces 20% max-change cap per update

**Approved Courier Registry**
- Role: Stores FairHarvest-approved courier names, domains

# Deal State Machine

| **Current State**       | **Trigger**                | **Called By**                   | **Next State**        | **Oracle / Security Check**                                                                                                                                                   |
|-------------------------|----------------------------|---------------------------------|-----------------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| _(none)_                | submitOffer()              | Buyer                           | Offered               | Reverts if contract paused; farmer must be registered; qty ≤ maxDealSize; farmer-level blacklist check; platform-level blacklist check; concurrent deals < maxConcurrentDeals |
| _Offered_               | counterOffer()             | Agent or Buyer                  | Negotiating           | Reverts if negotiation > 2h; round ≤ maxRounds                                                                                                                                |
| _Negotiating_           | counterOffer()             | Agent or Buyer                  | Negotiating (round++) | Same: 2h timeout + round limit                                                                                                                                                |
| _Offered / Negotiating_ | acceptDeal()               | Agent                           | Accepted              | Escrow calls oracle getPrice(); reverts if stale > 2h; reverts if price < minPriceRatio × oraclePrice; 2h negotiation timeout                                                 |
| _Accepted_              | fundEscrow() + send ETH    | Buyer                           | Funded                | require(msg.value == agreedPrice); require(msg.value ≥ MIN_DEPOSIT); per-deal escrow tracked by dealId (never pooled)                                                         |
| _Funded_                | signRelease() x2           | Farmer + Buyer (or +Arbitrator) | Completed             | ReentrancyGuard; checks-effects-interactions pattern; deal state updated before ETH transfer                                                                                  |
| _Funded_                | raiseDispute()             | Farmer or Buyer                 | Disputed              | Only farmer or buyer can call; deal must have escrowed funds                                                                                                                  |
| _Disputed_              | arbitratorResolve()        | Arbitrator                      | Completed or Refunded | onlyArbitrator modifier; winner must be farmer or buyer address; ReentrancyGuard                                                                                              |
| _Funded / Delivered_    | claimRefund() after 7 days | Anyone                          | Refunded              | Reverts if block.timestamp < createdAt + 7 days; ReentrancyGuard; funds return to original buyer only                                                                         |



