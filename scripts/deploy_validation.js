const hre = require("hardhat");

async function main() {
  const [deployer] = await hre.ethers.getSigners();
  console.log("Deploying BlockScamValidationRegistry with:", deployer.address);
  console.log("Network:", hre.network.name);

  const Registry = await hre.ethers.getContractFactory("BlockScamValidationRegistry");
  const registry = await Registry.deploy();
  await registry.waitForDeployment();

  const address = await registry.getAddress();
  console.log("BlockScamValidationRegistry deployed to:", address);
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
