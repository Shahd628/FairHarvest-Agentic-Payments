// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/**
 * @title MockOracle
 * @notice Stores commodity prices for FairHarvest. Enforces:
 *         - Admin-only updates
 *         - 10-minute minimum interval between updates
 *         - 20% maximum price change per update
 *         - 2-hour staleness window (getPrice() reverts if stale)
 */
contract MockOracle {
    // ─── State ────────────────────────────────────────────────────────────────

    address public admin;

    uint256 public currentPrice;      // price in wei-equivalent units (scaled ×1e6)
    uint256 public lastUpdated;       // block.timestamp of last successful update

    uint256 public constant MAX_DEVIATION_BPS = 2000;  // 20% in basis points
    uint256 public constant PRICE_STALENESS_LIMIT = 2 hours;
    
    bool public stalenessCheckEnabled = true;
    uint256 public minUpdateInterval = 1 minutes;
    // ─── Events ───────────────────────────────────────────────────────────────

    event PriceUpdated(uint256 indexed oldPrice, uint256 indexed newPrice, uint256 timestamp);
    event AdminTransferred(address indexed oldAdmin, address indexed newAdmin);

    // ─── Errors ───────────────────────────────────────────────────────────────

    error NotAdmin();
    error TooSoonToUpdate(uint256 nextAllowed);
    error DeviationTooLarge(uint256 oldPrice, uint256 newPrice, uint256 maxAllowed);
    error PriceIsStale(uint256 lastUpdated, uint256 stalenessLimit);
    error InvalidPrice();

    // ─── Constructor ──────────────────────────────────────────────────────────

    /**
     * @param initialPrice  Starting price (scaled ×1e6, e.g. 1 TL = 1_000_000)
     */
    constructor(uint256 initialPrice) {
        require(initialPrice > 0, "Initial price must be > 0");
        admin = msg.sender;
        currentPrice = initialPrice;
        lastUpdated = block.timestamp;
        emit PriceUpdated(0, initialPrice, block.timestamp);
    }

    // ─── Modifiers ────────────────────────────────────────────────────────────

    modifier onlyAdmin() {
        if (msg.sender != admin) revert NotAdmin();
        _;
    }

    // ─── Admin Functions ──────────────────────────────────────────────────────

    /**
     * @notice Update the commodity price.
     * @dev    Enforces 10-min cooldown and 20% deviation cap.
     * @param  newPrice  New price (same unit as initialPrice)
     */
    function updatePrice(uint256 newPrice) external onlyAdmin {
        if (newPrice == 0) revert InvalidPrice();

        // Cooldown check
        uint256 nextAllowed = lastUpdated + minUpdateInterval;
        if (block.timestamp < nextAllowed) revert TooSoonToUpdate(nextAllowed);

        // Deviation check — cap is 20% in either direction
        uint256 oldPrice = currentPrice;
        uint256 maxChange = (oldPrice * MAX_DEVIATION_BPS) / 10_000;
        uint256 diff = newPrice > oldPrice ? newPrice - oldPrice : oldPrice - newPrice;
        if (diff > maxChange) revert DeviationTooLarge(oldPrice, newPrice, oldPrice + maxChange);

        currentPrice = newPrice;
        lastUpdated  = block.timestamp;

        emit PriceUpdated(oldPrice, newPrice, block.timestamp);
    }

    /**
     * @notice Transfer oracle admin rights to a new address.
     */
    function transferAdmin(address newAdmin) external onlyAdmin {
        require(newAdmin != address(0), "Zero address");
        emit AdminTransferred(admin, newAdmin);
        admin = newAdmin;
    }

    function setStalenessCheck(bool enabled) external onlyAdmin {
        stalenessCheckEnabled = enabled;
    }

    function setMinUpdateInterval(uint256 newInterval) external onlyAdmin {
        minUpdateInterval = newInterval;
    }

    // ─── Read Functions ───────────────────────────────────────────────────────

    /**
     * @notice Returns the current price and timestamp.
     * @dev    Reverts if the price has not been updated within PRICE_STALENESS_LIMIT.
     *         This causes acceptDeal() in the main contract to also revert on stale data.
     */
    function getPrice() external view returns (uint256 price, uint256 timestamp) {
        if (stalenessCheckEnabled && block.timestamp > lastUpdated + PRICE_STALENESS_LIMIT) {
            revert PriceIsStale(lastUpdated, PRICE_STALENESS_LIMIT);
        }
        return (currentPrice, lastUpdated);
    }

    /**
     * @notice Returns the current price without a staleness check.
     *         Useful for off-chain monitoring only — do NOT use in escrow logic.
     */
    function getPriceUnsafe() external view returns (uint256 price, uint256 timestamp) {
        return (currentPrice, lastUpdated);
    }

    /**
     * @notice How many seconds until the next update is allowed.
     *         Returns 0 if an update can be submitted right now.
     */
    function secondsUntilNextUpdate() external view returns (uint256) {
        uint256 nextAllowed = lastUpdated + minUpdateInterval;
        if (block.timestamp >= nextAllowed) return 0;
        return nextAllowed - block.timestamp;
    }
}
