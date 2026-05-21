# Mantle Social Publisher

Mantle Social Publisher is a Web3-gated automation dashboard for content publishing and social distribution. Users connect an EVM wallet on Mantle Mainnet, pay a monthly access plan in MNT, and unlock a personal workspace that can automate RSS-based content creation, WordPress publishing, Telegram distribution, X/Facebook posting, Telegram forwarding, and basic scam-message filtering.

## Core concept

The app combines three layers:

1. **Web3 account and access layer**
   - Wallet login with MetaMask, Rabby, Coinbase Wallet, or any injected EVM wallet.
   - Mantle Mainnet support with chain ID `5000`.
   - On-chain payment verification for native MNT transfers.
   - Fixed project owner wallet for subscriptions.
   - Per-wallet workspace and saved settings.

2. **Publishing automation layer**
   - Fetches financial and crypto news from RSS / API sources.
   - Scores news relevance.
   - Writes a WordPress-ready article in the selected content language.
   - Optionally generates a featured image.
   - Publishes to WordPress through the REST API.

3. **Distribution and moderation layer**
   - Creates a social summary using the same selected language.
   - Posts to Telegram, X, and Facebook when enabled.
   - Forwards Telegram channel messages to selected target channels/groups.
   - Monitors selected Telegram chats for configured scam keywords and attempts removal when the Telegram account has permission.

## Web3 subscription model

The monthly plan is configured server-side and is not editable from the dashboard.

Default plan:

- **100 Credits / month**
- **50 MNT / month**
- **30 days access**
- **Mantle Mainnet**
- **Chain ID: 5000**

Credit Balance behaves like a monthly usage/status meter. A new subscription starts at `100 Credits`. The value decreases over time based on the remaining subscription window and reaches `0 Credits` when the 30-day plan expires. The dashboard also shows the same balance as a percentage progress bar.

Example:

| Subscription age | Credits left | Percentage |
|---:|---:|---:|
| Day 0 | 100 / 100 | 100% |
| Day 15 | 50 / 100 | 50% |
| Day 30 | 0 / 100 | 0% |

## Fixed project wallet and demo wallet

Payments are verified against this project owner wallet:

```text
0x152B5F1E58ACD5036D8d2027D3B793e81103E644
```

The wallet is hard-coded in the app and cannot be changed from the dashboard. Runtime settings such as RPC URL, explorer API URL, API key, plan price, and monthly credits are configured through Railway Variables or environment variables.

The same wallet is also configured as the built-in demo wallet for project review. When this wallet signs in, the dashboard grants a full monthly plan automatically without requiring an on-chain payment check. All other wallets must pay the monthly Mantle plan and pass the on-chain verification step before unlocking the automation features.

## Per-user workspace

Every connected wallet has its own isolated workspace. Settings saved by one wallet are not visible to another wallet.

Saved per wallet:

- OpenAI key override, WordPress URL, WordPress JWT
- RSS filters, selected categories, content language, scoring settings
- Telegram API ID, API hash, phone, session name, source channel, target channels
- X auth token and ct0 cookie
- Facebook target URL and cookie JSON
- Social posting switches
- Telegram forwarding settings
- BlockScam keyword and chat settings

When a user logs back in with the same wallet, their saved setup is restored automatically.

## Dashboard pages

### Home

Command center with bot status, setup flow, and quick actions.

### User Profile

Wallet connection, subscription status, Credit Balance, payment button, payment verification, and account details.

### RSS / WordPress

Configure news sources, categories, freshness window, minimum score, WordPress credentials, image generation, and content language.

The selected language is synced across:

- WordPress title
- WordPress article
- Social summary
- Telegram post
- X post
- Facebook post

### Login & Cookies

Configure Telegram login and optional social cookies.

### Social Posting

Enable or disable Telegram, X, and Facebook posting.

### Telegram Forward

Configure one source channel and multiple target channels/groups.

### BlockScam

Configure monitored chats, scam/risk keywords, and optional AI scam detection. The monitor uses keyword rules first, then sends only unclear messages to the AI classifier. This keeps moderation more accurate without spending unnecessary API calls.

### System Logs

View live application logs.

## Environment variables

Minimum required for Railway:

```bash
RUNTIME_DIR=/data
PLAYWRIGHT_HEADLESS=1
```

Recommended server-owned subscription settings:

```bash
MANTLE_RPC_URL=https://rpc.mantle.xyz
EXPLORER_API_V2_URL=https://api.etherscan.io/v2/api
ETHERSCAN_API_KEY=your_etherscan_api_key
MONTHLY_MNT_AMOUNT=50
MONTHLY_CREDIT_AMOUNT=100
SUBSCRIPTION_DAYS=30
```

Optional global defaults:

```bash
OPENAI_API_KEY=your_openai_api_key
WP_URL=https://your-domain.com/wp-json/wp/v2/posts
WP_JWT=your_wordpress_jwt
CRYPTO_PANIC=your_cryptopanic_token
API_ID=your_telegram_api_id
API_HASH=your_telegram_api_hash
```

Notes:

- `ETHERSCAN_API_KEY` is recommended for reliable payment verification on Etherscan API V2 with `chainid=5000`.
- If `ETHERSCAN_API_KEY` is not set, the app can still try the explorer endpoint, but rate limits may be stricter.
- Do not commit API keys, JWTs, cookies, Telegram sessions, or database files to GitHub.

## Deploy to Railway

