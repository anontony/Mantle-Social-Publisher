# Mantle Social Publisher + BlockScam ERC-8004

Mantle Social Publisher is a Web3-gated automation dashboard for publishing financial/crypto content, distributing posts to social platforms, forwarding Telegram messages, and moderating Telegram scam messages with optional ERC-8004 proof anchoring on Mantle.

The project includes:

- Web3 wallet login on Mantle Mainnet.
- MFC credit-token access plan.
- Per-wallet workspace configuration.
- RSS/API news scanning and AI WordPress article generation.
- Telegram, X, and Facebook posting.
- Telegram forwarding.
- BlockScam Telegram moderation.
- ERC-8004-compatible moderation evidence reports and optional on-chain proof submission.
- Railway-friendly deployment.

---

## 1. Repository structure

```text
.
├── app.py                    # FastAPI dashboard and routes
├── core.py                   # Services, config, DB, Telegram, WordPress, BlockScam, ERC-8004 proof logic
├── contracts/
│   └── MantleFlowCredit.sol  # MFC credit token contract
├── scripts/
│   └── deploy.js             # Hardhat deployment script for MFC
├── Dockerfile
├── hardhat.config.js
├── package.json
├── railway.json
├── requirements.txt
├── .env.example
├── README.md
└── README_RAILWAY.md
```

Do not commit `.env`, `runtime/`, `/data`, `app.db`, browser profiles, Telegram sessions, or cookies.

---

## 2. Requirements

### Python

Use Python 3.11+.

Install Python dependencies:

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

### Node.js

Use Node.js LTS, preferably Node 20 or Node 22. Avoid very new Current releases if Hardhat reports unsupported Node warnings.

Install contract dependencies:

```bash
npm install
```

---

## 3. Environment setup

Copy the example environment file:

```bash
cp .env.example .env
```

On Windows CMD:

```cmd
copy .env.example .env
```

Edit `.env`.

Minimum local development values:

```env
RUNTIME_DIR=runtime
PLAYWRIGHT_HEADLESS=0
PROJECT_OWNER_WALLET=0xYourTreasuryWallet
PROJECT_TREASURY=0xYourTreasuryWallet
MANTLE_RPC_URL=https://rpc.mantle.xyz
EXPLORER_API_V2_URL=https://api.etherscan.io/v2/api
EXPLORER_CHAIN_ID=5000
MONTHLY_MNT_AMOUNT=5
MONTHLY_CREDIT_AMOUNT=100
SUBSCRIPTION_DAYS=30
```

Optional global defaults:

```env
OPENAI_API_KEY=your_openai_key
WP_URL=https://your-domain.com/wp-json/wp/v2/posts
WP_JWT=your_wordpress_jwt
CRYPTO_PANIC=your_cryptopanic_token
API_ID=your_telegram_api_id
API_HASH=your_telegram_api_hash
```

Users can also save these values inside their own connected-wallet workspace in the dashboard.

---

## 4. Deploy the MFC access token on Mantle

MFC is the credit-token contract used by the dashboard access plan.

The default demo plan is:

```text
5 MNT -> 100 MFC credits -> 30 days access
```

### 4.1 Prepare a deployer wallet

Create a dedicated deployer wallet and fund it with a small amount of MNT for gas on Mantle Mainnet.

Add this to `.env`:

```env
DEPLOYER_PRIVATE_KEY=0xYourDeployerPrivateKey
PROJECT_TREASURY=0xYourTreasuryWallet
MANTLE_RPC_URL=https://rpc.mantle.xyz
MONTHLY_MNT_AMOUNT=5
MONTHLY_CREDIT_AMOUNT=100
SUBSCRIPTION_DAYS=30
TOKEN_CAP=10000000
TRANSFER_BURN_FEE_BPS=200
```

Never use a seed phrase. Use only the account private key of the deployer wallet. Do not commit `.env`.

### 4.2 Compile the contract

```bash
npm run compile
```

Expected output:

```text
Compiled Solidity files successfully
```

### 4.3 Deploy to Mantle Mainnet

```bash
npm run deploy:mantle
```

The script prints something like:

```text
MantleFlowCredit deployed to: 0xYourMFCContract
```

Copy that address.

### 4.4 Add the deployed token to the app

Set these variables locally or in Railway:

