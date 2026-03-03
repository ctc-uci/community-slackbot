# Gmail API setup for email-subscription feature

The bot can watch one or several Gmail inboxes and DM Slack users when mail arrives to addresses they subscribed to. Each Slack user can subscribe to whichever addresses they want (e.g. 4–5 different monitored inboxes).

## 1. Google Cloud Console

1. Go to [Google Cloud Console](https://console.cloud.google.com/).
2. Create or select a project.
3. Enable **Gmail API**: APIs & Services → Library → search "Gmail API" → Enable.
4. Create OAuth 2.0 credentials:
   - APIs & Services → Credentials → Create Credentials → OAuth client ID.
   - Application type: **Desktop app**.
   - Download the JSON and save it in this directory as `gmail-credentials.json` (or set `GMAIL_CREDENTIALS_PATH` in `.env`). One OAuth client is used for all monitored accounts; each account signs in once to get its own token.

## 2. Environment

In `.env`:

- **Single account (legacy):** `GMAIL_MONITORED_EMAIL` — one Gmail address to monitor (e.g. `ctc-uci@gmail.com`). Token and history are stored as `gmail-token.json` and `gmail-history-id.txt` in the project root.
- **Multiple accounts (4–5 inboxes):** `GMAIL_MONITORED_EMAILS` — comma-separated list (e.g. `inbox1@gmail.com,inbox2@gmail.com,inbox3@gmail.com`). Tokens and history are stored under `gmail-tokens/` (one token and one history file per address). If both are set, `GMAIL_MONITORED_EMAILS` wins.
- Optionally: `GMAIL_CREDENTIALS_PATH`, `GMAIL_TOKENS_DIR` (default `gmail-tokens/` for multi-account), `GMAIL_TOKEN_PATH`, `GMAIL_HISTORY_PATH` (single-account only).

## 3. First-time auth (token)

On first run, the bot starts one poll thread per monitored account. For each account that doesn’t have a token yet, it will open a browser so you can sign in with **that** Gmail account. After authorizing, a token is written (single: `gmail-token.json`; multi: `gmail-tokens/token_<account>.json`). You only need to do this once per account per machine.

If the bot runs headless, run a one-off auth script per account (multi-account example):

```bash
cd ctc-bot
.venv/bin/python -c "
from features.gmail import get_gmail_credentials
# Replace with each monitored address; repeat for each account
get_gmail_credentials('inbox1@gmail.com')
get_gmail_credentials('inbox2@gmail.com')
print('Tokens saved. You can start the bot.')
"
```

## 4. Firebase

The same Firebase project as the rest of the bot is used. A collection `email_subscriptions` is used with documents:

- `slack_id` (string)
- `email` (string)

No extra Firebase setup is required beyond existing `firebase-credentials.json`.

## 5. Slack commands

- **`/subscribe`** — With no args: list your subscriptions. With an email: subscribe to that address (you’ll get DMs when **any** monitored inbox receives mail to it).
- **`/unsubscribe email@example.com`** — Remove that subscription.

When any monitored Gmail inbox receives a message, the bot notifies every user who subscribed to the message’s To (or Cc) address by sending a DM with subject, from, and snippet. Users can subscribe to one or several of the monitored addresses; each address is independent.
