// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import "@openzeppelin/contracts/token/ERC20/extensions/ERC20Burnable.sol";
import "@openzeppelin/contracts/token/ERC20/extensions/ERC20Capped.sol";
import "@openzeppelin/contracts/token/ERC20/extensions/ERC20Pausable.sol";
import "@openzeppelin/contracts/token/ERC20/extensions/ERC20Permit.sol";
import "@openzeppelin/contracts/access/AccessControl.sol";
import "@openzeppelin/contracts/utils/ReentrancyGuard.sol";

contract MantleFlowCredit is
    ERC20,
    ERC20Burnable,
    ERC20Capped,
    ERC20Pausable,
    ERC20Permit,
    AccessControl,
    ReentrancyGuard
{
    bytes32 public constant CONFIG_ROLE = keccak256("CONFIG_ROLE");
    bytes32 public constant MINT_ROLE = keccak256("MINT_ROLE");
    bytes32 public constant PAUSE_ROLE = keccak256("PAUSE_ROLE");
    bytes32 public constant TREASURY_ROLE = keccak256("TREASURY_ROLE");

    address public treasury;
    uint256 public monthlyMntAmountWei;
    uint256 public monthlyCreditAmount;
    uint256 public subscriptionDays;
    uint16 public transferBurnFeeBps;

    uint16 public constant MAX_TRANSFER_BURN_FEE_BPS = 500;
    uint16 public constant BPS_DENOMINATOR = 10_000;

    event CreditsPurchased(address indexed buyer, uint256 paidWei, uint256 mintedCredits, uint256 subscriptionDays);
    event TreasuryUpdated(address indexed oldTreasury, address indexed newTreasury);
    event MonthlyPlanUpdated(uint256 monthlyMntAmountWei, uint256 monthlyCreditAmount, uint256 subscriptionDays);
    event TransferBurnFeeUpdated(uint16 transferBurnFeeBps);
    event NativeWithdrawn(address indexed to, uint256 amountWei);

    constructor(
        string memory name_,
        string memory symbol_,
        address treasury_,
        uint256 monthlyMntAmountWei_,
        uint256 monthlyCreditAmount_,
        uint256 subscriptionDays_,
        uint256 cap_,
        uint16 transferBurnFeeBps_
    )
        ERC20(name_, symbol_)
        ERC20Capped(cap_)
        ERC20Permit(name_)
    {
        require(treasury_ != address(0), "Treasury is zero address");
        require(monthlyMntAmountWei_ > 0, "Plan price is zero");
        require(monthlyCreditAmount_ > 0, "Credit amount is zero");
        require(subscriptionDays_ > 0, "Subscription days is zero");
        require(transferBurnFeeBps_ <= MAX_TRANSFER_BURN_FEE_BPS, "Burn fee too high");

        treasury = treasury_;
        monthlyMntAmountWei = monthlyMntAmountWei_;
        monthlyCreditAmount = monthlyCreditAmount_;
        subscriptionDays = subscriptionDays_;
        transferBurnFeeBps = transferBurnFeeBps_;

        _grantRole(DEFAULT_ADMIN_ROLE, msg.sender);
        _grantRole(CONFIG_ROLE, msg.sender);
        _grantRole(MINT_ROLE, msg.sender);
        _grantRole(PAUSE_ROLE, msg.sender);
        _grantRole(TREASURY_ROLE, msg.sender);
        _grantRole(TREASURY_ROLE, treasury_);
    }

    receive() external payable {
        buyCredits();
    }

    function buyCredits() public payable nonReentrant whenNotPaused {
        require(msg.value >= monthlyMntAmountWei, "Insufficient MNT for plan");

        _mint(msg.sender, monthlyCreditAmount);
        emit CreditsPurchased(msg.sender, monthlyMntAmountWei, monthlyCreditAmount, subscriptionDays);

        uint256 refund = msg.value - monthlyMntAmountWei;
        if (refund > 0) {
            (bool ok, ) = payable(msg.sender).call{value: refund}("");
            require(ok, "Refund failed");
        }
    }

    function setMonthlyPlan(
        uint256 monthlyMntAmountWei_,
        uint256 monthlyCreditAmount_,
        uint256 subscriptionDays_
    ) external onlyRole(CONFIG_ROLE) {
        require(monthlyMntAmountWei_ > 0, "Plan price is zero");
        require(monthlyCreditAmount_ > 0, "Credit amount is zero");
        require(subscriptionDays_ > 0, "Subscription days is zero");
        monthlyMntAmountWei = monthlyMntAmountWei_;
        monthlyCreditAmount = monthlyCreditAmount_;
        subscriptionDays = subscriptionDays_;
        emit MonthlyPlanUpdated(monthlyMntAmountWei_, monthlyCreditAmount_, subscriptionDays_);
    }

    function setTreasury(address newTreasury) external onlyRole(CONFIG_ROLE) {
        require(newTreasury != address(0), "Treasury is zero address");
        address oldTreasury = treasury;
        treasury = newTreasury;
        _grantRole(TREASURY_ROLE, newTreasury);
        emit TreasuryUpdated(oldTreasury, newTreasury);
    }

    function setTransferBurnFeeBps(uint16 newFeeBps) external onlyRole(CONFIG_ROLE) {
        require(newFeeBps <= MAX_TRANSFER_BURN_FEE_BPS, "Burn fee too high");
        transferBurnFeeBps = newFeeBps;
        emit TransferBurnFeeUpdated(newFeeBps);
    }

    function mint(address to, uint256 amount) external onlyRole(MINT_ROLE) {
        _mint(to, amount);
    }

    function pause() external onlyRole(PAUSE_ROLE) {
        _pause();
    }

    function unpause() external onlyRole(PAUSE_ROLE) {
        _unpause();
    }

    function withdrawNative() external nonReentrant onlyRole(TREASURY_ROLE) {
        uint256 balance = address(this).balance;
        require(balance > 0, "No MNT to withdraw");
        (bool ok, ) = payable(treasury).call{value: balance}("");
        require(ok, "Withdraw failed");
        emit NativeWithdrawn(treasury, balance);
    }

    function _update(address from, address to, uint256 value)
        internal
        override(ERC20, ERC20Capped, ERC20Pausable)
    {
        if (
            transferBurnFeeBps > 0 &&
            from != address(0) &&
            to != address(0) &&
            value > 0
        ) {
            uint256 fee = (value * transferBurnFeeBps) / BPS_DENOMINATOR;
            uint256 sendAmount = value - fee;
            if (fee > 0) {
                super._update(from, address(0), fee);
            }
            super._update(from, to, sendAmount);
            return;
        }

        super._update(from, to, value);
    }
}