1. Push this repository to GitHub.
2. Create a new Railway project.
3. Choose **Deploy from GitHub repo**.
4. Add a Railway Volume.
5. Mount the volume at:

```bash
/data
```

6. Add the environment variables listed above.
7. Deploy.
8. Open the generated Railway domain.
9. Connect a wallet from **User Profile**.
10. Pay the monthly plan in MNT and click **Refresh Credit Balance**.
11. Configure WordPress, Telegram, social cookies, and automation settings.
12. Start the bot.

## Local development

Install dependencies:

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

Run the server:

```bash
uvicorn app:app --host 0.0.0.0 --port 8080
```

Open:

```text
http://localhost:8080
```

For local persistent data, the app uses `runtime/` by default. On Railway, it uses `/data` when `RUNTIME_DIR=/data` is set.

## WordPress setup

The WordPress REST URL should look like:

```text
https://your-domain.com/wp-json/wp/v2/posts
```

The JWT must allow creating posts and uploading media. If image generation is enabled, the app will create a featured image and upload it to the WordPress media endpoint.

## Telegram setup

1. Create a Telegram API app and get `API ID` and `API Hash`.
2. Open the dashboard.
3. Go to **Login & Cookies**.
4. Enter API ID, API Hash, phone number, and session name.
5. Click **Send Telegram Code**.
6. Enter the code received on Telegram.
7. If your account has 2FA, enter the 2FA password.
8. Click **Confirm Code**.
9. Use **Test Telegram Session**.

Telegram sessions are stored under the configured runtime directory and are separated by wallet workspace.

## API cost optimization

The publishing pipeline is optimized to publish 5 hot articles per day by default and reduce OpenAI API cost:

- The scheduler defaults to `Posts Per Day: 5`, which spaces publishing across the day. Each run scans available RSS/API candidates and selects the highest-scoring hot item in that batch.
- RSS candidate scoring uses a local heuristic by default instead of calling AI for every news item.
- Title, WordPress article, and social draft are generated in one text API call.
- The default text model is `gpt-5-nano`. You can switch to `gpt-5-mini` in the dashboard when stronger writing is needed.
- Featured image generation is controlled by an Image Policy:
  - `Off`: no image cost.
  - `High-score news only`: generate images only for selected high-impact news.
  - `Every post`: generate an image for every article.
- The default image setup is `gpt-image-2`, `low` quality, and `1536x1024` landscape.

Recommended production setup:

```text
Posts Per Day: 5
Text Model: gpt-5-nano
AI RSS Scoring: Off
Image Policy: High-score news only
Image Min Score: 9
Image Model: gpt-image-2
Image Quality: low
Image Size: 1536x1024
```

Use `gpt-5-mini` only for premium users or when article quality matters more than cost. Use `Image Policy: Off` if the WordPress theme already has default thumbnails or if you want the lowest possible cost.

## X / Facebook setup

The dashboard supports cookie-based posting with Playwright.

X requires:

```text
auth_token
ct0
```

Facebook requires cookie JSON and a target URL.

Because social platforms can change UI selectors or session policies at any time, the dashboard includes test buttons for X and Facebook. Use them after saving cookies.

## Payment verification

Payment verification checks native MNT transfers from the connected wallet to the fixed project owner wallet.

The app uses Etherscan API V2 with:

```text
chainid=5000
module=account
action=txlist
```

A valid transaction must:

- Be sent from the connected wallet.
- Be sent to the fixed project owner wallet.
- Be on Mantle Mainnet.
- Be a successful transaction.
- Meet or exceed `MONTHLY_MNT_AMOUNT`.
- Be within the configured monthly window, default `30` days.

The demo wallet skips this payment check and receives a full plan automatically.

## Security notes

- Never commit `.env`, `runtime/`, `/data`, `app.db`, browser profiles, Telegram sessions, or cookies.
- Use Railway Variables for server-owned secrets.
- The project owner wallet is fixed in code to prevent user-side replacement.
- The demo wallet is fixed in code for project review access.
- Users only see and save their own workspace settings.
- Cookies and API keys are sensitive. Keep the Railway project private and restrict dashboard access if needed.

## Suggested repository structure

```text
.
├── app.py
├── core.py
├── Dockerfile
├── railway.json
├── requirements.txt
├── README.md
└── README_RAILWAY.md
```

## Production checklist

- [ ] Railway Volume mounted to `/data`
- [ ] `RUNTIME_DIR=/data`
- [ ] `PLAYWRIGHT_HEADLESS=1`
- [ ] `ETHERSCAN_API_KEY` configured
- [ ] WordPress URL and JWT tested
- [ ] Telegram session tested
- [ ] X/Facebook cookies tested if social posting is enabled
- [ ] Wallet payment tested on Mantle Mainnet
- [ ] Credit Balance refresh tested
- [ ] GitHub repository excludes runtime files and secrets

## Mobile UX

The dashboard includes a responsive mobile interface for phone users:

- Sticky mobile top bar
- Slide-in sidebar menu
- Full-width touch-friendly buttons
- Single-column forms
- Larger mobile inputs to avoid iOS zoom
- Mobile-friendly logs, profile cards, and Web3 wallet actions

Open the app on a phone browser or in MetaMask mobile browser, then use the menu button in the top-left corner to switch between User Profile, RSS / WordPress, Social Posting, Telegram Forward, BlockScam, Login & Cookies, and System Logs.