```env
CREDIT_TOKEN_ADDRESS=0xYourMFCContract
CREDIT_TOKEN_SYMBOL=MFC
MONTHLY_MNT_AMOUNT=5
MONTHLY_CREDIT_AMOUNT=100
SUBSCRIPTION_DAYS=30
```

The app verifies access by looking for an ERC-20 `Transfer` mint event from the zero address to the connected user wallet, scoped to `CREDIT_TOKEN_ADDRESS`.

### 4.5 Withdraw collected MNT

When users buy credits, they send native MNT to the MFC contract. The contract mints MFC to the user and keeps the MNT balance in the contract.

A wallet with `TREASURY_ROLE` can call:

```solidity
withdrawNative()
```

This sends the full native MNT balance from the MFC contract to the configured treasury wallet.

You can call this from MantleScan after verifying the contract, from Remix, or from a custom Hardhat script.

---

## 5. Run locally

```bash
uvicorn app:app --host 0.0.0.0 --port 8080
```

Open:

```text
http://localhost:8080
```

Then:

1. Open **User Profile**.
2. Connect a Mantle-compatible EVM wallet.
3. Buy MFC credits.
4. Click **Refresh Credit Balance**.
5. Configure WordPress, Telegram, X, Facebook, Telegram Forward, and BlockScam.
6. Start the bot.

---

## 6. Deploy to Railway

### 6.1 Create Railway project

1. Push this repository to GitHub.
2. Create a new Railway project.
3. Choose **Deploy from GitHub repo**.
4. Add a Railway Volume.
5. Mount the volume at:

```text
/data
```

The volume keeps sessions, SQLite database, browser profiles, and runtime config across redeploys.

### 6.2 Railway variables

Required:

```env
RUNTIME_DIR=/data
PLAYWRIGHT_HEADLESS=1
WEB3_COOKIE_SECURE=1
WEB3_COOKIE_SAMESITE=none
PORT=8080

PROJECT_OWNER_WALLET=0xYourTreasuryWallet
PROJECT_TREASURY=0xYourTreasuryWallet
MANTLE_RPC_URL=https://rpc.mantle.xyz
EXPLORER_API_V2_URL=https://api.etherscan.io/v2/api
EXPLORER_CHAIN_ID=5000
ETHERSCAN_API_KEY=your_etherscan_api_key

CREDIT_TOKEN_ADDRESS=0xYourMFCContract
CREDIT_TOKEN_SYMBOL=MFC
MONTHLY_MNT_AMOUNT=5
MONTHLY_CREDIT_AMOUNT=100
SUBSCRIPTION_DAYS=30
```

Optional global defaults:

```env
OPENAI_API_KEY=your_openai_key
WP_URL=https://your-domain.com/wp-json/wp/v2/posts
WP_JWT=your_wordpress_jwt
CRYPTO_PANIC=your_cryptopanic_token
API_ID=your_telegram_api_id
API_HASH=your_telegram_api_hash
DEMO_WALLETS=0xWalletOne,0xWalletTwo
```

### 6.3 Deploy

After adding variables, redeploy the Railway service and open the generated Railway domain.

---

## 7. Dashboard setup

### 7.1 User Profile

Use this page to:

- Connect wallet.
- Buy MFC credits.
- Refresh access status.
- View credit balance and subscription expiry.

### 7.2 RSS / WordPress

Configure:

- OpenAI API key.
- WordPress REST URL.
- WordPress JWT.
- CryptoPanic token.
- Categories and custom topic filters.
- Content language.
- AI text model.
- Image generation policy.

The WordPress REST URL should look like:

```text
https://your-domain.com/wp-json/wp/v2/posts
```

The WordPress JWT must allow post creation and media upload.

### 7.3 Login & API

This page includes expandable English setup guides for Telegram, X, and Facebook Page API.

#### Telegram

Required:

- Telegram API ID.
- Telegram API Hash.
- Session name.
- Phone number.
- Optional Telegram posting channel.

Flow:

1. Create a Telegram app at `my.telegram.org`.
2. Save API ID and API Hash.
3. Enter phone and session name.
4. Click **Send Telegram Code**.
5. Enter code and 2FA password if needed.
6. Click **Confirm Code**.
7. Click **Test Session**.

The Telegram account must have permission to post, delete messages, and ban users in target groups if BlockScam is enabled.

#### X / Twitter

Required:

- `auth_token` cookie.
- `ct0` cookie.

After saving, click **Test X Post**.

