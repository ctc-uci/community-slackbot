# Gmail API setup for email-subscription feature

The bot can watch a Gmail inbox and DM Slack users when mail arrives to addresses they subscribed to.

## 1. Google Cloud Console

1. Go to [Google Cloud Console](https://console.cloud.google.com/).
2. Create or select a project.
3. Enable **Gmail API**: APIs & Services → Library → search "Gmail API" → Enable.
4. Create OAuth 2.0 credentials:
   - APIs & Services → Credentials → Create Credentials → OAuth client ID.
   - Application type: **Desktop app**.
   - Download the JSON and save it in this directory as `gmail-credentials.json` (or set `GMAIL_CREDENTIALS_PATH` in `.env`).

## 2. Environment

In `.env`:

- `GMAIL_MONITORED_EMAIL` — the Gmail address to monitor (e.g. `ctc-uci@gmail.com`).
- Optionally: `GMAIL_CREDENTIALS_PATH`, `GMAIL_TOKEN_PATH`, `GMAIL_HISTORY_PATH` (defaults are in project root).

## 3. First-time auth (token)

On first run with Gmail enabled, the bot will open a browser for you to sign in with the **monitored** Gmail account. After authorizing, a `gmail-token.json` is written (or path from `GMAIL_TOKEN_PATH`). You only need to do this once per machine.

If the bot runs headless, run a one-off auth script that uses the same paths:

```bash
cd ctc-bot
venv/bin/python -c "
from features.gmail import get_gmail_credentials
get_gmail_credentials()
print('Token saved. You can start the bot.')
"
```

## 4. Firebase

The same Firebase project as the rest of the bot is used. A collection `email_subscriptions` is used with documents:

- `slack_id` (string)
- `email` (string)

No extra Firebase setup is required beyond existing `firebase-credentials.json`.

## 5. Slack commands

- **`/subscribe`** — With no args: list your subscriptions. With an email: subscribe to that address (you’ll get DMs when the monitored inbox receives mail to it).
- **`/unsubscribe email@example.com`** — Remove that subscription.

When the monitored Gmail receives a message, the bot notifies every user who subscribed to the message’s To (or Cc) address by sending a DM with subject, from, and snippet.
