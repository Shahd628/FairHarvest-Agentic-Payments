// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/**
 * @title  IOracle
 * @notice Interface to Zeynep's MockOracle. getPrice() returns (price, timestamp)
 *         and REVERTS if the price is stale (>2h), which propagates up into
 *         acceptDeal() and causes it to revert too — exactly as the spec wants.
 */
interface IOracle {
    function getPrice() external view returns (uint256 price, uint256 timestamp);
}

/**
 * @title FarmEscrow (TradeContract)
 * @notice On-chain negotiation + escrow for FairHarvest. Matches the interface
 *         the negotiation agent and frontend are coded against:
 *           - policies(address) -> (minPriceRatio, maxDealSize, maxRounds, isRegistered)
 *           - deals(uint256)    -> full Deal struct (11 fields)
 *           - submitOffer / counterOffer / acceptDeal / fundEscrow
 *           - signRelease (2-of-3) / raiseDispute / arbitratorResolve / claimRefund
 *           - farmerBlacklist / platformBlacklist
 *         Price floor is enforced on-chain in acceptDeal() via the oracle.
 *
 * Security carried over from the original design:
 *   - ReentrancyGuard + checks-effects-interactions on every ETH transfer
 *   - paused flag freezes new offers
 *   - Funds can only ever go to farmer (release / dispute win) or buyer
 *     (refund / dispute win). No admin withdrawal path.
 */