#### Facebook Page API

Required:

- Facebook Page ID.
- Facebook Page Access Token with Page publishing permission.

How to get it:

1. Open Meta Graph API Explorer.
2. Select your Meta app.
3. Add permissions: `pages_show_list`, `pages_read_engagement`, and `pages_manage_posts`.
4. Generate a user access token and allow access to the target Page.
5. Run `GET me/accounts?fields=id,name,access_token,tasks`.
6. Copy the Page `id` and that Page's `access_token` into the dashboard.
7. Save and click **Test Facebook Post**.

Facebook cookie/browser posting has been removed. This avoids unstable UI selectors and prevents accidental comments/share-flow clicks.

### 7.4 Social Posting

Enable or disable posting to:

- Telegram.
- X.
- Facebook.

### 7.5 Telegram Forward

Configure one source channel and multiple target channels/groups.

### 7.6 BlockScam

Configure:

- Telegram chats to scan.
- Scam keywords.
- Optional AI scam detection.
- ERC-8004 proof settings.

---

## 8. BlockScam moderation flow

When a user posts a suspicious Telegram message, BlockScam:

1. Reads the message.
2. Runs keyword rules.
3. Optionally calls the AI classifier.
4. Deletes suspicious messages.
5. Blocks/kicks the sender for high-risk messages when the Telegram account has permission.
6. Builds a moderation evidence report.
7. Hashes the report.
8. Saves the proof locally in SQLite.
9. Optionally submits the proof hash on-chain through ERC-8004 validation flow.

Example evidence fields:

```json
{
  "type": "telegram_moderation_action",
  "standard": "ERC-8004-compatible-offchain-evidence",
  "agentRegistry": "0x...",
  "agentId": "12",
  "platform": "telegram",
  "chatHash": "0x...",
  "userHash": "0x...",
  "messageHash": "0x...",
  "action": "delete_message_and_block_user",
  "riskScore": 94,
  "matchedRules": ["FREE_USDT_LURE", "CONTACT_ME_PATTERN"],
  "originalMessageRedacted": "Ai muốn kiếm **** miễn phí liên hệ tôi"
}
```

The bot does not put raw Telegram IDs, full usernames, full group names, or full private messages on-chain. It stores a proof hash on-chain and keeps detailed evidence off-chain.

---

## 9. ERC-8004 proof setup

ERC-8004 proof anchoring is optional. Local proof storage works even without on-chain configuration.

For public deployments, the project owner configures the shared BlockScam Agent and Validation Registry once. Users only provide a fresh low-balance Proof Writer wallet in the dashboard when they want on-chain proof submission.

### 9.1 Project-level environment variables

```env
ERC8004_RPC_URL=https://rpc.mantle.xyz
ERC8004_AGENT_REGISTRY=0x8004A169FB4a3325136EB29fA0ceB6D2e539a432
ERC8004_REPUTATION_REGISTRY=0x8004BAa17C55a88189AE136b182e5fdA19dE9b63
ERC8004_VALIDATION_REGISTRY=0xYourValidationRegistry
ERC8004_VALIDATOR_ADDRESS=0xYourValidatorAddress
ERC8004_AGENT_ID=YourAgentId
ERC8004_EVIDENCE_BASE_URL=https://your-public-domain.com  # use your public/custom domain, not the internal Railway domain
ERC8004_ONCHAIN_MIN_SCORE=90
```

Leave `ERC8004_PRIVATE_KEY` empty in Railway for public deployments. Each user can save their own fresh Proof Writer private key inside the BlockScam dashboard. The wallet should only contain a small amount of MNT for gas.

### 9.2 Register the BlockScam Agent

```bash
npm run compile
npm run register:agent
```

Copy the printed `ERC8004_AGENT_ID` into Railway.

The evidence report binds moderation proof to:

```text
agentRegistry + agentId + proofHash
```

### 9.3 Validation Registry and Validator

Deploy the lightweight validation registry included in this repository:

```bash
npm run deploy:validation
```

Copy the deployed address into `ERC8004_VALIDATION_REGISTRY`. For a demo, `ERC8004_VALIDATOR_ADDRESS` can be a fresh validator wallet or the same low-balance wallet used for proof writing.

The app calls:

```solidity
validationRequest(validatorAddress, agentId, requestURI, requestHash)
```

### 9.4 Proof endpoints

The app exposes:

