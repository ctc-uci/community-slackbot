"""
Gmail-filter Slack bot: subscribe to email addresses; when any monitored inbox
receives mail to a subscribed address, forward a summary to all subscribers via Slack DM.

Supports multiple Gmail accounts (e.g. 4-5). Set GMAIL_MONITORED_EMAILS (comma-separated)
or a single GMAIL_MONITORED_EMAIL. Each Slack user subscribes to whichever addresses
they want; any monitored inbox receiving mail to that address triggers a DM.

- /subscribe [email] — add an email to your subscriptions (or list current ones)
- Uses Firebase (email_subscriptions collection) with schema: slack_id, email
"""

import os
import re
import threading
import time
from pathlib import Path

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from slack_sdk import WebClient

from firebase_client import get_firebase_app

# -----------------------------------------------------------------------------
# Config (set in .env; you complete credentials)
# -----------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent

GMAIL_CREDENTIALS_PATH = os.environ.get(
    "GMAIL_CREDENTIALS_PATH",
    str(PROJECT_ROOT / "gmail-credentials.json"),
)
# Directory for per-account token and history files (used when multiple accounts)
GMAIL_TOKENS_DIR = Path(
    os.environ.get("GMAIL_TOKENS_DIR", str(PROJECT_ROOT / "gmail-tokens"))
)
# Single-account legacy: one token file, one history file
GMAIL_TOKEN_PATH = os.environ.get(
    "GMAIL_TOKEN_PATH",
    str(PROJECT_ROOT / "gmail-token.json"),
)
GMAIL_HISTORY_PATH = os.environ.get(
    "GMAIL_HISTORY_PATH",
    str(PROJECT_ROOT / "gmail-history-id.txt"),
)
# One monitored account (legacy) or multiple (comma-separated, e.g. "a@gmail.com,b@gmail.com")
GMAIL_MONITORED_EMAIL = os.environ.get("GMAIL_MONITORED_EMAIL", "")
GMAIL_MONITORED_EMAILS_RAW = os.environ.get("GMAIL_MONITORED_EMAILS", "")

def _get_monitored_emails() -> list[str]:
    """List of monitored Gmail addresses (1 to 5+). Prefer GMAIL_MONITORED_EMAILS."""
    if GMAIL_MONITORED_EMAILS_RAW:
        emails = [e.strip().lower() for e in GMAIL_MONITORED_EMAILS_RAW.split(",") if e.strip()]
        if emails:
            return emails
    if GMAIL_MONITORED_EMAIL:
        return [GMAIL_MONITORED_EMAIL.strip().lower()]
    return []

# Gmail API scopes needed for reading mail
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Firebase collection for subscriptions: document fields slack_id, email
SUBSCRIPTIONS_COLLECTION = "email_subscriptions"


def _safe_email(account_email: str) -> str:
    """Filesystem-safe string for one account (token/history filenames)."""
    return (account_email or "").strip().lower().replace("@", "_at_")


def _normalize_email(addr: str) -> str:
    """Normalize for comparison: lowercase, strip."""
    return (addr or "").strip().lower()


def get_firestore_subscriptions():
    """Return Firestore collection for email_subscriptions."""
    get_firebase_app()
    from firebase_admin import firestore

    return firestore.client().collection(SUBSCRIPTIONS_COLLECTION)


def add_subscription(slack_id: str, email_address: str) -> bool:
    """Add (slack_id, email) if not already present. Returns True if added."""
    email_address = _normalize_email(email_address)
    if not email_address or "@" not in email_address:
        return False
    col = get_firestore_subscriptions()
    # Avoid duplicate: same user + same email
    for doc in col.where("slack_id", "==", slack_id).where("email", "==", email_address).limit(1).stream():
        return False
    col.add({"slack_id": slack_id, "email": email_address})
    return True


def remove_subscription(slack_id: str, email_address: str) -> bool:
    """Remove one subscription. Returns True if something was removed."""
    email_address = _normalize_email(email_address)
    col = get_firestore_subscriptions()
    for doc in col.where("slack_id", "==", slack_id).where("email", "==", email_address).stream():
        doc.reference.delete()
        return True
    return False


def list_subscriptions(slack_id: str) -> list[str]:
    """Return list of email addresses the user is subscribed to."""
    col = get_firestore_subscriptions()
    return [doc.get("email") for doc in col.where("slack_id", "==", slack_id).stream() if doc.get("email")]


def get_subscribers_for_email(email_address: str) -> list[str]:
    """Return list of slack_ids subscribed to this (normalized) address."""
    email_address = _normalize_email(email_address)
    col = get_firestore_subscriptions()
    return [doc.get("slack_id") for doc in col.where("email", "==", email_address).stream() if doc.get("slack_id")]


