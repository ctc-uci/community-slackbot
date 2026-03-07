# Gmail API setup for email-subscription feature

The bot watches **one** Gmail inbox. Users subscribe to **sender** addresses: you get a DM only when that person (From) emails the monitored inbox. For example, if you subscribe to `email1@x.com`, you are notified when email1 sends to the monitored inbox; mail from `email2@x.com` does not notify you.

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

- `GMAIL_MONITORED_EMAIL` — the one Gmail address to monitor (e.g. `ctc-uci@gmail.com`). Token and history are stored as `gmail-token.json` and `gmail-history-id.txt` in the project root.
- Optionally: `GMAIL_CREDENTIALS_PATH`, `GMAIL_TOKEN_PATH`, `GMAIL_HISTORY_PATH`.

## 3. First-time auth (token)

On first run with Gmail enabled, the bot will open a browser for you to sign in with the **monitored** Gmail account. After authorizing, `gmail-token.json` is written. You only need to do this once per machine.

If the bot runs headless, run a one-off auth script:

```bash
cd ctc-bot
.venv/bin/python -c "
from features.gmail import get_gmail_credentials
get_gmail_credentials()
print('Token saved. You can start the bot.')
"
```

## 4. Firebase

The same Firebase project as the rest of the bot is used. A collection `email_subscriptions` is used with documents:

- `slack_id` (string)
- `email` (string) — the **sender** address this user is subscribed to (notify when this sender emails the monitored inbox)

No extra Firebase setup is required beyond existing `firebase-credentials.json`.

## 5. Slack app scope for images

To show images from emails in the forwarded Slack DMs, the Slack app needs the **`files:write`** scope. In [Slack API](https://api.slack.com/apps) → your app → **OAuth & Permissions** → Scopes → Bot Token Scopes, add `files:write`. Reinstall the app to the workspace if you add it later.

## 6. Slack commands

- **`/subscribe`** — With no args: list your sender subscriptions. With an email: subscribe to that **sender** (you’ll get DMs when that person emails the monitored inbox).
- **`/unsubscribe sender@example.com`** — Remove that sender subscription.

When the monitored inbox receives a message, the bot looks at the **From** address and notifies every user who subscribed to that sender, with a DM containing subject, from, and snippet.
