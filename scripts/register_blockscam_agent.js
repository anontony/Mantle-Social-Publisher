const hre = require("hardhat");

const IDENTITY_REGISTRY =
  process.env.ERC8004_AGENT_REGISTRY ||
  "0x8004A169FB4a3325136EB29fA0ceB6D2e539a432";

const CHAIN_ID = process.env.EXPLORER_CHAIN_ID || "5000";
const APP_URL =
  process.env.ERC8004_EVIDENCE_BASE_URL ||
  process.env.APP_URL ||
  "https://your-app.example";

const IDENTITY_ABI = [
  "function register(string agentURI) external returns (uint256 agentId)",
  "function setAgentURI(uint256 agentId, string calldata newURI) external",
  "function tokenURI(uint256 tokenId) external view returns (string)",
  "event Registered(uint256 indexed agentId, string agentURI, address indexed owner)",
];

function makeAgentCard(agentId) {
  const baseUrl = APP_URL.replace(/\/$/, "");
  const agentRegistryRef = `eip155:${CHAIN_ID}:${IDENTITY_REGISTRY}`;

  return {
    type: "https://eips.ethereum.org/EIPS/eip-8004#registration-v1",
    name: "BlockScam AI Agent",
    description:
      "A Telegram moderation agent that detects scam messages, removes risky content, blocks repeat attackers, and creates ERC-8004-compatible moderation proofs on Mantle.",
    image: `${baseUrl}/static/blockscam-agent.png`,
    services: [
      { name: "web", endpoint: baseUrl },
      {
        name: "evidence-api",
        endpoint: `${baseUrl}/proof/{proofHash}`,
        version: "1.0.0",
      },
      {
        name: "blockscam-telegram-moderation",
        endpoint: `${baseUrl}/blockscam/proofs`,
        version: "1.0.0",
      },
    ],
    x402Support: false,
    active: true,
    registrations: [
      {
        agentRegistry: agentRegistryRef,
        agentId: Number(agentId),
      },
    ],
    supportedTrust: ["reputation", "validation", "moderation-proof"],
  };
}

function toDataURI(json) {
  const encoded = Buffer.from(JSON.stringify(json, null, 2), "utf8").toString(
    "base64"
  );
  return `data:application/json;base64,${encoded}`;
}

async function main() {
  const [deployer] = await hre.ethers.getSigners();

  console.log("Registering BlockScam Agent...");
  console.log("Network:", hre.network.name);
  console.log("Owner wallet:", deployer.address);
  console.log("Identity Registry:", IDENTITY_REGISTRY);
  console.log("App URL:", APP_URL);

  const identity = new hre.ethers.Contract(
    IDENTITY_REGISTRY,
    IDENTITY_ABI,
    deployer
  );

  const placeholderURI = toDataURI(makeAgentCard(0));

  let predictedAgentId = 0n;
  try {
    predictedAgentId = await identity["register(string)"].staticCall(
      placeholderURI
    );
    console.log("Predicted Agent ID:", predictedAgentId.toString());
  } catch (error) {
    console.warn("Could not predict Agent ID. Will read it from the event.");
  }

  const firstURI = toDataURI(makeAgentCard(predictedAgentId || 0));
  const tx = await identity["register(string)"](firstURI);
  console.log("Register tx:", tx.hash);

  const receipt = await tx.wait();
  let actualAgentId = null;

  for (const log of receipt.logs) {
    try {
      const parsed = identity.interface.parseLog(log);
      if (parsed && parsed.name === "Registered") {
        actualAgentId = parsed.args.agentId;
        break;
      }
    } catch (_) {}
  }

  if (actualAgentId === null) {
    actualAgentId = predictedAgentId;
  }

  if (!actualAgentId || actualAgentId.toString() === "0") {
    throw new Error("Could not detect Agent ID from transaction logs.");
  }

  console.log("✅ BlockScam Agent registered.");
  console.log("Agent ID:", actualAgentId.toString());

  const finalURI = toDataURI(makeAgentCard(actualAgentId));
  if (predictedAgentId.toString() !== actualAgentId.toString()) {
    console.log("Updating final agentURI with actual Agent ID...");
    const updateTx = await identity.setAgentURI(actualAgentId, finalURI);
    console.log("setAgentURI tx:", updateTx.hash);
    await updateTx.wait();
    console.log("✅ agentURI updated.");
  }

  console.log("");
  console.log("Add these Railway Variables:");
  console.log("----------------------------------------");
  console.log(`ERC8004_AGENT_ID=${actualAgentId.toString()}`);
  console.log(`ERC8004_AGENT_REGISTRY=${IDENTITY_REGISTRY}`);
  console.log(
    "ERC8004_REPUTATION_REGISTRY=0x8004BAa17C55a88189AE136b182e5fdA19dE9b63"
  );
  console.log(`ERC8004_EVIDENCE_BASE_URL=${APP_URL}`);
  console.log("----------------------------------------");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
