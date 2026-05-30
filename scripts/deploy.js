const hre = require("hardhat");

function requiredEnv(name) {
  const value = process.env[name];
  if (!value || !String(value).trim()) {
    throw new Error(`Missing required environment variable: ${name}`);
  }
  return String(value).trim();
}

function envOrDefault(name, fallback) {
  const value = process.env[name];
  return value && String(value).trim() ? String(value).trim() : fallback;
}

async function main() {
  const treasury = requiredEnv("PROJECT_TREASURY");
  const monthlyMntAmount = envOrDefault("MONTHLY_MNT_AMOUNT", "5");
  const monthlyCreditAmount = envOrDefault("MONTHLY_CREDIT_AMOUNT", "100");
  const subscriptionDays = Number(envOrDefault("SUBSCRIPTION_DAYS", "30"));
  const tokenCap = envOrDefault("TOKEN_CAP", "10000000");
  const transferBurnFeeBps = Number(envOrDefault("TRANSFER_BURN_FEE_BPS", "200"));

  const name = envOrDefault("CREDIT_TOKEN_NAME", "MantleFlow Credit");
  const symbol = envOrDefault("CREDIT_TOKEN_SYMBOL", "MFC");

  const monthlyMntAmountWei = hre.ethers.parseEther(monthlyMntAmount);
  const monthlyCreditAmountUnits = hre.ethers.parseEther(monthlyCreditAmount);
  const capUnits = hre.ethers.parseEther(tokenCap);

  const [deployer] = await hre.ethers.getSigners();
  console.log("Deploying MantleFlowCredit...");
  console.log("Network:", hre.network.name);
  console.log("Deployer:", deployer.address);
  console.log("Treasury:", treasury);
  console.log("Plan:", `${monthlyMntAmount} MNT -> ${monthlyCreditAmount} ${symbol} for ${subscriptionDays} days`);

  const MantleFlowCredit = await hre.ethers.getContractFactory("MantleFlowCredit");
  const token = await MantleFlowCredit.deploy(
    name,
    symbol,
    treasury,
    monthlyMntAmountWei,
    monthlyCreditAmountUnits,
    subscriptionDays,
    capUnits,
    transferBurnFeeBps
  );

  await token.waitForDeployment();
  const address = await token.getAddress();

  console.log("MantleFlowCredit deployed to:", address);
  console.log("Railway variables:");
  console.log(`CREDIT_TOKEN_ADDRESS=${address}`);
  console.log(`CREDIT_TOKEN_SYMBOL=${symbol}`);
  console.log(`MONTHLY_MNT_AMOUNT=${monthlyMntAmount}`);
  console.log(`MONTHLY_CREDIT_AMOUNT=${monthlyCreditAmount}`);
  console.log(`SUBSCRIPTION_DAYS=${subscriptionDays}`);
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