contract FarmEscrow {
    // ─── Types ──────────────────────────────────────────────────────────────

    enum DealState { NEGOTIATING, FUNDED, SHIPPED, DISPUTED, COMPLETE, REFUNDED }

    struct FarmerPolicy {
        uint256 minPriceRatio;      // percent, e.g. 90 = 90% of oracle price
        uint256 maxDealSize;        // max kg per deal
        uint256 maxRounds;          // max negotiation rounds
        bool    isRegistered;
    }

    struct Deal {
        address farmer;
        address buyer;
        uint256 agreedPrice;        // price per kg, oracle precision units (x1e6)
        uint256 quantity;           // kg
        uint256 dealTimestamp;      // when the deal was accepted
        uint256 escrowAmount;       // wei locked
        uint8   round;              // current negotiation round
        DealState state;
        string  trackingUrl;        // farmer submits after shipping
        bool    farmerSigned;       // 2-of-3 multisig
        bool    buyerSigned;
    }

    // ─── Constants ──────────────────────────────────────────────────────────

    uint256 public constant NEGOTIATION_TIMEOUT = 2 hours;
    uint256 public constant REFUND_TIMEOUT      = 7 days;
    uint256 public constant MIN_DEPOSIT         = 0.0001 ether;

    // ─── Storage ────────────────────────────────────────────────────────────

    address public owner;
    address public arbitrator;
    IOracle public oracle;
    bool    public paused;

    uint256 public nextDealId;       // starts at 0; first deal is id 0
    mapping(uint256 => Deal) public deals;
    mapping(address => FarmerPolicy) public policies;

    // Negotiation bookkeeping (not in the public Deal struct)
    mapping(uint256 => uint256) public offerStartedAt;   // dealId -> first offer ts
    mapping(uint256 => bool)    public arbitratorSigned; // dealId -> arbitrator approved

    // Blacklists
    mapping(address => mapping(address => bool)) public farmerBlacklist; // farmer => buyer => blocked
    mapping(address => bool) public platformBlacklist;

    // Concurrency
    mapping(address => uint256) public activeDealCount;
    uint256 public maxConcurrentDeals = 5;

    // Approved courier registry
    mapping(string => bool) public approvedCouriers;

    // Reentrancy guard
    uint256 private _lock = 1;

    // ─── Events ─────────────────────────────────────────────────────────────

    event OfferSubmitted(uint256 indexed dealId, address indexed buyer, address indexed farmer, uint256 price, uint256 quantity);
    event CounterOffered(uint256 indexed dealId, address indexed caller, uint256 price, uint8 round);
    event DealAccepted(uint256 indexed dealId, address indexed agent, uint256 agreedPrice);
    event EscrowFunded(uint256 indexed dealId, uint256 amount);
    event TrackingSubmitted(uint256 indexed dealId, string trackingUrl);
    event ReleaseSigned(uint256 indexed dealId, address indexed signer);
    event DealCompleted(uint256 indexed dealId, address indexed paidTo, uint256 amount);
    event DealRefunded(uint256 indexed dealId, address indexed buyer, uint256 amount);
    event DisputeRaised(uint256 indexed dealId, address indexed by);
    event DisputeResolved(uint256 indexed dealId, address indexed winner, uint256 amount);

    event FarmerRegistered(address indexed farmer);
    event BuyerBlacklisted(address indexed farmer, address indexed buyer);
    event BuyerPlatformBlacklisted(address indexed buyer);
    event PausedSet(bool paused);
    event CourierSet(string name, bool approved);

    // ─── Modifiers ──────────────────────────────────────────────────────────

    modifier onlyOwner() { require(msg.sender == owner, "not owner"); _; }
    modifier onlyArbitrator() { require(msg.sender == arbitrator, "not arbitrator"); _; }
    modifier nonReentrant() { require(_lock == 1, "reentrant"); _lock = 2; _; _lock = 1; }
    modifier whenNotPaused() { require(!paused, "paused"); _; }

    // ─── Constructor ────────────────────────────────────────────────────────

    constructor(address _oracle, address _arbitrator) {
        require(_oracle != address(0) && _arbitrator != address(0), "zero addr");
        owner = msg.sender;
        oracle = IOracle(_oracle);
        arbitrator = _arbitrator;
    }

    // ─── Farmer policy ──────────────────────────────────────────────────────

    function registerPolicy(uint256 _minPriceRatio, uint256 _maxDealSize, uint256 _maxRounds) external {
        require(_minPriceRatio > 0 && _minPriceRatio <= 100, "ratio 1-100");
        require(_maxDealSize > 0, "deal size > 0");
        policies[msg.sender] = FarmerPolicy(_minPriceRatio, _maxDealSize, _maxRounds, true);
        emit FarmerRegistered(msg.sender);
    }

    // ─── Negotiation ────────────────────────────────────────────────────────

    /// @notice Buyer opens a deal with a farmer at a proposed price.
    function submitOffer(address farmer, uint256 price, uint256 quantity)
        external
        whenNotPaused
        returns (uint256 dealId)
    {
        FarmerPolicy memory p = policies[farmer];
        require(p.isRegistered, "farmer not registered");
        require(quantity <= p.maxDealSize, "exceeds max deal size");
        require(!farmerBlacklist[farmer][msg.sender], "blocked by farmer");
        require(!platformBlacklist[msg.sender], "platform blacklisted");
        require(activeDealCount[farmer] < maxConcurrentDeals, "farmer at max concurrent deals");

        dealId = nextDealId++;
        Deal storage d = deals[dealId];
        d.farmer = farmer;
        d.buyer = msg.sender;
        d.agreedPrice = price;     // proposed; finalized on acceptDeal
        d.quantity = quantity;
        d.round = 1;
        d.state = DealState.NEGOTIATING;

        offerStartedAt[dealId] = block.timestamp;
        activeDealCount[farmer] += 1;

        emit OfferSubmitted(dealId, msg.sender, farmer, price, quantity);
    }

    /// @notice Either the agent (for the farmer) or the buyer proposes a new price.
    function counterOffer(uint256 dealId, uint256 price) external {
        Deal storage d = deals[dealId];
        require(d.state == DealState.NEGOTIATING, "not negotiating");
        require(block.timestamp <= offerStartedAt[dealId] + NEGOTIATION_TIMEOUT, "negotiation timed out");
        require(d.round < policies[d.farmer].maxRounds, "max rounds reached");
        require(msg.sender == d.buyer || msg.sender == d.farmer, "not a party");

        d.round += 1;
        d.agreedPrice = price;
        emit CounterOffered(dealId, msg.sender, price, d.round);
    }

    /// @notice Finalizes the deal at the current price. Enforces the oracle floor.
    /// @dev    Anyone party-side may accept; the agent calls this for the farmer.
    function acceptDeal(uint256 dealId) external {
        Deal storage d = deals[dealId];
        require(d.state == DealState.NEGOTIATING, "not negotiating");
        require(block.timestamp <= offerStartedAt[dealId] + NEGOTIATION_TIMEOUT, "negotiation timed out");
        require(msg.sender == d.buyer || msg.sender == d.farmer, "not a party");

        // On-chain price floor: agreedPrice must be >= minPriceRatio% of oracle price.
        // getPrice() reverts if stale, which reverts acceptDeal() too.
        (uint256 oraclePrice, ) = oracle.getPrice();
        uint256 floor = (policies[d.farmer].minPriceRatio * oraclePrice) / 100;
        require(d.agreedPrice >= floor, "price below floor");

        d.dealTimestamp = block.timestamp;
        // state stays NEGOTIATING until funded? Spec: acceptDeal -> Accepted -> fundEscrow -> Funded.
        // We collapse "Accepted" into NEGOTIATING-with-timestamp; fundEscrow moves to FUNDED.
        emit DealAccepted(dealId, msg.sender, d.agreedPrice);
    }

    /// @notice Buyer locks the agreed payment into escrow.
    function fundEscrow(uint256 dealId) external payable whenNotPaused nonReentrant {
        Deal storage d = deals[dealId];
        require(d.state == DealState.NEGOTIATING, "not in fundable state");
        require(d.dealTimestamp != 0, "deal not accepted yet");
        require(msg.sender == d.buyer, "only buyer funds");
        require(msg.value >= MIN_DEPOSIT, "below min deposit");

        d.escrowAmount = msg.value;
        d.state = DealState.FUNDED;
        emit EscrowFunded(dealId, msg.value);
    }

    // ─── Delivery & release ─────────────────────────────────────────────────

    /// @notice Farmer records shipment. Courier must be on the approved list.
    function submitTracking(uint256 dealId, string calldata courier, string calldata trackingUrl) external {
        Deal storage d = deals[dealId];
        require(msg.sender == d.farmer, "only farmer");
        require(d.state == DealState.FUNDED, "not funded");
        require(approvedCouriers[courier], "courier not approved");
        d.trackingUrl = trackingUrl;
        d.state = DealState.SHIPPED;
        emit TrackingSubmitted(dealId, trackingUrl);
    }

    /// @notice 2-of-3 release. Any two of {farmer, buyer, arbitrator} sign,
    ///         funds go to the farmer. Works in FUNDED or SHIPPED state.
    function signRelease(uint256 dealId) external nonReentrant {
        Deal storage d = deals[dealId];
        require(d.state == DealState.FUNDED || d.state == DealState.SHIPPED, "not releasable");

        if (msg.sender == d.farmer) {
            require(!d.farmerSigned, "already signed");
            d.farmerSigned = true;
        } else if (msg.sender == d.buyer) {
            require(!d.buyerSigned, "already signed");
            d.buyerSigned = true;
        } else if (msg.sender == arbitrator) {
            require(!arbitratorSigned[dealId], "already signed");
            arbitratorSigned[dealId] = true;
        } else {
            revert("not a signer");
        }
        emit ReleaseSigned(dealId, msg.sender);

        uint8 sigs = (d.farmerSigned ? 1 : 0) + (d.buyerSigned ? 1 : 0) + (arbitratorSigned[dealId] ? 1 : 0);
        if (sigs >= 2) {
            _payFarmer(dealId, d);
        }
    }

    /// @notice Buyer reclaims funds after 7 days if the deal never completed.
    function claimRefund(uint256 dealId) external nonReentrant {
        Deal storage d = deals[dealId];
        require(d.state == DealState.FUNDED || d.state == DealState.SHIPPED, "not refundable");
        require(block.timestamp >= d.dealTimestamp + REFUND_TIMEOUT, "timeout not reached");

        d.state = DealState.REFUNDED;
        if (activeDealCount[d.farmer] > 0) activeDealCount[d.farmer] -= 1;
        uint256 amount = d.escrowAmount;
        d.escrowAmount = 0;

        (bool ok, ) = payable(d.buyer).call{value: amount}("");
        require(ok, "refund failed");
        emit DealRefunded(dealId, d.buyer, amount);
    }

    // ─── Disputes ───────────────────────────────────────────────────────────

    function raiseDispute(uint256 dealId) external {
        Deal storage d = deals[dealId];
        require(msg.sender == d.farmer || msg.sender == d.buyer, "not a party");
        require(d.state == DealState.FUNDED || d.state == DealState.SHIPPED, "no escrow to dispute");
        d.state = DealState.DISPUTED;
        emit DisputeRaised(dealId, msg.sender);
    }

    /// @notice Arbitrator resolves a dispute. Winner must be farmer or buyer.
    function arbitratorResolve(uint256 dealId, address winner) external onlyArbitrator nonReentrant {
        Deal storage d = deals[dealId];
        require(d.state == DealState.DISPUTED, "not disputed");
        require(winner == d.farmer || winner == d.buyer, "winner must be a party");

        if (activeDealCount[d.farmer] > 0) activeDealCount[d.farmer] -= 1;
        uint256 amount = d.escrowAmount;
        d.escrowAmount = 0;
        d.state = (winner == d.farmer) ? DealState.COMPLETE : DealState.REFUNDED;

        (bool ok, ) = payable(winner).call{value: amount}("");
        require(ok, "transfer failed");
        emit DisputeResolved(dealId, winner, amount);
    }

    // ─── Internal settlement ────────────────────────────────────────────────

    function _payFarmer(uint256 dealId, Deal storage d) internal {
        d.state = DealState.COMPLETE;
        if (activeDealCount[d.farmer] > 0) activeDealCount[d.farmer] -= 1;
        uint256 amount = d.escrowAmount;
        d.escrowAmount = 0;

        (bool ok, ) = payable(d.farmer).call{value: amount}("");
        require(ok, "payout failed");
        emit DealCompleted(dealId, d.farmer, amount);
    }

    // ─── Blacklists ─────────────────────────────────────────────────────────

    function blacklistBuyer(address buyer) external {
        require(policies[msg.sender].isRegistered, "not registered");
        farmerBlacklist[msg.sender][buyer] = true;
        emit BuyerBlacklisted(msg.sender, buyer);
    }

    function unblacklistBuyer(address buyer) external {
        farmerBlacklist[msg.sender][buyer] = false;
        emit BuyerBlacklisted(msg.sender, buyer); // event reused; off-chain reads current state
    }

    function platformBlacklistBuyer(address buyer) external onlyOwner {
        platformBlacklist[buyer] = true;
        emit BuyerPlatformBlacklisted(buyer);
    }

    function platformUnblacklistBuyer(address buyer) external onlyOwner {
        platformBlacklist[buyer] = false;
    }

    // ─── Admin ──────────────────────────────────────────────────────────────

    function setPaused(bool _paused) external onlyOwner { paused = _paused; emit PausedSet(_paused); }
    function setArbitrator(address a) external onlyOwner { require(a != address(0), "zero"); arbitrator = a; }
    function setOracle(address o) external onlyOwner { require(o != address(0), "zero"); oracle = IOracle(o); }
    function setMaxConcurrentDeals(uint256 n) external onlyOwner { require(n > 0, "n>0"); maxConcurrentDeals = n; }
    function setCourier(string calldata name, bool approved) external onlyOwner {
        approvedCouriers[name] = approved;
        emit CourierSet(name, approved);
    }

    // ─── Views ──────────────────────────────────────────────────────────────

    function getDeal(uint256 dealId) external view returns (Deal memory) { return deals[dealId]; }
}