# -----------------------------------------------------------------------------
# Gmail API auth (OAuth2; one token per account when using multiple accounts)
# -----------------------------------------------------------------------------
def _token_path_for_account(account_email: str | None) -> Path:
    """Path to token file for this account. None = single-account legacy."""
    if not account_email:
        return Path(GMAIL_TOKEN_PATH)
    GMAIL_TOKENS_DIR.mkdir(parents=True, exist_ok=True)
    return GMAIL_TOKENS_DIR / f"token_{_safe_email(account_email)}.json"


def _history_path_for_account(account_email: str | None) -> Path:
    """Path to history-id file for this account. None = single-account legacy."""
    if not account_email:
        return Path(GMAIL_HISTORY_PATH)
    GMAIL_TOKENS_DIR.mkdir(parents=True, exist_ok=True)
    return GMAIL_TOKENS_DIR / f"history_{_safe_email(account_email)}.txt"


def get_gmail_credentials(account_email: str | None = None):
    """Load or refresh Gmail OAuth2 credentials for one account."""
    token_path = _token_path_for_account(account_email)
    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not Path(GMAIL_CREDENTIALS_PATH).exists():
                raise FileNotFoundError(
                    f"Gmail OAuth credentials not found at {GMAIL_CREDENTIALS_PATH}. "
                    "Download from Google Cloud Console (OAuth 2.0 Client ID) and run once to generate token."
                )
            flow = InstalledAppFlow.from_client_secrets_file(GMAIL_CREDENTIALS_PATH, GMAIL_SCOPES)
            auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline")
            login_hint = account_email or GMAIL_MONITORED_EMAIL or "the monitored Gmail account"
            print(f"[Gmail] Token missing. Sign in with: {login_hint}")
            print(f"[Gmail] Authorization URL: {auth_url}")
            creds = flow.run_local_server(port=0)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        with open(token_path, "w") as f:
            f.write(creds.to_json())
    return creds


def get_gmail_service(account_email: str | None = None):
    """Build Gmail API service for one account."""
    creds = get_gmail_credentials(account_email)
    return build("gmail", "v1", credentials=creds)


def _load_history_id(account_email: str | None = None) -> str | None:
    path = _history_path_for_account(account_email)
    if path.exists():
        return path.read_text().strip() or None
    return None


def _save_history_id(history_id: str, account_email: str | None = None) -> None:
    path = _history_path_for_account(account_email)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(history_id)


def _parse_message_recipients(msg) -> list[str]:
    """Extract To (and optionally Cc) addresses from Gmail message for matching."""
    headers = msg.get("payload", {}).get("headers") or []
    to_header = next((h["value"] for h in headers if h.get("name", "").lower() == "to"), "")
    cc_header = next((h["value"] for h in headers if h.get("name", "").lower() == "cc"), "")
    addresses = []
    for raw in (to_header, cc_header):
        # Simple parse: "Name <a@b.com>, c@d.com"
        for part in re.split(r",|\s", raw):
            part = part.strip()
            if not part:
                continue
            if "<" in part and ">" in part:
                m = re.search(r"<([^>]+)>", part)
                if m:
                    addresses.append(_normalize_email(m.group(1)))
            elif "@" in part:
                addresses.append(_normalize_email(part))
    return [a for a in addresses if a and "@" in a]


def _get_header(msg, name: str) -> str:
    headers = msg.get("payload", {}).get("headers") or []
    return next((h["value"] for h in headers if h.get("name", "").lower() == name.lower()), "")


def _format_email_for_slack(msg) -> str:
    """Build a short Slack-friendly summary of the message."""
    subject = _get_header(msg, "Subject") or "(no subject)"
    from_addr = _get_header(msg, "From")
    raw_snippet = (msg.get("snippet") or "")
    snippet = raw_snippet.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")[:500]
    if len(raw_snippet) > 500:
        snippet += "…"
    return f"*Subject:* {subject}\n*From:* {from_addr}\n*Snippet:* {snippet}"


def _process_new_message(service, message_id: str, slack_client: WebClient) -> None:
    """Fetch one message, determine recipients, notify subscribers."""
    try:
        msg = service.users().messages().get(userId="me", id=message_id, format="full").execute()
    except Exception:
        return
    recipients = _parse_message_recipients(msg)
    if not recipients:
        return
    text = _format_email_for_slack(msg)
    notified = set()
    for addr in recipients:
        for slack_id in get_subscribers_for_email(addr):
            if slack_id in notified:
                continue
            notified.add(slack_id)
            try:
                dm = slack_client.conversations_open(users=[slack_id])
                channel_id = dm.get("channel", {}).get("id")
                if channel_id:
                    slack_client.chat_postMessage(
                        channel=channel_id,
                        text=f"You're subscribed to *{addr}*. New email:\n\n{text}",
                    )
            except Exception:
                pass


