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

### Running headless (server, Docker, SSH, no display)

1. **Option A — Copy an existing token (recommended)**  
   Run the bot or the one-off auth once on a machine that has a browser (e.g. your laptop). After signing in, copy the token file to the headless server:
   - `gmail-token.json` (or `GMAIL_TOKEN_PATH`), and  
   - if you use per-inbox paths: the file under `gmail-tokens/` for that inbox.  
   Then start the bot on the server. It will load and refresh the token; no browser needed.

2. **Option B — Headless with paste-back**  
   On the headless server, set in `.env`:
   ```bash
   GMAIL_HEADLESS=1
   ```
   (or `HEADLESS=1`). Start the app (or run the one-off auth script). It will print an authorization URL. Open that URL in a browser (on any device), sign in with the monitored Gmail account. The browser will redirect to `http://localhost/?state=...&code=...` and show “can’t connect” (no server is running). Copy the **entire URL** from the address bar, paste it into the terminal when prompted, and press Enter. The app will exchange it for a token and save it;    no need to copy token files from another machine.

3. **Option C — Railway (or any public URL) OAuth callback**  
   Deploy the app to [Railway](https://railway.app) (or any host with a public URL). Railway sets `RAILWAY_PUBLIC_DOMAIN`; the app starts an HTTP server and uses `https://<RAILWAY_PUBLIC_DOMAIN>/gmail/oauth/callback` as the OAuth redirect.

   - In **Google Cloud Console** → your OAuth client → **Authorized redirect URIs**, add:
     `https://<your-railway-app>.up.railway.app/gmail/oauth/callback`
     (Use your real Railway URL.)
   - Deploy with `GMAIL_CREDENTIALS_JSON`, `GMAIL_MONITORED_EMAIL`, and Slack env vars set. Do **not** set `GMAIL_HEADLESS`.
   - After deploy, open **https://your-app.up.railway.app/gmail/oauth** in a browser. You’ll be redirected to Google to sign in; after authorizing, the app saves the token and Gmail polling starts.

   If your host is not Railway, set `GMAIL_REDIRECT_URI` to your app’s base URL and add that base + `/gmail/oauth/callback` to Google’s authorized redirect URIs.

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