```text
/blockscam/proofs
/proof/{proof_hash}
```

Use these to review local evidence and compare it with on-chain proof hashes.

---

## 10. Security notes

- Never commit `.env`.
- Never commit private keys, API keys, cookies, WordPress JWTs, Telegram sessions, browser profiles, `runtime/`, `/data`, or `app.db`.
- Use a dedicated deployer wallet for contracts.
- Use a dedicated proof-writer wallet for ERC-8004 proof transactions.
- Use a dedicated automation account for social posting.
- Keep Railway private and restrict access to the dashboard.
- If a cookie or private key is leaked, rotate it immediately.

---

## 11. Common commands

Install Python dependencies:

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

Install contract dependencies:

```bash
npm install
```

Compile contracts:

```bash
npm run compile
```

Deploy MFC token:

```bash
npm run deploy:mantle
```

Run app locally:

```bash
uvicorn app:app --host 0.0.0.0 --port 8080
```

Clean Node dependencies on Windows CMD:

```cmd
rmdir /s /q node_modules
del package-lock.json
npm install
```

Clean Node dependencies on PowerShell:

```powershell
Remove-Item -Recurse -Force node_modules
Remove-Item -Force package-lock.json
npm install
```

---

## 12. Troubleshooting

### Hardhat says Node.js is unsupported

Install Node.js LTS, preferably Node 20 or Node 22.

### Solidity compile error: `mcopy not found`

Make sure OpenZeppelin is pinned to `5.0.2`:

```bash
npm install @openzeppelin/contracts@5.0.2 --save-exact
npm run compile
```

### User paid but access is not active

Check:

- `CREDIT_TOKEN_ADDRESS` is set to the deployed MFC contract.
- `MONTHLY_MNT_AMOUNT` matches the contract deployment value.
- The transaction confirmed on Mantle Mainnet.
- `ETHERSCAN_API_KEY` is set in Railway for reliable explorer checks.
- User clicked **Refresh Credit Balance** after the transaction confirmed.

### Telegram BlockScam does not delete messages

Check:

- Telegram session is logged in.
- The account is admin in the target group.
- The account has delete-message and ban-user permissions.
- Target chats are entered correctly.

### Social posting fails

For X browser fallback, cookies may have expired or the platform UI may have changed. Re-export X cookies and use the test button.

For Facebook, verify that `FACEBOOK_PAGE_ID` and `FACEBOOK_PAGE_ACCESS_TOKEN` are correct, the token belongs to the target Page, and it has Page publishing permission. Facebook browser/cookie posting is not used in this build.

## Simple ERC-8004 / Verified BlockScam Proof Mode

This build supports a simpler ERC-8004 user experience.

The project owner can register the default BlockScam Agent and configure the proof infrastructure once:

```bash
npm run compile
npm run register:agent
npm run deploy:validation
```

Then set:

```env
ERC8004_AGENT_REGISTRY=0x8004A169FB4a3325136EB29fA0ceB6D2e539a432
ERC8004_REPUTATION_REGISTRY=0x8004BAa17C55a88189AE136b182e5fdA19dE9b63
ERC8004_VALIDATION_REGISTRY=0xYourValidationRegistry
ERC8004_VALIDATOR_ADDRESS=0xYourValidatorAddress
ERC8004_AGENT_ID=YourAgentId
ERC8004_EVIDENCE_BASE_URL=https://your-public-domain.com  # use your public/custom domain, not the internal Railway domain
ERC8004_ONCHAIN_MIN_SCORE=90
```

Normal users do not need to understand or deploy ERC-8004 contracts. They only need to:

1. Create a fresh wallet used only as a Proof Writer wallet.
2. Fund it with a small amount of MNT for gas.
3. Paste that Proof Writer private key in the BlockScam dashboard.
4. Enable **Verified On-chain Proof**.
5. Click **Test Proof**.

Security warning: users should never paste a main wallet private key. The Proof Writer wallet should only contain a small amount of MNT for gas.

### Deploy the demo Validation Registry

A lightweight ERC-8004-compatible demo validation registry is included:

```bash
npm run compile
npm run deploy:validation
```

After deployment, copy the printed address into:

```env
ERC8004_VALIDATION_REGISTRY=0xYourDeployedValidationRegistry
```

For a quick demo, `ERC8004_VALIDATOR_ADDRESS` can be the Proof Writer wallet address or another fresh validator wallet.