def _poll_gmail_loop(account_email: str | None):
    """Background thread: poll one Gmail account; account_email None = single-account legacy."""
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        return
    slack_client = WebClient(token=token)
    service = None
    label = account_email or "single"
    while True:
        try:
            if service is None:
                service = get_gmail_service(account_email)
            start_id = _load_history_id(account_email)
            if start_id:
                try:
                    hist = (
                        service.users()
                        .history()
                        .list(userId="me", startHistoryId=start_id, historyTypes=["messageAdded"])
                        .execute()
                    )
                except Exception as e:
                    if getattr(e, "resp", None) and getattr(e.resp, "status", None) == 404:
                        start_id = None
                    else:
                        time.sleep(60)
                        continue
            else:
                hist = None

            if start_id is None or hist is None:
                profile = service.users().getProfile(userId="me").execute()
                new_history_id = profile.get("historyId")
                if new_history_id:
                    _save_history_id(str(new_history_id), account_email)
                time.sleep(60)
                continue

            new_history_id = hist.get("historyId")
            if new_history_id:
                _save_history_id(str(new_history_id), account_email)
            for rec in hist.get("history", []):
                for msg_added in rec.get("messagesAdded", []):
                    mid = msg_added.get("message", {}).get("id")
                    if mid:
                        _process_new_message(service, mid, slack_client)
        except FileNotFoundError:
            # Credentials not set up yet
            pass
        except Exception:
            pass
        time.sleep(30)


def register_gmail_handlers(app):
    """Register /subscribe (and /unsubscribe) and start Gmail polling."""

    @app.command("/subscribe")
    def cmd_subscribe(ack, body, client, logger):
        try:
            ack()
            user_id = body["user_id"]
            text = (body.get("text") or "").strip()
            if not text:
                # No email provided: show current subscriptions and hint
                subs = list_subscriptions(user_id)
                monitored = _get_monitored_emails()
                hint = "Add: `/subscribe email@example.com`"
                if monitored:
                    hint += f"\nMonitored inboxes you can subscribe to: {', '.join(monitored)}"
                if not subs:
                    client.chat_postEphemeral(
                        channel=body["channel_id"],
                        user=user_id,
                        text=f"You have no email subscriptions. {hint}",
                    )
                else:
                    client.chat_postEphemeral(
                        channel=body["channel_id"],
                        user=user_id,
                        text="Your subscriptions:\n• " + "\n• ".join(subs) + "\n\n" + hint,
                    )
                return
            added = add_subscription(user_id, text)
            if added:
                client.chat_postEphemeral(
                    channel=body["channel_id"],
                    user=user_id,
                    text=f"Subscribed to *{text}*. You'll get Slack DMs when any monitored inbox receives mail to this address.",
                )
            else:
                client.chat_postEphemeral(
                    channel=body["channel_id"],
                    user=user_id,
                    text=f"You're already subscribed to *{text}*, or the address is invalid.",
                )
        except Exception as e:
            logger.exception("Subscribe failed: %s", e)
            raise

    @app.command("/unsubscribe")
    def cmd_unsubscribe(ack, body, client, logger):
        try:
            ack()
            user_id = body["user_id"]
            text = (body.get("text") or "").strip()
            if not text:
                subs = list_subscriptions(user_id)
                if not subs:
                    client.chat_postEphemeral(
                        channel=body["channel_id"],
                        user=user_id,
                        text="You have no subscriptions. Use `/unsubscribe email@example.com` to remove one.",
                    )
                else:
                    client.chat_postEphemeral(
                        channel=body["channel_id"],
                        user=user_id,
                        text="Your subscriptions:\n• " + "\n• ".join(subs) + "\n\nRemove: `/unsubscribe email@example.com`",
                    )
                return
            removed = remove_subscription(user_id, text)
            if removed:
                client.chat_postEphemeral(
                    channel=body["channel_id"],
                    user=user_id,
                    text=f"Unsubscribed from *{text}*.",
                )
            else:
                client.chat_postEphemeral(
                    channel=body["channel_id"],
                    user=user_id,
                    text=f"You weren't subscribed to *{text}* (or invalid address).",
                )
        except Exception as e:
            logger.exception("Unsubscribe failed: %s", e)
            raise

    # Start one Gmail poll thread per monitored account
    monitored = _get_monitored_emails()
    if monitored and Path(GMAIL_CREDENTIALS_PATH).exists():
        use_legacy_single = len(monitored) == 1 and bool(GMAIL_MONITORED_EMAIL) and not GMAIL_MONITORED_EMAILS_RAW
        for i, account in enumerate(monitored):
            arg = None if use_legacy_single else account
            t = threading.Thread(target=_poll_gmail_loop, args=(arg,), daemon=True)
            t.start()
            # Stagger starts so first-time auth opens one browser at a time
            if i < len(monitored) - 1:
                time.sleep(5)
        print(f"[Gmail] Polling {len(monitored)} account(s): {', '.join(monitored)}")
